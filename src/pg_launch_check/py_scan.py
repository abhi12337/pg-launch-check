"""Python application code: SQLAlchemy, Django ORM, psycopg / psycopg2, asyncpg. Uses the
standard `ast` module; the scanned code is parsed, never imported or run."""

from __future__ import annotations

import ast
import re

from .appcommon import (LIKE_PARAM_RE, OFFSET_PARAM_RE, SMALL_TABLE_RE, ProjectCtx, like_columns, looks_sql,
                        select_star, trgm_extra, unbounded_select)
from .findings import Finding, clip_line
from .sql_scan import is_money_name

HANDLER_DECO_LAST = {"get", "post", "put", "patch", "delete", "route", "api_route", "websocket", "api_view",
                     "require_POST", "require_GET", "require_http_methods", "csrf_exempt", "login_required"}
DJ_READ = {"get", "filter", "exclude", "first", "last", "count", "exists", "all", "values", "values_list",
           "aggregate", "in_bulk", "earliest", "latest", "get_or_none"}
DJ_LIST = {"all", "filter", "exclude", "order_by", "select_related", "prefetch_related", "annotate", "values",
           "values_list", "distinct", "only", "defer", "using"}
SA_READ_END = {"first", "one", "one_or_none", "all", "scalar", "scalar_one", "scalar_one_or_none", "count", "get"}
EAGER_LAZY = {"joined", "selectin", "subquery", "immediate", "raise", "raise_on_sql", "noload", "write_only",
              "dynamic"}
FLOAT_TYPES = {"Float", "REAL", "DOUBLE", "DOUBLE_PRECISION", "Double", "FLOAT"}
DT_TYPES = {"DateTime", "TIMESTAMP"}
SAFE_FUNCS = {"int", "float", "len", "bool", "abs", "round"}
NON_PG_IMPORT_RE = re.compile(r"^\s*(?:import|from)\s+(sqlite3|aiosqlite|pymysql|MySQLdb|mysql\.connector|aiomysql|"
                              r"pyodbc|cx_Oracle|oracledb|pymssql)\b", re.M)
PG_IMPORT_RE = re.compile(r"^\s*(?:import|from)\s+(psycopg2?|psycopg_pool|asyncpg|sqlalchemy|django)\b", re.M)


def flat(node: ast.AST) -> tuple[list[str], list[ast.Call]]:
    """a.b(x).c(y) -> (['a', 'b', 'c'], [call c, call b])."""
    names, calls = [], []
    while True:
        if isinstance(node, ast.Await):
            node = node.value
        elif isinstance(node, ast.Call):
            calls.append(node)
            node = node.func
        elif isinstance(node, ast.Attribute):
            names.append(node.attr)
            node = node.value
        elif isinstance(node, ast.Name):
            names.append(node.id)
            break
        else:
            names.append("?")
            break
    return names[::-1], calls


def _kw(call: ast.Call, name: str) -> ast.AST | None:
    for k in call.keywords:
        if k.arg == name:
            return k.value
    return None


def _has_kw(call: ast.Call, *names: str) -> bool:
    return any(k.arg in names for k in call.keywords) or any(k.arg is None for k in call.keywords)


def _const_str(node) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _type_name(node) -> str:
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


# ---------------------------------------------------------------- model collection (first pass)
def collect_models(rel: str, tree: ast.AST, text: str, proj: ProjectCtx) -> list[Finding]:
    findings: list[Finding] = []
    lines = text.splitlines()
    is_django = "django" in text
    is_sqla = "sqlalchemy" in text or "flask_sqlalchemy" in text

    def ev(n: int) -> str:
        return clip_line(lines[n - 1]) if 0 < n <= len(lines) else ""

    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        bases = " ".join(ast.unparse(b) for b in cls.bases)
        body_src = [s for s in cls.body if isinstance(s, (ast.Assign, ast.AnnAssign))]
        tablename = any(isinstance(s, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__tablename__"
                                                        for t in s.targets) for s in body_src)
        dj = is_django and re.search(r"\bmodels\.Model\b|\bModel\b", bases) is not None and not tablename
        sa = is_sqla and (tablename or re.search(r"\b(Base|DeclarativeBase|db\.Model)\b", bases) is not None)
        if not (dj or sa):
            continue
        ts_cols = []
        # indexes declared on the class: Django Meta.indexes, SQLAlchemy __table_args__
        idx_lists: list[list[str]] = []
        for n in ast.walk(cls):
            if isinstance(n, ast.Call) and _type_name(n) in ("Index", "GinIndex", "GistIndex", "UniqueConstraint",
                                                              "PrimaryKeyConstraint", "BTreeIndex", "HashIndex"):
                cols = [(_const_str(a) or (a.attr if isinstance(a, ast.Attribute) else
                                           a.id if isinstance(a, ast.Name) else "")) for a in n.args]
                fields_kw = _kw(n, "fields")
                if isinstance(fields_kw, (ast.List, ast.Tuple)):
                    cols = [_const_str(e) or "" for e in fields_kw.elts]
                elif _type_name(n) == "Index" and cols and not _kw(n, "fields"):
                    cols = cols[1:] if _const_str(n.args[0]) and len(n.args) > 1 else cols
                idx_lists.append([c.lstrip("-") for c in cols if c])
                if "trgm_ops" in ast.unparse(n):
                    for c in cols:
                        proj.trgm_cols.add(c.lstrip("-").lower().replace("_", ""))
        sa_fk_cols = []
        for s in body_src:
            target = s.targets[0] if isinstance(s, ast.Assign) else s.target
            if not isinstance(target, ast.Name):
                continue
            fname = target.id
            val = s.value
            ann = ast.unparse(s.annotation) if isinstance(s, ast.AnnAssign) else ""
            if dj and isinstance(val, ast.Call):
                tname = _type_name(val)
                if tname in ("ForeignKey", "OneToOneField", "ManyToManyField", "ParentalKey"):
                    tgt = val.args[0] if val.args else _kw(val, "to")
                    tgt_name = (_const_str(tgt) or (ast.unparse(tgt) if tgt is not None else "")).split(".")[-1]
                    if tgt_name == "self":
                        tgt_name = cls.name
                    rn = _const_str(_kw(val, "related_name"))
                    m = proj.django_models.setdefault(cls.name, {"fk": set(), "rev": set()})
                    t = proj.django_models.setdefault(tgt_name, {"fk": set(), "rev": set()})
                    if tname == "ManyToManyField":
                        m["rev"].add(fname)
                        if rn != "+":
                            t["rev"].add(rn or f"{cls.name.lower()}_set")
                    elif tname == "OneToOneField":
                        m["fk"].add(fname)
                        if rn != "+":
                            t["fk"].add(rn or cls.name.lower())
                    else:
                        m["fk"].add(fname)
                        if rn != "+":
                            t["rev"].add(rn or f"{cls.name.lower()}_set")
                else:
                    proj.django_models.setdefault(cls.name, {"fk": set(), "rev": set()})
                if tname == "FloatField" and is_money_name(fname) and proj.postgres_py:
                    findings.append(Finding(
                        "PGL009", "medium",
                        f"{cls.name}.{fname} looks like money but is a FloatField (double precision): amounts and "
                        f"sums will be off by fractions of a cent.",
                        rel, s.lineno, ev(s.lineno), True, extra={"fixkeys": ["django"], "field": fname}))
                continue
            if not sa:
                continue
            call = val if isinstance(val, ast.Call) else None
            cname = _type_name(call) if call is not None else ""
            if val is None and ann.startswith("Mapped["):
                call = ast.Call(func=ast.Name("mapped_column"), args=[], keywords=[])   # bare annotation
                cname = "mapped_column"
            if cname == "relationship":
                lazy = _const_str(_kw(call, "lazy"))
                tgt = call.args[0] if call.args else None
                tgt_name = (_const_str(tgt) or (ast.unparse(tgt) if tgt is not None else "")).split(".")[-1]
                if not tgt_name and "Mapped[" in ann:
                    mm = re.search(r"Mapped\[(?:list\[|List\[)?['\"]?(\w+)", ann)
                    tgt_name = mm.group(1) if mm else ""
                rels = proj.sqla_models.setdefault(cls.name, {"rels": {}})["rels"]
                if lazy not in EAGER_LAZY:
                    rels[fname] = tgt_name or "Related"
                continue
            proj.sqla_models.setdefault(cls.name, {"rels": {}})
            if cname not in ("Column", "mapped_column"):
                continue
            if any(_type_name(a) == "ForeignKey" for a in call.args):
                flags = {k.arg: k.value for k in call.keywords}
                if not any(isinstance(flags.get(k), ast.Constant) and flags[k].value is True
                           for k in ("index", "primary_key", "unique")):
                    sa_fk_cols.append((fname, s.lineno))
            col_type = None
            for a in call.args:
                tn = _type_name(a)
                if tn and tn not in ("ForeignKey", "Identity", "Sequence", "Computed"):
                    col_type = a
                    break
            tn = _type_name(col_type) if col_type is not None else ""
            if not tn and "Mapped[" in ann:
                inner = re.search(r"Mapped\[(?:Optional\[)?(\w+)", ann)
                tn = {"float": "Float", "datetime": "DateTime"}.get(inner.group(1) if inner else "", "")
                col_type = None
            if not proj.postgres_py:
                continue
            if tn in FLOAT_TYPES and is_money_name(fname):
                findings.append(Finding(
                    "PGL009", "medium",
                    f"{cls.name}.{fname} looks like money but is mapped as {tn} (floating point): amounts and sums "
                    f"will be off by fractions of a cent.",
                    rel, s.lineno, ev(s.lineno), True, extra={"fixkeys": ["sqlalchemy"], "field": fname}))
            if tn in DT_TYPES:
                tz = _kw(col_type, "timezone") if isinstance(col_type, ast.Call) else None
                if not (isinstance(tz, ast.Constant) and tz.value is True):
                    ts_cols.append((fname, s.lineno))
        for fname, ln in sa_fk_cols:
            if not proj.postgres_py or any(lst[:1] == [fname] for lst in idx_lists):
                continue
            findings.append(Finding(
                "PGL001", "medium",
                f"{cls.name}.{fname} is a ForeignKey column without index=True (and no Index() in __table_args__ "
                f"starts with it): SQLAlchemy does not index foreign keys, so deletes of the parent row and joins on "
                f"{fname} scan the table.",
                rel, ln, ev(ln), extra={"fixkeys": ["sqlalchemy"], "field": fname}))
        if ts_cols and proj.postgres_py:
            names = ", ".join(n for n, _ in ts_cols)
            findings.append(Finding(
                "PGL010", "info",
                f"{cls.name}: {names} {'is' if len(ts_cols) == 1 else 'are'} DateTime without timezone=True, which "
                f"SQLAlchemy creates as timestamp without time zone.",
                rel, ts_cols[0][1], ev(ts_cols[0][1]), extra={"fixkeys": ["sqlalchemy"], "field": ts_cols[0][0]}))
    return findings


# ---------------------------------------------------------------- per-file scan
class PyScan:
    def __init__(self, rel: str, text: str, tree: ast.AST, proj: ProjectCtx):
        self.rel = rel
        self.text = text
        self.tree = tree
        self.proj = proj
        self.lines = text.splitlines()
        self.findings: list[Finding] = []
        self._seen: set = set()
        self.parent: dict = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                self.parent[child] = node
        self.module_consts: dict[str, ast.AST] = {}
        for s in tree.body if isinstance(tree, ast.Module) else []:
            if isinstance(s, ast.Assign) and len(s.targets) == 1 and isinstance(s.targets[0], ast.Name):
                self.module_consts[s.targets[0].id] = s.value
        self.imports_sa_text = re.search(r"from\s+sqlalchemy(?:\.\w+)*\s+import\s+[^\n]*\btext\b", text) is not None
        self.is_django = "django" in text

    def add(self, rule: str, sev: str, msg: str, line: int, heuristic: bool = False, **extra) -> None:
        if (rule, line, msg[:40]) in self._seen:
            return
        self._seen.add((rule, line, msg[:40]))
        raw = self.lines[line - 1] if 0 < line <= len(self.lines) else ""
        self.findings.append(Finding(rule, sev, msg, self.rel, line, clip_line(raw), heuristic,
                                     extra={k: v for k, v in extra.items() if v is not None}))

    def ancestors(self, node):
        while node in self.parent:
            node = self.parent[node]
            yield node

    def func_of(self, node):
        for a in self.ancestors(node):
            if isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return a
        return None

    def is_handler(self, func) -> bool:
        while func is not None:
            for d in func.decorator_list:
                target = d.func if isinstance(d, ast.Call) else d
                names, _ = flat(target)
                if names and names[-1] in HANDLER_DECO_LAST and (len(names) > 1 or names[-1] in (
                        "api_view", "require_POST", "require_GET", "require_http_methods", "csrf_exempt",
                        "login_required")):
                    return True
            args = [a.arg for a in func.args.args]
            if args[:1] == ["request"] or (args[:1] in (["self"], ["cls"]) and args[1:2] == ["request"]):
                return True
            func = self.func_of(func)
        return False

    def outermost(self, call: ast.Call) -> bool:
        p = self.parent.get(call)
        return not (isinstance(p, ast.Attribute) and isinstance(self.parent.get(p), ast.Call))

    # ------------------------------------------------------------ string expressions
    def assigned(self, name: str, at: ast.AST) -> list[ast.AST]:
        """Values assigned to `name` in the enclosing function (or module) before `at`."""
        scope = self.func_of(at) or self.tree
        out = []
        for n in ast.walk(scope):
            if getattr(n, "lineno", 10 ** 9) > getattr(at, "lineno", 0):
                continue
            if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in n.targets):
                out.append(n.value)
            elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == name and n.value:
                out.append(n.value)
            elif isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name) and n.target.id == name:
                out.append(n.value)
        if not out and name in self.module_consts:
            out.append(self.module_consts[name])
        return out

    def safe(self, node: ast.AST, depth: int = 0) -> bool:
        if isinstance(node, ast.Constant):
            return True
        if isinstance(node, ast.Name):
            if re.fullmatch(r"[A-Z][A-Z0-9_]*", node.id):
                return True
            if depth < 2:
                vals = self.assigned(node.id, node)
                return bool(vals) and all(self.safe(v, depth + 1) for v in vals)
            return False
        if isinstance(node, ast.Call):
            names, _ = flat(node.func)
            if names and names[-1] in SAFE_FUNCS and len(names) == 1:
                return True
            if names and names[-1] == "join" and isinstance(node.func, ast.Attribute) and \
                    isinstance(node.func.value, ast.Constant):
                return all(self.safe(a, depth + 1) for a in node.args)
            return False
        if isinstance(node, (ast.List, ast.Tuple)):
            return all(self.safe(e, depth + 1) for e in node.elts)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
            return self.safe(node.left, depth + 1) or self.safe(node.right, depth + 1)
        if isinstance(node, (ast.ListComp, ast.GeneratorExp)):
            return self.safe(node.elt, depth + 1) if not isinstance(node.elt, ast.Name) else False
        if isinstance(node, ast.JoinedStr):
            return all(self.safe(v.value, depth + 1) for v in node.values if isinstance(v, ast.FormattedValue))
        if isinstance(node, ast.Attribute):
            return node.attr in ("__tablename__",)
        return False

    def str_info(self, node: ast.AST, depth: int = 0):
        """(static text with {} markers, [unsafe expression sources]) for a string-building expression."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value, []
        if isinstance(node, ast.JoinedStr):
            text, unsafe = "", []
            for v in node.values:
                if isinstance(v, ast.Constant):
                    text += str(v.value)
                elif isinstance(v, ast.FormattedValue):
                    text += "{}"
                    if not self.safe(v.value):
                        unsafe.append(ast.unparse(v.value))
            return text, unsafe
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            left = self.str_info(node.left, depth)
            if left is None:
                return None
            specs = re.findall(r"%(?:\(\w+\))?[-#0 +]*\d*(?:\.\d+)?([a-zA-Z%])", left[0])
            if specs and all(s in "dif%" for s in specs):
                return left[0], left[1]
            unsafe = left[1] + ([] if self.safe(node.right) else [ast.unparse(node.right)])
            return left[0], unsafe
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = self.str_info(node.left, depth)
            right = self.str_info(node.right, depth)
            if left is None and right is None:
                return None
            text, unsafe = "", []
            for side, info in ((node.left, left), (node.right, right)):
                if info is None:
                    text += "{}"
                    if not self.safe(side):
                        unsafe.append(ast.unparse(side))
                else:
                    text += info[0]
                    unsafe += info[1]
            return text, unsafe
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
            base = self.str_info(node.func.value, depth)
            if base is None:
                return None
            unsafe = base[1] + [ast.unparse(a) for a in list(node.args) + [k.value for k in node.keywords]
                                if not self.safe(a)]
            return base[0], unsafe
        if isinstance(node, ast.Name) and depth < 2:
            vals = self.assigned(node.id, node)
            if not vals:
                return None
            text, unsafe, any_str = "", [], False
            for v in vals:
                info = self.str_info(v, depth + 1)
                if info is None:
                    continue
                any_str = True
                text += info[0]
                unsafe += info[1]
            return (text, unsafe) if any_str else None
        return None

    # ------------------------------------------------------------ run
    def run(self) -> list[Finding]:
        if NON_PG_IMPORT_RE.search(self.text) and not PG_IMPORT_RE.search(self.text):
            return []          # sqlite / MySQL code: out of scope
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                self.call(node)
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                self.lazy_loop(node)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if not isinstance(self.parent.get(node), (ast.Expr, ast.JoinedStr)):
                    self.sql_text(node.value, node)
            elif isinstance(node, ast.JoinedStr):
                info = self.str_info(node)
                if info:
                    self.sql_text(info[0], node)
            elif isinstance(node, ast.Subscript):
                self.django_slice(node)
        return self.findings

    # ------------------------------------------------------------ calls
    def call(self, node: ast.Call) -> None:
        names, calls = flat(node.func)
        method = names[-1] if names else ""
        self.injection(node, names, method)
        self.connections(node, names, method)
        if self.outermost(node):
            self.n_plus_one(node)
            self.unbounded(node)
        self.like(node, names, method)
        if method == "offset" and node.args and not isinstance(node.args[0], ast.Constant):
            self.add("PGL015", "info",
                     f".offset({ast.unparse(node.args[0])}) pages with OFFSET: Postgres reads and discards every "
                     f"skipped row, so deep pages get slower.", node.lineno, fixkeys=["sqlalchemy"])

    def injection(self, node: ast.Call, names: list[str], method: str) -> None:
        key = None
        if method in ("execute", "executemany", "exec_driver_sql", "fetch", "fetchrow", "fetchval", "copy_expert"):
            key = "asyncpg" if method.startswith("fetch") else ("sqlalchemy" if method == "exec_driver_sql" else
                                                                 "django" if self.is_django else "psycopg")
        elif method == "raw" and "objects" in names:
            key = "django"
        elif method == "text" and (names in (["text"], ["sa", "text"], ["sqlalchemy", "text"]) and
                                   (self.imports_sa_text or names[0] in ("sa", "sqlalchemy"))):
            key = "sqlalchemy"
        elif method == "RawSQL":
            key = "django"
        elif names[-2:] == ["sql", "SQL"] or names == ["SQL"]:
            key = "psycopg"
        if key is None or not node.args:
            return
        arg = node.args[0]
        if isinstance(arg, ast.Call) and _type_name(arg) == "text":
            return   # the inner text() call is checked on its own
        info = self.str_info(arg)
        if info is None or not info[1]:
            return
        text, unsafe = info
        if not looks_sql(text) and method not in ("RawSQL",):
            return
        shown = ", ".join(sorted(set(unsafe))[:3])
        self.add("PGL011", "high",
                 f"{'.'.join(names[-2:])}() receives SQL built from {shown} with an f-string, + or % / .format(): "
                 f"a value containing a quote changes the query (SQL injection). Pass it as a parameter.",
                 node.lineno, fixkeys=[key])

    def connections(self, node: ast.Call, names: list[str], method: str) -> None:
        dotted = ".".join(names)
        lib = None
        sev = "high"
        if method in ("create_engine", "create_async_engine"):
            lib = "sqlalchemy"
        elif dotted in ("psycopg.connect", "psycopg2.connect", "psycopg.AsyncConnection.connect",
                        "psycopg.Connection.connect"):
            lib, sev = "psycopg", "medium"
        elif dotted == "asyncpg.connect":
            lib, sev = "asyncpg", "medium"
        elif dotted == "asyncpg.create_pool":
            lib = "asyncpg"
        elif method in ("ConnectionPool", "AsyncConnectionPool", "SimpleConnectionPool", "ThreadedConnectionPool"):
            lib = "psycopg"
        if lib is None:
            return
        func = self.func_of(node)
        label = dotted + "()"
        if func is not None and self.is_handler(func):
            self.add("PGL012", sev, f"{label} is called inside a request handler: it opens new database "
                                    f"connections on every request instead of reusing a pool.",
                     node.lineno, fixkeys=[lib])
            return
        if sev == "medium":
            return
        if method in ("create_engine", "create_async_engine"):
            url = node.args[0] if node.args else _kw(node, "url")
            s = _const_str(url)
            pg = s.startswith(("postgres", "postgresql")) if s else self.proj.postgres_py
            if not pg:
                return
            if not _has_kw(node, "pool_size", "poolclass"):
                self.add("PGL018", "low", f"{label} uses SQLAlchemy's default pool (pool_size=5 plus max_overflow=10 "
                                          f"per process): set it so processes x pool size stays below max_connections.",
                         node.lineno, fixkeys=["sqlalchemy"])
            ca = _kw(node, "connect_args")
            if not (ca is not None and re.search(r"statement_timeout|options|\*\*", ast.unparse(ca))) and \
                    not self._timeout_elsewhere():
                self.add("PGL019", "low", f"{label} sets no statement_timeout: a runaway query keeps running and "
                                          f"holds the connection long after the request gave up.",
                         node.lineno, fixkeys=["sqlalchemy", "default"])
            return
        if dotted == "asyncpg.create_pool":
            if not _has_kw(node, "max_size"):
                self.add("PGL018", "low", f"{label} uses asyncpg's default pool size (10 connections per process).",
                         node.lineno, fixkeys=["asyncpg"])
            if not _has_kw(node, "command_timeout") and not re.search(
                    r"statement_timeout", ast.unparse(node)) and not self._timeout_elsewhere():
                self.add("PGL019", "low", f"{label} sets no statement_timeout or command_timeout: a runaway query "
                                          f"keeps running and holds the connection.",
                         node.lineno, fixkeys=["asyncpg", "default"])
            return
        if method in ("ConnectionPool", "AsyncConnectionPool"):
            if not _has_kw(node, "max_size") and len(node.args) < 3:
                self.add("PGL018", "low", f"{label} sets no max_size (psycopg_pool then keeps min_size, 4 by "
                                          f"default, as the maximum).", node.lineno, fixkeys=["psycopg"])
            if not re.search(r"statement_timeout", ast.unparse(node)) and not self._timeout_elsewhere():
                self.add("PGL019", "low", f"{label} sets no statement_timeout for its connections.",
                         node.lineno, fixkeys=["psycopg", "default"])

    def _timeout_elsewhere(self) -> bool:
        return "statement_timeout" in self.text or self.proj.role_statement_timeout

    # ------------------------------------------------------------ N+1
    def loop_of(self, node):
        """(loop node, target names) for the innermost loop whose body contains node."""
        child = node
        for a in self.ancestors(node):
            if isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                return None
            if isinstance(a, ast.While):
                return None
            if isinstance(a, (ast.For, ast.AsyncFor)) and child is not a.iter and child is not a.target:
                return a, {n.id for n in ast.walk(a.target) if isinstance(n, ast.Name)}
            if isinstance(a, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)) and \
                    not any(child is g for g in a.generators):
                return a, {n.id for g in a.generators for n in ast.walk(g.target) if isinstance(n, ast.Name)}
            child = a
        return None

    def db_read(self, node: ast.Call):
        """(label, fix key, target name) when node is a database read, else None."""
        names, calls = flat(node)
        if not names:
            return None
        method = names[-1]
        if "objects" in names and method in DJ_READ:
            return f"{'.'.join(names[:names.index('objects') + 1])}.{method}()", "django", names[0]
        if names[0] in ("session", "db", "s", "sess", "dbsession", "db_session") or "session" in names[:-1]:
            if method == "get" and node.args and isinstance(node.args[0], ast.Name) and node.args[0].id[:1].isupper():
                return f"{'.'.join(names)}()", "sqlalchemy", node.args[0].id
            if "query" in names and method in SA_READ_END:
                q = next((c for c in calls if isinstance(c.func, ast.Attribute) and c.func.attr == "query"), None)
                tgt = q.args[0].id if q is not None and q.args and isinstance(q.args[0], ast.Name) else "Related"
                return f"{'.'.join(names)}()", "sqlalchemy", tgt
            for c in calls:
                if isinstance(c.func, ast.Attribute) and c.func.attr in ("execute", "scalars", "scalar") and c.args \
                        and isinstance(c.args[0], ast.Call) and flat(c.args[0])[0][:1] == ["select"]:
                    sel = flat(c.args[0])[1][-1]
                    tgt = sel.args[0].id if sel.args and isinstance(sel.args[0], ast.Name) else "Related"
                    return f"{names[0]}.{c.func.attr}(select({tgt}))", "sqlalchemy", tgt
        if len(names) >= 3 and names[1] == "query" and names[0][:1].isupper() and method in SA_READ_END | {
                "filter_by", "filter"}:
            return f"{'.'.join(names)}()", "sqlalchemy", names[0]
        for c in calls:
            if isinstance(c.func, ast.Attribute) and c.func.attr in ("execute", "fetch", "fetchrow", "fetchval") \
                    and c.args:
                info = self.str_info(c.args[0])
                if info and re.match(r"(?is)^\s*\(?\s*(select|with)\b", info[0]):
                    key = "asyncpg" if c.func.attr.startswith("fetch") else "psycopg"
                    tables = re.findall(r"(?i)\bfrom\s+\"?(\w+)", info[0])
                    return f"{ast.unparse(c.func)}()", key, tables[0] if tables else "related_table"
        return None

    def n_plus_one(self, node: ast.Call) -> None:
        lp = self.loop_of(node)
        if lp is None:
            return
        loop, targets = lp
        r = self.db_read(node)
        if r is None:
            return
        used = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} & targets
        if not used:
            return
        label, key, target = r
        it = ast.unparse(loop.iter) if isinstance(loop, (ast.For, ast.AsyncFor)) else \
            ast.unparse(loop.generators[0].iter)
        if len(it) > 60:
            it = it[:57] + "..."
        attr = None
        for n in ast.walk(node):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in used:
                attr = n.attr
                break
        self.add("PGL013", "medium",
                 f"{label} runs once per item of {it} (loop at line {loop.lineno}): one query per row.",
                 node.lineno, heuristic=True, fixkeys=[key], target=target, model=self._model_of(it) or "Model",
                 relation=re.sub(r"_id$", "", attr) if attr else target.lower(), key=attr or "id")

    def _model_of(self, it: str) -> str | None:
        m = re.match(r"(\w+)\.objects\b", it) or re.search(r"query\((\w+)\)", it) or re.search(r"select\((\w+)\)", it)
        return m.group(1) if m else None

    def _queryset_source(self, it: ast.AST, at: ast.AST) -> ast.AST | None:
        if isinstance(it, ast.Name):
            vals = self.assigned(it.id, at)
            it = vals[-1] if vals else None
        while isinstance(it, ast.Subscript):      # qs[:20]
            it = it.value
        return it

    def lazy_loop(self, loop) -> None:
        src = self._queryset_source(loop.iter, loop)
        if not isinstance(src, (ast.Call, ast.Await)):
            return
        if any(isinstance(n, ast.Await) for n in ast.walk(src)):
            return
        names, calls = flat(src)
        targets = {n.id for n in ast.walk(loop.target) if isinstance(n, ast.Name)}
        if len(targets) != 1:
            return
        var = next(iter(targets))
        # Django
        if "objects" in names and names[0] in self.proj.django_models:
            model = self.proj.django_models[names[0]]
            sel, pre, sel_all = set(), set(), False
            for c in calls:
                if isinstance(c.func, ast.Attribute) and c.func.attr in ("select_related", "prefetch_related"):
                    if c.func.attr == "select_related" and not c.args:
                        sel_all = True
                    for a in c.args:
                        s = _const_str(a) or (_const_str(getattr(a, "args", [None])[0]) if isinstance(a, ast.Call)
                                              and a.args else None)
                        if s:
                            (sel if c.func.attr == "select_related" else pre).add(s.split("__")[0])
            for n in self._body_nodes(loop):
                if not (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == var):
                    continue
                if n.attr in model["fk"] and n.attr not in sel and n.attr not in pre and not sel_all:
                    self.add("PGL013", "medium",
                             f"{var}.{n.attr} is a ForeignKey read inside a loop over {names[0]}.objects... without "
                             f"select_related('{n.attr}'): Django runs one query per row to load it.",
                             n.lineno, heuristic=True, fixkeys=["django-fk"], model=names[0], relation=n.attr,
                             target=n.attr.capitalize())
                elif n.attr in model["rev"] and n.attr not in pre:
                    p = self.parent.get(n)
                    if isinstance(p, ast.Attribute) and isinstance(self.parent.get(p), ast.Call):
                        self.add("PGL013", "medium",
                                 f"{var}.{n.attr}.{p.attr}() runs a query per row of {names[0]}.objects... (no "
                                 f"prefetch_related('{n.attr}')).",
                                 n.lineno, heuristic=True, fixkeys=["django-rev"], model=names[0], relation=n.attr,
                                 target=n.attr)
            return
        # SQLAlchemy lazy relationships
        if "options" in names:
            return
        model_name = None
        for c in calls:
            fn = c.func.attr if isinstance(c.func, ast.Attribute) else (c.func.id if isinstance(c.func, ast.Name)
                                                                        else "")
            if fn == "query" and c.args and isinstance(c.args[0], ast.Name):
                model_name = c.args[0].id
            if fn in ("scalars", "execute") and c.args and isinstance(c.args[0], ast.Call):
                inner_names, inner_calls = flat(c.args[0])
                if "options" in inner_names:
                    return
                sel = inner_calls[-1] if inner_calls else None
                if inner_names[:1] == ["select"] and sel is not None and sel.args and isinstance(sel.args[0], ast.Name):
                    model_name = sel.args[0].id
        if model_name is None and len(names) >= 2 and names[1] == "query" and names[0][:1].isupper():
            model_name = names[0]
        if model_name not in self.proj.sqla_models:
            return
        rels = self.proj.sqla_models[model_name]["rels"]
        for n in self._body_nodes(loop):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == var and n.attr in rels:
                self.add("PGL013", "medium",
                         f"{var}.{n.attr} is a lazy-loaded relationship read inside a loop over {model_name} rows: "
                         f"SQLAlchemy runs one SELECT per row to load it.",
                         n.lineno, heuristic=True, fixkeys=["sqlalchemy"], model=model_name, relation=n.attr,
                         target=rels[n.attr])

    @staticmethod
    def _body_nodes(loop):
        for stmt in loop.body:
            yield from ast.walk(stmt)

    # ------------------------------------------------------------ unbounded / offset
    def unbounded(self, node: ast.Call) -> None:
        func = self.func_of(node)
        if func is None or not self.is_handler(func):
            return
        names, calls = flat(node)
        method = names[-1] if names else ""
        fsrc = ast.unparse(func)
        if re.search(r"Paginator|paginat", fsrc):
            return
        if "objects" in names and method in DJ_LIST and names[0][:1].isupper():
            if SMALL_TABLE_RE.match(names[0]) or isinstance(self.parent.get(node), ast.Subscript):
                return
            if any(k.arg and k.arg.split("__")[0] in ("pk", "id", "uuid", "slug", "email") and "__" not in k.arg
                   for c in calls for k in c.keywords):
                return
            p = self.parent.get(node)
            if isinstance(p, ast.Assign) and len(p.targets) == 1 and isinstance(p.targets[0], ast.Name):
                v = p.targets[0].id
                for n in ast.walk(func):
                    if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name) and n.value.id == v:
                        return
                    if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == v and \
                            n.attr in ("first", "last", "get", "count", "exists", "aggregate", "update", "delete",
                                       "earliest", "latest"):
                        return
            self.add("PGL014", "low",
                     f"{'.'.join(names)}() in a request handler has no slice or pagination: it returns every "
                     f"matching row, however large the table grows.",
                     node.lineno, heuristic=True, fixkeys=["django"], model=names[0])
            return
        if method == "all" and ("query" in names or "scalars" in names or "execute" in names):
            for n in ast.walk(node):
                if isinstance(n, ast.Attribute) and n.attr in ("limit", "slice", "paginate", "in_", "get"):
                    return
            tgt = self._model_of(ast.unparse(node)) or "Model"
            if SMALL_TABLE_RE.match(tgt):
                return
            self.add("PGL014", "low",
                     f"{ast.unparse(node)[:80]} in a request handler has no .limit(): it returns every matching row.",
                     node.lineno, heuristic=True, fixkeys=["sqlalchemy"], model=tgt)

    def django_slice(self, node: ast.Subscript) -> None:
        if not isinstance(node.slice, ast.Slice) or node.slice.lower is None or \
                isinstance(node.slice.lower, ast.Constant):
            return
        src = node.value
        if isinstance(src, ast.Name):
            vals = self.assigned(src.id, node)
            src = vals[-1] if vals else None
        if src is None:
            return
        names, _ = flat(src)
        if "objects" not in names:
            return
        self.add("PGL015", "info",
                 f"Slicing a queryset with [{ast.unparse(node.slice)}] pages with OFFSET: Postgres reads and "
                 f"discards every skipped row, so deep pages get slower.",
                 node.lineno, fixkeys=["django"], model=names[0])

    # ------------------------------------------------------------ LIKE
    def like(self, node: ast.Call, names: list[str], method: str) -> None:
        django_q = "objects" in names or method in ("Q", "filter", "exclude", "get")
        for k in node.keywords:
            if django_q and k.arg and re.search(r"__i?(contains|endswith)$", k.arg) and "__" in k.arg:
                if isinstance(k.value, (ast.List, ast.Dict, ast.Set, ast.Tuple)):
                    continue   # ArrayField / JSONField containment (@>), not LIKE
                parts = k.arg.split("__")
                col = parts[-2]
                if self.proj.has_trgm(col):
                    continue
                self.add("PGL017", "low",
                         f"{k.arg} becomes LIKE/ILIKE '%...%': without a trigram index on {col} every search scans "
                         f"the whole table.",
                         node.lineno, heuristic=True, fixkeys=["sql"], col=col, index=f"{col}_trgm_idx")
        if isinstance(node.func, ast.Attribute) and method in ("contains", "icontains", "endswith", "iendswith",
                                                               "like", "ilike"):
            recv = node.func.value
            if not (isinstance(recv, ast.Attribute) and isinstance(recv.value, ast.Name) and
                    recv.value.id[:1].isupper()):
                return
            col = recv.attr
            model = recv.value.id
            if model in self.proj.sqla_models and col in self.proj.sqla_models[model]["rels"]:
                return
            if model not in self.proj.sqla_models and "sqlalchemy" not in self.text:
                return
            if method in ("like", "ilike"):
                if not node.args:
                    return
                info = self.str_info(node.args[0])
                if info is None or not info[0].startswith("%"):
                    return
            if self.proj.has_trgm(col):
                return
            self.add("PGL017", "low",
                     f"{model}.{col}.{method}(...) matches with a leading wildcard: without a trigram index every "
                     f"search scans the whole table.",
                     node.lineno, heuristic=True, fixkeys=["sql"], col=col, index=f"{col}_trgm_idx",
                     table=model.lower())

    # ------------------------------------------------------------ SQL text
    def sql_text(self, text: str, node: ast.AST) -> None:
        if not looks_sql(text):
            return
        line = node.lineno
        if select_star(text):
            self.add("PGL016", "low", "SELECT * in application SQL: the query reads and returns every column, "
                                      "including ones this code never uses.", line, fixkeys=["default"])
        for col in like_columns(text):
            if not self.proj.has_trgm(col):
                self.add("PGL017", "low",
                         f"{col} LIKE '%...' starts with a wildcard: without a trigram index every search scans the "
                         f"whole table.", line, heuristic=True, fixkeys=["sql"], **trgm_extra(col, text))
        if re.search(r"""f["']%\{|["']%["']\s*\+|%\{\w+\}%""", self.text):
            for lm in LIKE_PARAM_RE.finditer(text):
                col = lm.group(1)
                if not self.proj.has_trgm(col):
                    self.add("PGL017", "low",
                             f"{col} LIKE {lm.group(2)} is matched against a value built as '%...%' in this file: "
                             f"without a trigram index every search scans the whole table.",
                             line, heuristic=True, fixkeys=["sql"], **trgm_extra(col, text))
        if OFFSET_PARAM_RE.search(text):
            self.add("PGL015", "info", "OFFSET with a request-dependent value: Postgres reads and discards every "
                                       "skipped row, so deep pages get slower.", line, fixkeys=["sql"])
        func = self.func_of(node)
        if func is not None and self.is_handler(func) and unbounded_select(text):
            self.add("PGL014", "low", "SELECT without LIMIT in a request handler: it returns every matching row, "
                                      "however large the table grows.", line, heuristic=True, fixkeys=["sql"])
