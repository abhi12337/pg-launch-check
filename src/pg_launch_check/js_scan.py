"""JavaScript / TypeScript application code: Prisma, Drizzle, node-postgres (pg), postgres.js,
Knex raw SQL. Built on the lexer in jslex.py (there is no JS parser in the standard library):
comments and string contents are blanked so patterns only match real code."""

from __future__ import annotations

import bisect
import re

from .appcommon import (LIKE_PARAM_RE, OFFSET_PARAM_RE, SMALL_TABLE_RE, ProjectCtx, like_columns, looks_sql,
                        select_star, trgm_extra, unbounded_select)
from .findings import Finding, clip_line
from .jslex import StrTok, lex, match_close, split_args, strip_span

IDENT = r"[A-Za-z_$][\w$]*"
IMPORT_RE = re.compile(r"""(?:from\s+|require\(\s*|import\(\s*)["']([^"']+)["']""")
NON_PG_LIBS = {"mysql", "mysql2", "mysql2/promise", "sqlite3", "better-sqlite3", "sqlite", "@libsql/client",
               "mssql", "tedious", "oracledb", "mongodb", "mongoose"}
PRISMA_READ = ("findMany", "findFirst", "findFirstOrThrow", "findUnique", "findUniqueOrThrow", "count",
               "aggregate", "groupBy")
PRISMA_CALL_RE = re.compile(r"\b((?:" + IDENT + r"\s*\.\s*)*?" + IDENT + r")\s*\.\s*(" + IDENT + r")\s*\.\s*("
                            + "|".join(PRISMA_READ) + r")\s*\(")
DRIZZLE_REL_RE = re.compile(r"\b(" + IDENT + r")\s*\.\s*query\s*\.\s*(" + IDENT + r")\s*\.\s*(findMany|findFirst)\s*\(")
DRIZZLE_SELECT_RE = re.compile(r"\b(" + IDENT + r")\s*\.\s*(select|selectDistinct)\s*\(")
RAW_SINK_RE = re.compile(r"\.\s*(query|execute|unsafe|\$queryRawUnsafe|\$executeRawUnsafe|raw|whereRaw|orWhereRaw|"
                         r"havingRaw|orderByRaw|joinRaw|fromRaw|selectRaw)\s*\(")
SQL_RAW_FN_RE = re.compile(r"\bsql\s*\.\s*raw\s*\(")
FRAGMENT_SINKS = {"whereRaw", "orWhereRaw", "havingRaw", "orderByRaw", "joinRaw", "fromRaw", "selectRaw", "sql.raw"}
POOLER_PKGS = ("@neondatabase/serverless", "@vercel/postgres", "@prisma/extension-accelerate")


class JsScan:
    def __init__(self, rel: str, text: str, proj: ProjectCtx):
        self.rel = rel
        self.text = text
        self.proj = proj
        self.lines = text.splitlines()
        self.clean, self.code, self.toks = lex(text)
        self.tok_at = {t.start: t for t in self.toks}
        self.tok_starts = [t.start for t in self.toks]
        self.line_starts = [0] + [m.end() for m in re.finditer(r"\n", text)]
        self.imports = {m.group(1) for m in IMPORT_RE.finditer(self.clean)}
        self.findings: list[Finding] = []
        self._depth: list[int] | None = None
        self._seen: set = set()

    # ------------------------------------------------------------ basics
    def line_of(self, off: int) -> int:
        return bisect.bisect_right(self.line_starts, off)

    def depth_at(self, off: int) -> int:
        if self._depth is None:
            d, arr = 0, []
            for ch in self.code:
                arr.append(d)
                if ch == "{":
                    d += 1
                elif ch == "}":
                    d -= 1
            self._depth = arr
        return self._depth[off] if off < len(self._depth) else 0

    def add(self, rule: str, sev: str, msg: str, off: int, heuristic: bool = False, **extra) -> None:
        line = self.line_of(off)
        if (rule, line) in self._seen:
            return
        self._seen.add((rule, line))
        raw = self.lines[line - 1] if 0 < line <= len(self.lines) else ""
        self.findings.append(Finding(rule, sev, msg, self.rel, line, clip_line(raw), heuristic,
                                     extra={k: v for k, v in extra.items() if v is not None}))

    def tok_covering(self, a: int, b: int) -> StrTok | None:
        tok = self.tok_at.get(a)
        return tok if tok is not None and tok.end >= b else None

    def tok_in(self, a: int, b: int) -> list[StrTok]:
        i = bisect.bisect_left(self.tok_starts, a)
        out = []
        while i < len(self.toks) and self.toks[i].start < b:
            out.append(self.toks[i])
            i += 1
        return out

    def tok_text(self, tok: StrTok) -> str:
        """Static text with $X where each ${...} was."""
        if not tok.has_interp:
            return tok.text
        out, prev = [], 0
        for pos in tok.interps:
            out.append(tok.text[prev:pos])
            out.append("$X")
            prev = pos
        out.append(tok.text[prev:])
        return "".join(out)

    def interp_exprs(self, tok: StrTok) -> list[tuple[int, int]]:
        spans = []
        k = tok.start
        while True:
            k = self.code.find("${", k, tok.end)
            if k == -1:
                break
            c = match_close(self.code, k + 1)
            spans.append((k + 2, c))
            k = c + 1
        return spans

    def chain_end(self, open_idx: int) -> tuple[int, list[str]]:
        """End offset of a call chain starting at the '(' at open_idx, and the method names after it."""
        code = self.code
        c = match_close(code, open_idx)
        methods = []
        while True:
            m = re.compile(r"\s*\??\.\s*(" + IDENT + r")\s*(\()").match(code, c + 1)
            if not m:
                break
            methods.append(m.group(1))
            c = match_close(code, m.start(2))
        return c, methods

    # ------------------------------------------------------------ spans
    def collect_spans(self) -> None:
        code = self.code
        self.funcs: list[tuple[int, int]] = []
        for m in re.finditer(r"=>\s*\{|\bfunction\b[^{;]*?\{|^\s*(?:(?:public|private|protected|static|async)\s+)*"
                             + IDENT + r"\s*\([^)]*\)\s*(?::\s*[^{;]+)?\{", code, re.M):
            o = m.end() - 1
            self.funcs.append((o, match_close(code, o)))
        self.handlers: list[tuple[int, int]] = []

        def body_after(idx: int):
            """Body of the function whose parameter list starts after idx (skips the parameters)."""
            p = code.find("(", idx)
            if p == -1 or p - idx > 200 or "{" in code[idx:p] or ";" in code[idx:p]:
                m = re.compile(r"[^{;]*?\{").match(code, idx)
                if not m:
                    return None
                o = m.end() - 1
                return o, match_close(code, o)
            c = match_close(code, p)
            m = re.compile(r"\s*(?::\s*[^{;=]*?(?:<[^;]*?>)?\s*)?(?:=>\s*)?\{").match(code, c + 1)
            if not m:
                return None
            o = m.end() - 1
            return o, match_close(code, o)

        for m in re.finditer(r"\bexport\s+(?:async\s+)?function\s+(?:GET|POST|PUT|PATCH|DELETE|loader|action|load|"
                             r"getServerSideProps|handler)\b", code):
            span = body_after(m.end())
            if span:
                self.handlers.append(span)
        for m in re.finditer(r"\b(?:export\s+const|exports\.)\s*(?:GET|POST|PUT|PATCH|DELETE|loader|action|load|"
                             r"getServerSideProps|handler)\b\s*(?::[^=]+)?=", code):
            span = body_after(m.end())
            if span:
                self.handlers.append(span)
        if re.search(r"(^|/)(pages/api/|app/(.*/)?(page|route|layout)\.[jt]sx?$)", self.rel):
            for m in re.finditer(r"\bexport\s+default\s+(?:async\s+)?function\b", code):
                span = body_after(m.end())
                if span:
                    self.handlers.append(span)
        for m in re.finditer(r"\b(?:app|router|server|api|fastify|hono|route|routes|r|instance)\s*\.\s*"
                             r"(?:get|post|put|patch|delete|all|route)\s*\(\s*['\"`]", code):
            o = code.index("(", m.start())
            self.handlers.append((o, match_close(code, o)))
        for m in re.finditer(r"\b(?:defineEventHandler|eventHandler)\s*\(|\.\s*(?:query|mutation)\s*\(\s*async\s*\(",
                             code):
            o = code.index("(", m.start())
            self.handlers.append((o, match_close(code, o)))
        if re.match(r"\s*['\"]use server['\"]", self.clean):
            self.handlers.append((0, len(code)))
        # loops: (start, end, iterable, loop vars, line, kind)
        self.loops: list[tuple[int, int, str, set, int, str]] = []
        for m in re.finditer(r"\bfor\s*(?:await\s*)?\(", code):
            o = m.end() - 1
            c = match_close(code, o)
            header = code[o + 1:c]
            hm = re.match(r"\s*(?:const|let|var)?\s*(.+?)\s+(?:of|in)\s+(.+)$", header, re.S)
            if hm:
                names = set(re.findall(IDENT, re.sub(r":\s*" + IDENT, "", hm.group(1))))
                iterable = hm.group(2).strip()
            else:
                cm = re.match(r"\s*(?:let|var)?\s*(" + IDENT + r")\s*=", header)
                if not cm or re.search(r";\s*\w+\s*<=?\s*\d+\s*;", header):
                    continue
                names = {cm.group(1)}
                im = re.search(r"<=?\s*([\w$.]+?)\.length", header)
                iterable = im.group(1) if im else ""
            k = c + 1
            while k < len(code) and code[k].isspace():
                k += 1
            end = match_close(code, k) if k < len(code) and code[k] == "{" else code.find(";", k)
            self.loops.append((o, end if end > 0 else len(code), iterable, names, self.line_of(m.start()), "for"))
        for m in re.finditer(r"([\w$.\])]+)\s*\.\s*(forEach|map|flatMap)\s*\(", code):
            o = m.end() - 1
            c = match_close(code, o)
            inner = code[o + 1:c]
            pm = re.match(r"\s*(?:async\s*)?(?:function\s*" + IDENT + r"?\s*)?\(([^)]*)\)", inner) or \
                re.match(r"\s*(?:async\s+)?(" + IDENT + r")\s*=>", inner)
            if not pm:
                continue
            params = re.sub(r":\s*[\w$<>\[\].| ]+", "", pm.group(1))
            names = set(re.findall(IDENT, params)) - {"async"}
            kind = "map" if m.group(2) in ("map", "flatMap") else "forEach"
            self.loops.append((o, c, m.group(1), names, self.line_of(m.start()), kind))
        self.whiles = []
        for m in re.finditer(r"\bwhile\s*\(", code):
            c = match_close(code, m.end() - 1)
            k = c + 1
            while k < len(code) and code[k].isspace():
                k += 1
            self.whiles.append((m.start(), match_close(code, k) if k < len(code) and code[k] == "{" else c))

    def in_handler(self, off: int) -> bool:
        return any(a < off < b for a, b in self.handlers)

    def in_function(self, off: int) -> bool:
        return any(a < off < b for a, b in self.funcs)

    def loop_at(self, off: int):
        best = None
        for lp in self.loops:
            if lp[0] < off <= lp[1] and (best is None or lp[0] > best[0]):
                best = lp
        if best is None:
            return None
        for wa, wb in self.whiles:
            if best[0] < wa < off <= wb:
                return None
        return best

    # ------------------------------------------------------------ string expressions
    def rhs_spans(self, name: str, before: int) -> list[tuple[int, int]]:
        code = self.code
        spans = []
        for m in re.finditer(r"\b(?:const|let|var)\s+" + re.escape(name) + r"\b\s*(?::[^=;]+)?=(?!=)", code):
            if m.start() < before:
                spans.append((m.end(), self._expr_end(m.end())))
        for m in re.finditer(r"(?<![\w$.])" + re.escape(name) + r"\s*\+=", code):
            if m.start() < before:
                spans.append((m.end(), self._expr_end(m.end())))
        return spans

    def _expr_end(self, a: int) -> int:
        code = self.code
        depth = 0
        k = a
        while k < len(code):
            ch = code[k]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                if depth == 0:
                    return k
                depth -= 1
            elif depth == 0 and ch in ";,":
                return k
            elif depth == 0 and ch == "\n":
                prev = code[a:k].rstrip()
                nxt = code[k:].lstrip()
                if prev and prev[-1] not in "+=(,?:" and not nxt.startswith(("+", ".", "?", ":")):
                    return k
            k += 1
        return k

    def is_safe(self, expr: str, depth: int = 0) -> bool:
        e = expr.strip()
        if re.fullmatch(r"-?[\d_.]+", e) or re.fullmatch(r"[A-Z][A-Z0-9_]*", e):
            return True
        if re.search(r"\bNumber\s*\(|\bparseInt\s*\(|\bparseFloat\s*\(|\bMath\.|\.length\b|\bNumber\.", e):
            return True
        if "$${" in e or re.search(r"""['"`]\$['"`]\s*\+""", e) or re.search(r"\$\$\{", e):
            return True
        if re.fullmatch(IDENT, e) and depth < 2:
            spans = self.rhs_spans(e, len(self.code))
            if spans:
                return all(self._rhs_safe(a, b, depth + 1) for a, b in spans)
        return False

    def _rhs_safe(self, a: int, b: int, depth: int) -> bool:
        a, b = strip_span(self.code, a, b)
        tok = self.tok_covering(a, b)
        if tok is not None:
            return not tok.has_interp or all(self.is_safe(self.code[x:y], depth) for x, y in self.interp_exprs(tok))
        src = self.clean[a:b]
        if "$${" in src or re.search(r"""['"`]\$['"`]\s*\+""", src):
            return True
        return self.is_safe(self.code[a:b], depth)

    def str_expr(self, a: int, b: int, depth: int = 0):
        """(static text with $X markers, [unsafe expressions], first string offset) or None."""
        a, b = strip_span(self.code, a, b)
        if a >= b:
            return None
        tok = self.tok_covering(a, b)
        if tok is not None:
            unsafe = [self.code[x:y].strip() for x, y in self.interp_exprs(tok)
                      if not self.is_safe(self.code[x:y])]
            return self.tok_text(tok), unsafe, a
        expr = self.code[a:b]
        if re.fullmatch(IDENT, expr) and depth < 2:
            parts_text, unsafe, first = "", [], None
            for ra, rb in self.rhs_spans(expr, a):
                r = self.str_expr(ra, rb, depth + 1)
                if r is None:
                    continue
                parts_text += r[0]
                unsafe += r[1]
                first = first if first is not None else r[2]
            return (parts_text, unsafe, first if first is not None else a) if parts_text else None
        if "+" in expr and self.tok_in(a, b):
            text, unsafe = "", []
            depth_ = 0
            pieces, start = [], a
            for k in range(a, b):
                ch = self.code[k]
                if ch in "([{":
                    depth_ += 1
                elif ch in ")]}":
                    depth_ -= 1
                elif ch == "+" and depth_ == 0:
                    pieces.append((start, k))
                    start = k + 1
            pieces.append((start, b))
            for pa, pb in pieces:
                pa, pb = strip_span(self.code, pa, pb)
                t = self.tok_covering(pa, pb)
                if t is not None:
                    text += self.tok_text(t)
                    unsafe += [self.code[x:y].strip() for x, y in self.interp_exprs(t)
                               if not self.is_safe(self.code[x:y])]
                    continue
                sub = self.code[pa:pb]
                if re.fullmatch(IDENT, sub) and depth < 2:
                    r = self.str_expr(pa, pb, depth + 1)
                    if r is not None:
                        text += r[0]
                        unsafe += r[1]
                        continue
                text += "$X"
                if not self.is_safe(sub):
                    unsafe.append(sub.strip())
            return text, unsafe, a
        return None

    # ------------------------------------------------------------ run
    def run(self) -> list[Finding]:
        if self.imports & NON_PG_LIBS and not self.imports & {"pg", "postgres", "@prisma/client", "drizzle-orm"}:
            return []
        self.collect_spans()
        self.uses_drizzle = self.proj.drizzle or any(i.startswith("drizzle-orm") for i in self.imports)
        self.uses_prisma = self.proj.prisma or "@prisma/client" in self.imports
        self.sql_injection()
        self.clients()
        self.prisma_calls()
        self.drizzle_calls()
        self.raw_sql_strings()
        return self.findings

    # ------------------------------------------------------------ PGL011
    def sql_injection(self) -> None:
        code = self.code
        sinks = [(m, m.group(1)) for m in RAW_SINK_RE.finditer(code)] + \
                [(m, "sql.raw") for m in SQL_RAW_FN_RE.finditer(code)]
        for m, method in sinks:
            o = m.end() - 1
            c = match_close(code, o)
            args = split_args(code, o + 1, c)
            if not args:
                continue
            a, b = args[0]
            info = self.str_expr(a, b)
            if info is None:
                continue
            text, unsafe, _ = info
            if not unsafe:
                continue
            fragment = method in FRAGMENT_SINKS
            if not fragment and not looks_sql(text):
                continue
            if method in ("raw",) and not looks_sql(text):
                continue
            recv = code[max(0, m.start() - 40):m.start()]
            rm = re.search(r"([\w$.]+)\s*$", recv)
            recv_name = rm.group(1) if rm else "pool"
            if method in ("$queryRawUnsafe", "$executeRawUnsafe"):
                key = "prisma"
            elif method == "sql.raw" or (method == "execute" and self.uses_drizzle):
                key = "drizzle"
            elif method == "unsafe":
                key = "postgres-js"
            elif method in FRAGMENT_SINKS or method == "raw" or "knex" in self.imports:
                key = "knex"
            else:
                key = "node-pg"
            shown = ", ".join(sorted(set(unsafe))[:3])
            self.add("PGL011", "high",
                     f"{method}() receives SQL built from {shown} by string interpolation or concatenation: a value "
                     f"containing a quote changes the query (SQL injection). Pass it as a parameter.",
                     m.start(), fixkeys=[key], recv=recv_name)

    # ------------------------------------------------------------ PGL012 / 018 / 019 / 020
    def clients(self) -> None:
        code = self.code
        pg_imported = "pg" in self.imports or re.search(r"\bpg\s*\.\s*(Pool|Client)\b", code) is not None
        neon = "@neondatabase/serverless" in self.imports
        pjs_name = None
        pm = re.search(r"\bimport\s+(" + IDENT + r")\s+from\s+['\"]postgres['\"]", self.clean) or \
            re.search(r"\b(?:const|let|var)\s+(" + IDENT + r")\s*=\s*require\(\s*['\"]postgres['\"]\s*\)", self.clean)
        if pm:
            pjs_name = pm.group(1)
        pats = []
        if pg_imported and not neon:
            pats.append((re.compile(r"\bnew\s+(?:pg\s*\.\s*)?(Pool|Client)\s*\("), "node-pg"))
        if self.uses_prisma or "@prisma/client" in self.imports:
            pats.append((re.compile(r"\bnew\s+(PrismaClient)\s*\("), "prisma"))
        if pjs_name:
            pats.append((re.compile(r"(?<![\w$.])(" + re.escape(pjs_name) + r")\s*\("), "postgres-js"))
        file_has_timeout = "statement_timeout" in self.text or "query_timeout" in self.text
        for rx, lib in pats:
            for m in rx.finditer(code):
                kind = m.group(1)
                o = code.index("(", m.end() - 1)
                c = match_close(code, o)
                args = split_args(code, o + 1, c)
                opts = None
                if args:
                    a, b = strip_span(code, *args[-1] if lib == "postgres-js" and len(args) > 1 else args[0])
                    if code[a:b].startswith("{"):
                        opts = self.clean[a:b]
                    elif lib == "postgres-js" and len(args) == 1:
                        opts = ""
                elif lib != "postgres-js":
                    opts = ""
                label = {"Pool": "new Pool()", "Client": "new Client()", "PrismaClient": "new PrismaClient()"}.get(
                    kind, f"{kind}()")
                if self.in_handler(m.start()):
                    sev = "medium" if kind == "Client" else "high"
                    closes = re.search(r"\.\s*(end|\$disconnect)\s*\(", code[m.start():self._handler_end(m.start())])
                    tail = ("opens a new connection on every request (it is closed, but every request still pays "
                            "the connection setup)" if closes and kind == "Client" else
                            "opens new connections on every request" + (
                                "" if closes else " and they are never closed, so they pile up until max_connections"))
                    self.add("PGL012", sev, f"{label} is created inside a request handler: it {tail}.",
                             m.start(), fixkeys=[lib])
                    continue
                if lib in ("node-pg", "postgres-js") and kind != "Client" and opts is not None:
                    if not re.search(r"\bmax\s*:", opts):
                        default = "10" if lib == "node-pg" else "10"
                        self.add("PGL018", "low",
                                 f"{label} uses the driver's default pool size ({default} connections per instance): "
                                 f"set max so instances x pool size stays below max_connections.",
                                 m.start(), fixkeys=[lib])
                if lib in ("node-pg", "postgres-js") and opts is not None and not file_has_timeout and \
                        not self.proj.role_statement_timeout:
                    self.add("PGL019", "low",
                             f"{label} sets no statement_timeout: a runaway query keeps running and holds the "
                             f"connection long after the request gave up.",
                             m.start(), fixkeys=[lib, "default"], with_default=True)
                if self.proj.serverless and not self.proj.pooler_hint and not self.proj.serverless_reported and \
                        not self.in_handler(m.start()):
                    self.proj.serverless_reported = True
                    self.add("PGL020", "info",
                             f"This project deploys to a serverless platform and {label} connects without any "
                             f"pooler hint in the repo (no pooled URL, pgbouncer=true, Neon / Vercel Postgres "
                             f"driver or Prisma directUrl): each function instance opens its own connections.",
                             m.start(), fixkeys=["default"])

    def _handler_end(self, off: int) -> int:
        best = None
        for a, b in self.handlers:
            if a < off < b and (best is None or a > best[0]):
                best = (a, b)
        return best[1] if best else off

    # ------------------------------------------------------------ Prisma
    def prisma_calls(self) -> None:
        if not self.uses_prisma:
            return
        code = self.code
        for m in PRISMA_CALL_RE.finditer(code):
            recv, model, method = re.sub(r"\s+", "", m.group(1)), m.group(2), m.group(3)
            last = recv.split(".")[-1]
            if recv.endswith(".query") or last == "query" or model in ("query",):
                continue
            if not (re.search(r"(?i)prisma", recv) or last in ("db", "tx", "client", "database", "orm", "trx")):
                continue
            o = m.end() - 1
            c = match_close(code, o)
            call_src = code[m.start():c + 1]
            arg = self.clean[o + 1:c].strip()
            obj_arg = arg.startswith("{") or arg == ""
            # N+1
            lp = self.loop_at(m.start())
            if lp and method in PRISMA_READ:
                a_, b_, iterable, names, lline, kind = lp
                used = [n for n in names if re.search(r"(?<![\w$.])" + re.escape(n) + r"\b", self.code[o:c])]
                batched = kind == "map" and method in ("findUnique", "findUniqueOrThrow")
                if used and not batched:
                    km = re.search(r"(?<![\w$.])" + re.escape(used[0]) + r"\s*\.\s*(" + IDENT + r")", self.code[o:c])
                    key = km.group(1) if km else "id"
                    outer = self._outer_model(iterable, m.start())
                    self.add("PGL013", "medium",
                             f"{recv}.{model}.{method}() runs once per item of {iterable or 'the loop'} (loop at "
                             f"line {lline}): one query per row. Load it with the first query (include) or with "
                             f"one {{ in: [...] }} query.",
                             m.start(), heuristic=True, fixkeys=["prisma"], model=outer or "parent", target=model,
                             key=key, relation=re.sub(r"Id$", "", key) if key.endswith("Id") else model)
            if method != "findMany" or not obj_arg:
                continue
            top = self._top_keys(o + 1, c)
            # PGL015 offset
            if "skip" in top and "cursor" not in top:
                val = top["skip"]
                if not re.fullmatch(r"\d+", val.strip()):
                    self.add("PGL015", "info",
                             f"{recv}.{model}.findMany() pages with skip: {val.strip()}: Postgres reads and discards "
                             f"every skipped row, so deep pages get slower.",
                             m.start(), fixkeys=["prisma"], model=model)
            # PGL014 unbounded
            if self.in_handler(m.start()) and "take" not in top and not SMALL_TABLE_RE.match(model) and \
                    not re.search(r"\bin\s*:", call_src) and not re.search(r"\bdistinct\s*:", call_src):
                self.add("PGL014", "low",
                         f"{recv}.{model}.findMany() in a request handler has no take: it returns every matching "
                         f"row, however many the table grows to.",
                         m.start(), heuristic=True, fixkeys=["prisma"], model=model)
        # PGL017 contains / endsWith
        for m in re.finditer(r"\b(" + IDENT + r")\s*:\s*\{[^{}]*?\b(contains|endsWith)\s*:", code):
            if not self._inside_prisma_call(m.start()):
                continue
            col = m.group(1)
            if self.proj.has_trgm(col):
                continue
            mm = self._prisma_model_at(m.start())
            self.add("PGL017", "low",
                     f"Prisma {m.group(2)} on {col} becomes LIKE '%...%' / ILIKE: without a trigram index every search "
                     f"scans the whole table.",
                     m.start(), heuristic=True, fixkeys=["prisma"], col=col, model=mm or "Model")

    def _top_keys(self, a: int, b: int) -> dict:
        code = self.code
        a, b = strip_span(code, a, b)
        if a >= b or code[a] != "{":
            return {}
        out = {}
        for pa, pb in split_args(code, a + 1, match_close(code, a)):
            pa, pb = strip_span(code, pa, pb)
            part = self.clean[pa:pb]
            km = re.match(r"(" + IDENT + r")\s*:\s*(.*)$", part, re.S)
            if km:
                out[km.group(1)] = km.group(2)
            elif re.fullmatch(IDENT, part):
                out[part] = part
        return out

    def _outer_model(self, iterable: str, before: int) -> str | None:
        if not iterable or not re.fullmatch(r"[\w$]+", iterable):
            return None
        rx = re.compile(r"\b(?:const|let|var)\s+" + re.escape(iterable) + r"\s*=\s*(?:await\s+)?[\w$.]+\s*\.\s*("
                        + IDENT + r")\s*\.\s*findMany\b")
        found = None
        for m in rx.finditer(self.code[:before]):
            found = m.group(1)
        return found

    def _inside_prisma_call(self, off: int) -> bool:
        for m in PRISMA_CALL_RE.finditer(self.code[:off]):
            o = m.end() - 1
            if match_close(self.code, o) > off:
                return True
        return False

    def _prisma_model_at(self, off: int) -> str | None:
        found = None
        for m in PRISMA_CALL_RE.finditer(self.code[:off]):
            o = m.end() - 1
            if match_close(self.code, o) > off:
                found = m.group(2)
        return found[:1].upper() + found[1:] if found else None

    # ------------------------------------------------------------ Drizzle
    def drizzle_calls(self) -> None:
        if not self.uses_drizzle:
            return
        code = self.code
        for m in DRIZZLE_REL_RE.finditer(code):
            o = m.end() - 1
            c = match_close(code, o)
            model, method = m.group(2), m.group(3)
            self._n1(m.start(), o, c, f"{m.group(1)}.query.{model}.{method}()", "drizzle", model)
            if method == "findMany" and self.in_handler(m.start()):
                top = self._top_keys(o + 1, c)
                if "limit" not in top and not SMALL_TABLE_RE.match(model) and not re.search(
                        r"\binArray\s*\(", code[o:c]):
                    self.add("PGL014", "low",
                             f"{m.group(1)}.query.{model}.findMany() in a request handler has no limit: it returns "
                             f"every matching row.",
                             m.start(), heuristic=True, fixkeys=["drizzle"], model=model)
        for m in DRIZZLE_SELECT_RE.finditer(code):
            if m.group(1) in ("sql", "Promise", "document", "window", "this"):
                continue
            o = m.end() - 1
            end, methods = self.chain_end(o)
            if "from" not in methods:
                continue
            chain = code[m.start():end + 1]
            fm = re.search(r"\.\s*from\s*\(\s*([\w$.]+)", chain)
            table = fm.group(1).split(".")[-1] if fm else "table"
            self._n1(m.start(), o, end, f"{m.group(1)}.select().from({table})", "drizzle", table)
            if "offset" in methods:
                om = re.search(r"\.\s*offset\s*\(\s*([^)]*)\)", chain)
                if om and not re.fullmatch(r"\s*\d+\s*", om.group(1)):
                    self.add("PGL015", "info",
                             f"{m.group(1)}.select().from({table}) pages with .offset({om.group(1).strip()}): "
                             f"Postgres reads and discards every skipped row.",
                             m.start(), fixkeys=["drizzle"], model=table)
            if not self.in_handler(m.start()) or "limit" in methods or SMALL_TABLE_RE.match(table):
                continue
            sel_args = code[o + 1:match_close(code, o)]
            if re.search(r"\b(count|sum|avg|min|max)\s*\(", sel_args):
                continue
            if re.search(r"\beq\s*\(\s*[\w$.]+\.\s*(id|uuid|slug|email)\s*,|\binArray\s*\(", chain):
                continue
            before = code[max(0, m.start() - 60):m.start()]
            if re.search(r"\[\s*" + IDENT + r"\s*\]\s*=\s*(?:await\s+)?$", before) or \
                    re.match(r"\s*\)?\s*\[\s*0\s*\]", code[end + 1:end + 8]):
                continue
            if "returning" in methods:
                continue
            self.add("PGL014", "low",
                     f"{m.group(1)}.select().from({table}) in a request handler has no .limit(): it returns every "
                     f"matching row.",
                     m.start(), heuristic=True, fixkeys=["drizzle"], model=table)
        # PGL017 like(col, `%${q}%`)
        for m in re.finditer(r"(?<![\w$.])(i?like)\s*\(\s*([\w$.]+)\s*,", code):
            o = code.index("(", m.start())
            c = match_close(code, o)
            args = split_args(code, o + 1, c)
            if len(args) < 2:
                continue
            a, b = strip_span(code, *args[1])
            tok = self.tok_covering(a, b)
            if tok is None:
                continue
            if not self.tok_text(tok).startswith("%"):
                continue
            col = m.group(2).split(".")[-1]
            if self.proj.has_trgm(col):
                continue
            self.add("PGL017", "low",
                     f"{m.group(1)}({m.group(2)}, '%...') starts with a wildcard: without a trigram index every "
                     f"search scans the whole table.",
                     m.start(), heuristic=True, fixkeys=["sql"], col=col, table=m.group(2).split(".")[0],
                     index=f"{m.group(2).split('.')[0]}_{col}_trgm_idx")

    def _n1(self, start: int, o: int, c: int, label: str, key: str, target: str) -> None:
        lp = self.loop_at(start)
        if not lp:
            return
        a_, b_, iterable, names, lline, kind = lp
        seg = self.code[o:c + 1]
        used = [n for n in names if re.search(r"(?<![\w$.])" + re.escape(n) + r"\b", seg)]
        if not used:
            return
        km = re.search(r"(?<![\w$.])" + re.escape(used[0]) + r"\s*\.\s*(" + IDENT + r")", seg)
        k = km.group(1) if km else "id"
        self.add("PGL013", "medium",
                 f"{label} runs once per item of {iterable or 'the loop'} (loop at line {lline}): one query per row.",
                 start, heuristic=True, fixkeys=[key], target=target, key=k, model=target,
                 relation=re.sub(r"(Id|_id)$", "", k))

    # ------------------------------------------------------------ raw SQL strings
    def raw_sql_strings(self) -> None:
        code = self.code
        file_like_param = re.search(r"`%\$\{|['\"]%['\"]\s*\+", self.clean) is not None
        sql_calls = []
        for m in re.finditer(r"(?:\.\s*(?:query|execute|unsafe)\s*\(|(?<![\w$.])(?:sql|db\.sql)\s*`)", code):
            sql_calls.append(m)
        for tok in self.toks:
            text = self.tok_text(tok)
            if not looks_sql(text):
                continue
            off = tok.start
            if select_star(text):
                self.add("PGL016", "low", "SELECT * in application SQL: the query reads and returns every column, "
                                          "including ones this code never uses.", off, fixkeys=["default"])
            for col in like_columns(text):
                if not self.proj.has_trgm(col):
                    self.add("PGL017", "low",
                             f"{col} LIKE '%...' starts with a wildcard: without a trigram index every search scans "
                             f"the whole table.",
                             off, heuristic=True, fixkeys=["sql"], **trgm_extra(col, text))
            if file_like_param:
                for lm in LIKE_PARAM_RE.finditer(text):
                    col = lm.group(1)
                    if col.lower() in ("not",) or self.proj.has_trgm(col):
                        continue
                    self.add("PGL017", "low",
                             f"{col} LIKE {lm.group(2)} is matched against a value built as '%...%' in this file: "
                             f"without a trigram index every search scans the whole table.",
                             off, heuristic=True, fixkeys=["sql"], **trgm_extra(col, text))
            if OFFSET_PARAM_RE.search(text):
                self.add("PGL015", "info", "OFFSET with a request-dependent value: Postgres reads and discards every "
                                           "skipped row, so deep pages get slower.", off, fixkeys=["sql"])
            if self.in_handler(off) and unbounded_select(text) and not self._limited_by_caller(tok):
                self.add("PGL014", "low", "SELECT without LIMIT in a request handler: it returns every matching row, "
                                          "however large the table grows.", off, heuristic=True, fixkeys=["sql"])
            # N+1 for raw SQL inside loops
            if re.match(r"(?is)^\s*\(?\s*(select|with)\b", text):
                for m in sql_calls:
                    if m.start() <= off <= m.end() + 5 or (m.group(0).strip().endswith("(") and
                                                          m.end() <= off <= m.end() + 3):
                        if m.group(0).endswith("`"):
                            o = m.end() - 1
                            c = tok.end
                        else:
                            o = code.index("(", m.start())
                            c = match_close(code, o)
                        tables = re.findall(r"(?i)\bfrom\s+\"?(\w+)", text)
                        self._n1(m.start(), o, c, "This SQL query", "node-pg", tables[0] if tables else "related_table")
                        break

    def _limited_by_caller(self, tok: StrTok) -> bool:
        after = self.code[tok.end:tok.end + 40]
        return bool(re.match(r"\s*\+", after))
