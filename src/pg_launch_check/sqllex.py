"""A small PostgreSQL tokenizer and statement splitter.

It understands what is needed to split migrations correctly and read DDL: `--` and nested
`/* */` comments, 'strings' (including E'..' escapes and '' doubling), "quoted identifiers",
$tag$ dollar-quoted bodies $tag$, $1 parameters and `::` casts. It is not a full SQL parser:
the DDL reader in schema.py works on these tokens."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

WORD_RE = re.compile(r"[A-Za-z_\x80-￿][A-Za-z0-9_$\x80-￿]*")
NUM_RE = re.compile(r"\d+(?:\.\d*)?(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?")
DOLLAR_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


@dataclass
class Tok:
    kind: str          # word | qident | str | dollar | num | param | op
    val: str           # word: lower-cased; qident: unquoted text; str/dollar: content; op: the operator
    line: int
    start: int         # offsets in the original text
    end: int


@dataclass
class Stmt:
    toks: list[Tok]
    line: int
    start: int
    end: int
    text: str = ""                 # original text of the statement (comments included)
    from_do: bool = False          # extracted from a DO $$ ... $$ block

    def words(self, n: int = 6) -> list[str]:
        return [t.val if t.kind == "word" else "" for t in self.toks[:n]]


@dataclass
class Lexed:
    stmts: list[Stmt] = field(default_factory=list)
    comments: list[tuple[int, str]] = field(default_factory=list)   # (line, text)


def tokenize(text: str, base_line: int = 1, base_off: int = 0) -> tuple[list[Tok], list[tuple[int, str]]]:
    toks: list[Tok] = []
    comments: list[tuple[int, str]] = []
    i, n, line = 0, len(text), base_line
    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
            i += 1
            continue
        if c in " \t\r\f\v":
            i += 1
            continue
        if text.startswith("--", i):
            j = text.find("\n", i)
            j = n if j == -1 else j
            comments.append((line, text[i:j]))
            i = j
            continue
        if text.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if text.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif text.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            comments.append((line, text[i:j]))
            line += text.count("\n", i, j)
            i = j
            continue
        start_line = line
        if c == "'" or (c in "eE" and i + 1 < n and text[i + 1] == "'" and not _word_before(text, i)):
            esc = c != "'"
            j = i + (2 if esc else 1)
            buf = []
            while j < n:
                ch = text[j]
                if esc and ch == "\\" and j + 1 < n:
                    buf.append(text[j:j + 2])
                    j += 2
                    continue
                if ch == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        buf.append("'")
                        j += 2
                        continue
                    break
                buf.append(ch)
                j += 1
            j = min(j + 1, n)
            line += text.count("\n", i, j)
            toks.append(Tok("str", "".join(buf), start_line, base_off + i, base_off + j))
            i = j
            continue
        if c == '"':
            j = i + 1
            buf = []
            while j < n:
                if text[j] == '"':
                    if j + 1 < n and text[j + 1] == '"':
                        buf.append('"')
                        j += 2
                        continue
                    break
                buf.append(text[j])
                j += 1
            j = min(j + 1, n)
            line += text.count("\n", i, j)
            toks.append(Tok("qident", "".join(buf), start_line, base_off + i, base_off + j))
            i = j
            continue
        if c == "$":
            m = DOLLAR_RE.match(text, i)
            if m and not _word_before(text, i):
                tag = m.group(0)
                k = text.find(tag, m.end())
                k = n if k == -1 else k
                j = min(k + len(tag), n)
                toks.append(Tok("dollar", text[m.end():k], start_line, base_off + i, base_off + j))
                toks[-1].body_start = base_off + m.end()        # type: ignore[attr-defined]
                toks[-1].body_line = start_line + text.count("\n", i, m.end())   # type: ignore[attr-defined]
                line += text.count("\n", i, j)
                i = j
                continue
            m = re.compile(r"\$\d+").match(text, i)
            if m:
                toks.append(Tok("param", m.group(0), line, base_off + i, base_off + m.end()))
                i = m.end()
                continue
        m = WORD_RE.match(text, i)
        if m:
            toks.append(Tok("word", m.group(0).lower(), line, base_off + i, base_off + m.end()))
            i = m.end()
            continue
        m = NUM_RE.match(text, i)
        if m:
            toks.append(Tok("num", m.group(0), line, base_off + i, base_off + m.end()))
            i = m.end()
            continue
        if text.startswith("::", i):
            toks.append(Tok("op", "::", line, base_off + i, base_off + i + 2))
            i += 2
            continue
        toks.append(Tok("op", c, line, base_off + i, base_off + i + 1))
        i += 1
    return toks, comments


def _word_before(text: str, i: int) -> bool:
    return i > 0 and (text[i - 1].isalnum() or text[i - 1] in "_$")


def split(text: str) -> Lexed:
    """Split a SQL file into statements. Statements inside DO $$ BEGIN ... END $$ blocks are
    extracted as well (drizzle-kit and hand-written migrations wrap DDL in them)."""
    toks, comments = tokenize(text)
    out = Lexed(comments=comments)
    _split_toks(text, toks, out.stmts, from_do=False)
    return out


def _split_toks(text: str, toks: list[Tok], out: list[Stmt], from_do: bool) -> None:
    cur: list[Tok] = []
    for t in toks + [Tok("op", ";", 0, len(text), len(text))]:
        if t.kind == "op" and t.val == ";":
            if cur:
                st = Stmt(cur, cur[0].line, cur[0].start, cur[-1].end, from_do=from_do)
                st.text = text[st.start:st.end]
                out.append(st)
                if cur[0].kind == "word" and cur[0].val == "do":
                    body = next((x for x in cur if x.kind == "dollar"), None)
                    if body is not None:
                        _split_do(text, body, out)
            cur = []
            continue
        cur.append(t)


def _split_do(text: str, body: Tok, out: list[Stmt]) -> None:
    """Statements inside a DO block body: BEGIN <stmts> [EXCEPTION ...] END."""
    start = getattr(body, "body_start", body.start)
    inner, _ = tokenize(body.val, getattr(body, "body_line", body.line), start)
    words = [t.val if t.kind == "word" else "" for t in inner]
    if not words or words[0] not in ("begin", "declare"):
        return
    if words[0] == "declare":
        try:
            b = words.index("begin")
        except ValueError:
            return
    else:
        b = 0
    # stop at a top-level EXCEPTION or the final END
    depth = 0
    stop = len(inner)
    for k in range(b + 1, len(inner)):
        w = words[k]
        if w in ("if", "loop", "case") and not (k > 0 and words[k - 1] == "end"):
            depth += 1
        elif w == "end" and depth > 0 and k + 1 < len(inner) and words[k + 1] in ("if", "loop", "case"):
            depth -= 1
        elif depth == 0 and w in ("exception", "end"):
            stop = k
            break
    sub = inner[b + 1:stop]
    if not sub:
        return
    collected: list[Stmt] = []
    full = text  # offsets of inner tokens are relative to the original file text
    _split_toks(full, sub, collected, from_do=True)
    for st in collected:
        if st.toks and is_word(st.toks[0], "if", "elsif", "else"):
            k = next((i for i, t in enumerate(st.toks) if is_word(t, "then", "else")), None)
            if k is None or k + 1 >= len(st.toks):
                continue
            rest = st.toks[k + 1:]
            st = Stmt(rest, rest[0].line, rest[0].start, rest[-1].end, text[rest[0].start:rest[-1].end], True)
        if st.toks and st.toks[0].kind == "word" and st.toks[0].val in (
                "alter", "create", "drop", "comment", "set"):
            out.append(st)


# ---------------------------------------------------------------- token helpers
def is_word(t: Tok | None, *vals: str) -> bool:
    return t is not None and t.kind == "word" and (not vals or t.val in vals)


def is_op(t: Tok | None, val: str) -> bool:
    return t is not None and t.kind == "op" and t.val == val


def ident_val(t: Tok) -> str:
    """Normalised identifier: unquoted words are folded to lower case, quoted ones keep case."""
    return t.val


def match_paren(toks: list[Tok], i: int) -> int:
    """Index of the token closing the parenthesis/bracket at toks[i]."""
    depth = 0
    for j in range(i, len(toks)):
        t = toks[j]
        if t.kind == "op" and t.val in "([":
            depth += 1
        elif t.kind == "op" and t.val in ")]":
            depth -= 1
            if depth == 0:
                return j
    return len(toks) - 1


def split_commas(toks: list[Tok]) -> list[list[Tok]]:
    out, cur, depth = [], [], 0
    for t in toks:
        if t.kind == "op" and t.val in "([":
            depth += 1
        elif t.kind == "op" and t.val in ")]":
            depth -= 1
        if depth == 0 and t.kind == "op" and t.val == ",":
            out.append(cur)
            cur = []
            continue
        cur.append(t)
    if cur:
        out.append(cur)
    return out


def qualified_name(toks: list[Tok], i: int) -> tuple[str | None, int]:
    """Read schema.name at toks[i]; returns (normalised name, next index). public. is dropped."""
    parts = []
    while i < len(toks) and toks[i].kind in ("word", "qident"):
        parts.append(toks[i].val)
        i += 1
        if i < len(toks) and is_op(toks[i], ".") and i + 1 < len(toks) and toks[i + 1].kind in ("word", "qident"):
            i += 1
            continue
        break
    if not parts:
        return None, i
    if len(parts) > 1 and parts[0] == "public":
        parts = parts[1:]
    return ".".join(parts), i


def source(text: str, toks: list[Tok]) -> str:
    if not toks:
        return ""
    return re.sub(r"\s+", " ", text[toks[0].start:toks[-1].end]).strip()
