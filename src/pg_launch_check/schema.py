"""A schema model built by replaying DDL in migration order.

`Schema.apply(stmt, ctx)` reads one statement (CREATE TABLE / INDEX, ALTER TABLE, DROP,
RENAME, ...), updates tables, columns, indexes, foreign keys and CHECK constraints, and returns
`Event`s describing operations on tables that already existed before the current file, which
sql_scan.py turns into lock-risk findings. Anything it does not understand is ignored."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .sqllex import Stmt, Tok, is_op, is_word, match_paren, qualified_name, source, split_commas

CONSTRAINT_START = {"constraint", "primary", "unique", "foreign", "check", "exclude"}
COL_KW = {"constraint", "not", "null", "default", "primary", "unique", "references", "check", "generated",
          "collate", "deferrable", "initially", "compression", "storage"}
VOLATILE_FUNCS = {"random", "gen_random_uuid", "uuid_generate_v1", "uuid_generate_v1mc", "uuid_generate_v4",
                  "clock_timestamp", "timeofday", "nextval", "gen_random_bytes", "uuidv4", "uuidv7",
                  "random_normal", "setseed"}
UUID_GEN_RE = re.compile(r"(?i)\b(gen_random_uuid|uuid_generate_v[14]\w*|uuidv[47])\s*\(")
SERIAL_TYPES = {"serial", "bigserial", "smallserial", "serial2", "serial4", "serial8"}


@dataclass
class Column:
    name: str
    type: str                  # display form, e.g. "varchar(255)", "timestamp(3)"
    base: str                  # normalised base type: timestamp, timestamptz, float, varchar, text, ...
    args: tuple = ()
    array: bool = False
    not_null: bool = False
    default: str | None = None
    volatile_default: str | None = None
    identity: bool = False
    generated_stored: bool = False
    file: str = ""
    line: int = 0

    @property
    def serial(self) -> bool:
        return self.base == "serial"


@dataclass
class Table:
    name: str
    file: str
    line: int
    columns: dict = field(default_factory=dict)
    pk: list | None = None
    pk_name: str | None = None
    partitioned: bool = False
    partition_child: bool = False
    derived: bool = False            # CREATE TABLE ... AS / LIKE / OF: columns not fully known
    managed: str = ""                # "prisma" when the schema of record is schema.prisma


@dataclass
class Index:
    name: str
    table: str
    cols: list | None                # None when any element is an expression
    unique: bool = False
    partial: bool = False
    method: str = "btree"
    trgm: set = field(default_factory=set)
    file: str = ""
    line: int = 0
    constraint: str = ""             # "pk" / "unique" when it backs a constraint


@dataclass
class ForeignKey:
    name: str
    table: str
    cols: list
    ref_table: str
    ref_cols: list
    file: str
    line: int


@dataclass
class Check:
    name: str
    table: str
    notnull_col: str | None
    valid: bool


@dataclass
class Event:
    kind: str                        # create_index | add_fk | add_check | add_unique | add_pk | rewrite_type |
    stmt: Stmt                       # add_rewrite | add_notnull | set_notnull | lock (any lock-taking DDL)
    table: str
    line: int
    info: dict = field(default_factory=dict)


@dataclass
class Ctx:
    file: str
    migration: bool                  # lock events are only produced for migration files
    pg_version: int = 14
    text: str = ""


def short(name: str) -> str:
    return name.split(".")[-1]


def parse_type(toks: list[Tok]) -> tuple[str, str, tuple, bool]:
    """(display, base, numeric args, array) for the tokens of a column type."""
    array = any(is_op(t, "[") for t in toks) or any(is_word(t, "array") for t in toks)
    words, args, depth, parts = [], [], 0, []
    for t in toks:
        if t.kind == "op" and t.val in "([":
            depth += 1
        elif t.kind == "op" and t.val in ")]":
            depth -= 1
        elif depth == 0 and t.kind in ("word", "qident") and t.val != "array":
            words.append(t.val.lower() if t.kind == "word" else t.val)
        elif depth == 1 and t.kind == "num":
            args.append(int(float(t.val)))
        if t.kind in ("word", "qident", "num") or (t.kind == "op" and t.val in "()[],"):
            parts.append(t.val if t.kind != "qident" else t.val)
    display = " ".join(parts)
    display = re.sub(r"\s*\(\s*", "(", display)
    display = re.sub(r"\s*\)", ")", display)
    display = re.sub(r"\s*,\s*", ",", display)
    display = re.sub(r"\s*\[\s*\]", "[]", display)
    if not words:
        return display, "", tuple(args), array
    w = words
    first = w[0].split(".")[-1]
    if first == "timestamp":
        base = "timestamptz" if "with" in w and "without" not in w else "timestamp"
    elif first == "timestamptz":
        base = "timestamptz"
    elif first == "double" or first in ("float8", "real", "float4") or first == "float":
        base = "float"
    elif first == "character" and len(w) > 1 and w[1] == "varying":
        base = "varchar"
    elif first in ("varchar",):
        base = "varchar"
    elif first in ("character", "char", "bpchar"):
        base = "char"
    elif first in ("numeric", "decimal"):
        base = "numeric"
    elif first in SERIAL_TYPES:
        base = "serial"
    elif first in ("int", "integer", "int4"):
        base = "integer"
    elif first in ("bigint", "int8"):
        base = "bigint"
    elif first in ("smallint", "int2"):
        base = "smallint"
    else:
        base = first
    return display, base, tuple(args), array


def _volatile(toks: list[Tok]) -> str | None:
    for i, t in enumerate(toks):
        if t.kind == "word" and t.val in VOLATILE_FUNCS and i + 1 < len(toks) and is_op(toks[i + 1], "("):
            return t.val + "()"
    return None


class Schema:
    def __init__(self):
        self.tables: dict[str, Table] = {}
        self.indexes: dict[str, Index] = {}          # by index name
        self.fks: list[ForeignKey] = []
        self.checks: list[Check] = []
        self.created_in: dict[str, set] = {}         # table -> files that contain a CREATE TABLE for it
        self.role_statement_timeout = False          # ALTER ROLE / DATABASE ... SET statement_timeout
        self._auto = 0

    # ------------------------------------------------------------ helpers
    def existing(self, table: str, ctx: Ctx) -> bool:
        return ctx.file not in self.created_in.get(table, set())

    def indexes_on(self, table: str) -> list[Index]:
        return [i for i in self.indexes.values() if i.table == table]

    def _index_name(self, table: str, cols: list | None, suffix: str) -> str:
        base = short(table) + "_" + ("_".join(cols) if cols else "expr") + "_" + suffix
        name = base
        k = 1
        while name in self.indexes:
            name = f"{base}{k}"
            k += 1
        return name

    def add_index(self, idx: Index) -> None:
        self.indexes[idx.name] = idx

    # ------------------------------------------------------------ dispatch
    def apply(self, st: Stmt, ctx: Ctx) -> list[Event]:
        t = st.toks
        w = st.words(8)
        try:
            if w[0] == "create":
                k = 1
                while k < len(t) and is_word(t[k], "global", "local", "temp", "temporary", "unlogged", "or",
                                             "replace"):
                    k += 1
                if is_word(t[k] if k < len(t) else None, "table"):
                    if any(x in ("temp", "temporary") for x in w[1:k]):
                        return []
                    return self.create_table(st, k + 1, ctx)
                if is_word(t[k] if k < len(t) else None, "unique") or is_word(t[k] if k < len(t) else None, "index"):
                    return self.create_index(st, k, ctx)
                if is_word(t[k] if k < len(t) else None, "trigger", "constraint"):
                    return self.create_trigger(st, ctx)
                return []
            if w[0] == "alter" and w[1] == "table":
                return self.alter_table(st, ctx)
            if w[0] == "alter" and w[1] == "index":
                self.alter_index(st)
                return []
            if w[0] == "alter" and w[1] in ("role", "user", "database"):
                if re.search(r"(?i)\bset\s+statement_timeout\b", st.text):
                    self.role_statement_timeout = True
                return []
            if w[0] == "drop" and w[1] == "table":
                self.drop_table(st)
                return []
            if w[0] == "drop" and w[1] == "index":
                return self.drop_index(st, ctx)
            if w[0] == "lock":
                name, _ = qualified_name(t, 2 if w[1] == "table" else 1)
                if name and ctx.migration and self.existing(name, ctx):
                    return [Event("lock", st, name, st.line)]
        except IndexError:
            return []
        return []

    # ------------------------------------------------------------ CREATE TABLE
    def create_table(self, st: Stmt, i: int, ctx: Ctx) -> list[Event]:
        t = st.toks
        if_not_exists = False
        if is_word(t[i], "if") and is_word(t[i + 1], "not"):
            if_not_exists = True
            i += 3
        name, i = qualified_name(t, i)
        if not name:
            return []
        self.created_in.setdefault(name, set()).add(ctx.file)
        if if_not_exists and name in self.tables:
            return []
        self._drop_table_objects(name)
        tab = Table(name, ctx.file, st.line)
        self.tables[name] = tab
        if i >= len(t):
            return []
        if is_word(t[i], "partition") and is_word(t[i + 1] if i + 1 < len(t) else None, "of"):
            tab.partition_child = True
            return []
        if is_word(t[i], "as", "of"):
            tab.derived = True
            return []
        if not is_op(t[i], "("):
            tab.derived = True
            return []
        close = match_paren(t, i)
        rest = t[close + 1:]
        if any(is_word(x, "partition") for x in rest[:2]):
            tab.partitioned = True
        for el in split_commas(t[i + 1:close]):
            if not el:
                continue
            if el[0].kind == "word" and el[0].val in CONSTRAINT_START:
                self.table_constraint(tab, el, st, ctx, in_create=True)
            elif el[0].kind == "word" and el[0].val == "like":
                tab.derived = True
            else:
                self.column_def(tab, el, st, ctx)
        return []

    def column_def(self, tab: Table, el: list[Tok], st: Stmt, ctx: Ctx) -> Column | None:
        if not el or el[0].kind not in ("word", "qident"):
            return None
        name = el[0].val
        j, depth = 1, 0
        while j < len(el):
            x = el[j]
            if x.kind == "op" and x.val in "([":
                depth += 1
            elif x.kind == "op" and x.val in ")]":
                depth -= 1
            elif depth == 0 and x.kind == "word" and x.val in COL_KW:
                break
            j += 1
        display, base, args, array = parse_type(el[1:j])
        col = Column(name, display, base, args, array, file=ctx.file, line=el[0].line)
        k = j
        while k < len(el):
            x = el[k]
            if is_word(x, "constraint"):
                k += 2
                continue
            if is_word(x, "not") and is_word(el[k + 1] if k + 1 < len(el) else None, "null"):
                col.not_null = True
                k += 2
                continue
            if is_word(x, "null"):
                k += 1
                continue
            if is_word(x, "default"):
                m, depth = k + 1, 0
                while m < len(el):
                    y = el[m]
                    if y.kind == "op" and y.val in "([":
                        depth += 1
                    elif y.kind == "op" and y.val in ")]":
                        depth -= 1
                    elif depth == 0 and y.kind == "word" and y.val in COL_KW - {"null"}:
                        break
                    m += 1
                dtoks = el[k + 1:m]
                col.default = source(ctx.text, dtoks) if ctx.text else " ".join(x.val for x in dtoks)
                col.volatile_default = _volatile(dtoks)
                k = m
                continue
            if is_word(x, "primary"):
                tab.pk = [name]
                col.not_null = True
                tab.pk_name = f"{short(tab.name)}_pkey"
                self.add_index(Index(tab.pk_name, tab.name, [name], unique=True, file=ctx.file, line=x.line,
                                     constraint="pk"))
                k += 2
                continue
            if is_word(x, "unique"):
                self.add_index(Index(self._index_name(tab.name, [name], "key"), tab.name, [name], unique=True,
                                     file=ctx.file, line=x.line, constraint="unique"))
                k += 1
                continue
            if is_word(x, "references"):
                ref, m = qualified_name(el, k + 1)
                ref_cols = []
                if m < len(el) and is_op(el[m], "("):
                    c = match_paren(el, m)
                    ref_cols = [y.val for y in el[m + 1:c] if y.kind in ("word", "qident")]
                    m = c + 1
                if ref:
                    self.fks.append(ForeignKey(f"{short(tab.name)}_{name}_fkey", tab.name, [name], ref, ref_cols,
                                               ctx.file, x.line))
                k = m
                continue
            if is_word(x, "check"):
                m = k + 1
                if m < len(el) and is_op(el[m], "("):
                    c = match_paren(el, m)
                    inner = el[m + 1:c]
                    nn = self._notnull_check(inner)
                    self.checks.append(Check(f"{short(tab.name)}_{name}_check", tab.name, nn, True))
                    m = c + 1
                k = m
                continue
            if is_word(x, "generated"):
                m = k + 1
                while m < len(el) and not is_word(el[m], "as"):
                    m += 1
                if m + 1 < len(el) and is_word(el[m + 1], "identity"):
                    col.identity = True
                    col.not_null = True
                    k = m + 2
                    if k < len(el) and is_op(el[k], "("):
                        k = match_paren(el, k) + 1
                    continue
                if m + 1 < len(el) and is_op(el[m + 1], "("):
                    c = match_paren(el, m + 1)
                    if c + 1 < len(el) and is_word(el[c + 1], "stored"):
                        col.generated_stored = True
                    k = c + 1
                    continue
                k = m + 1
                continue
            k += 1
        if col.serial:
            col.not_null = True
        tab.columns[name] = col
        return col

    @staticmethod
    def _notnull_check(inner: list[Tok]) -> str | None:
        toks = inner
        while toks and is_op(toks[0], "(") and match_paren(toks, 0) == len(toks) - 1:
            toks = toks[1:-1]
        if len(toks) == 4 and toks[0].kind in ("word", "qident") and is_word(toks[1], "is") and \
                is_word(toks[2], "not") and is_word(toks[3], "null"):
            return toks[0].val
        return None

    def table_constraint(self, tab: Table | None, el: list[Tok], st: Stmt, ctx: Ctx, in_create: bool,
                         table_name: str | None = None) -> dict | None:
        """Register a table constraint. Returns a description for ALTER TABLE lock checks."""
        tname = tab.name if tab else table_name
        k = 0
        cname = None
        if is_word(el[0], "constraint") and len(el) > 1:
            cname = el[1].val
            k = 2
        if k >= len(el):
            return None
        kind = el[k].val if el[k].kind == "word" else ""
        not_valid = any(is_word(el[m], "not") and is_word(el[m + 1], "valid") for m in range(len(el) - 1))
        line = el[0].line
        if kind in ("primary", "unique"):
            m = k + (2 if kind == "primary" else 1)
            while m < len(el) and is_word(el[m], "nulls", "not", "distinct"):
                m += 1
            using_index = m < len(el) and is_word(el[m], "using") and is_word(el[m + 1] if m + 1 < len(el) else None,
                                                                            "index")
            if using_index:
                iname = el[m + 2].val if m + 2 < len(el) else ""
                idx = self.indexes.pop(iname, None) or next(
                    (i for n, i in list(self.indexes.items()) if short(n) == iname), None)
                cols = idx.cols if idx else None
                newname = cname or iname
                if idx:
                    self.indexes.pop(idx.name, None)
                    idx.name = newname
                    idx.constraint = "pk" if kind == "primary" else "unique"
                    self.add_index(idx)
                if kind == "primary" and tname in self.tables:
                    self.tables[tname].pk = cols
                    self.tables[tname].pk_name = newname
                return {"kind": "using_index", "cname": newname}
            cols = []
            if m < len(el) and is_op(el[m], "("):
                c = match_paren(el, m)
                cols = [y.val for y in el[m + 1:c] if y.kind in ("word", "qident")]
            suffix = "pkey" if kind == "primary" else "key"
            iname = cname or (f"{short(tname)}_pkey" if kind == "primary" else self._index_name(tname, cols, "key"))
            self.add_index(Index(iname, tname, cols, unique=True, file=ctx.file, line=line,
                                 constraint="pk" if kind == "primary" else "unique"))
            if kind == "primary" and tname in self.tables:
                self.tables[tname].pk = cols
                self.tables[tname].pk_name = iname
                for c in cols:
                    if c in self.tables[tname].columns:
                        self.tables[tname].columns[c].not_null = True
            return {"kind": "add_pk" if kind == "primary" else "add_unique", "cname": iname, "cols": cols,
                    "suffix": suffix}
        if kind == "foreign":
            m = k + 2
            cols = []
            if m < len(el) and is_op(el[m], "("):
                c = match_paren(el, m)
                cols = [y.val for y in el[m + 1:c] if y.kind in ("word", "qident")]
                m = c + 1
            ref = None
            ref_cols = []
            if m < len(el) and is_word(el[m], "references"):
                ref, m = qualified_name(el, m + 1)
                if m < len(el) and is_op(el[m], "("):
                    c = match_paren(el, m)
                    ref_cols = [y.val for y in el[m + 1:c] if y.kind in ("word", "qident")]
            name = cname or f"{short(tname)}_{'_'.join(cols)}_fkey"
            if ref and cols:
                self.fks.append(ForeignKey(name, tname, cols, ref, ref_cols, ctx.file, line))
            return {"kind": "add_fk", "cname": name, "cols": cols, "ref": ref, "not_valid": not_valid}
        if kind == "check":
            m = k + 1
            nn = None
            if m < len(el) and is_op(el[m], "("):
                c = match_paren(el, m)
                nn = self._notnull_check(el[m + 1:c])
            name = cname or f"{short(tname)}_check"
            self.checks.append(Check(name, tname, nn, not not_valid))
            return {"kind": "add_check", "cname": name, "not_valid": not_valid}
        if kind == "exclude":
            name = cname or self._index_name(tname, None, "excl")
            self.add_index(Index(name, tname, None, file=ctx.file, line=line, constraint="exclude"))
            return {"kind": "add_exclude", "cname": name}
        return None

    # ------------------------------------------------------------ CREATE INDEX
    def create_index(self, st: Stmt, i: int, ctx: Ctx) -> list[Event]:
        t = st.toks
        unique = False
        if is_word(t[i], "unique"):
            unique = True
            i += 1
        i += 1   # INDEX
        concurrently = False
        if i < len(t) and is_word(t[i], "concurrently"):
            concurrently = True
            i += 1
        if i < len(t) and is_word(t[i], "if"):
            i += 3
        iname = None
        if i < len(t) and not is_word(t[i], "on"):
            iname, i = qualified_name(t, i)
        if i >= len(t) or not is_word(t[i], "on"):
            return []
        i += 1
        only = False
        if is_word(t[i], "only"):
            only = True
            i += 1
        table, i = qualified_name(t, i)
        if not table:
            return []
        method = "btree"
        if i < len(t) and is_word(t[i], "using"):
            method = t[i + 1].val
            i += 2
        if i >= len(t) or not is_op(t[i], "("):
            return []
        close = match_paren(t, i)
        cols: list | None = []
        trgm: set = set()
        for el in split_commas(t[i + 1:close]):
            if not el:
                continue
            ops = [x.val for x in el if x.kind == "word"]
            is_trgm = any(o in ("gin_trgm_ops", "gist_trgm_ops") for o in ops)
            if el[0].kind in ("word", "qident") and (len(el) == 1 or (el[1].kind in ("word", "qident") and
                                                                     not is_op(el[1], "("))):
                if cols is not None:
                    cols.append(el[0].val)
                if is_trgm:
                    trgm.add(el[0].val)
            else:
                cols = None
                if is_trgm:
                    for x in el:
                        if x.kind in ("word", "qident") and x.val not in ("lower", "upper", "unaccent", "immutable_unaccent",
                                                                          "gin_trgm_ops", "gist_trgm_ops", "text",
                                                                          "coalesce", "f_unaccent"):
                            trgm.add(x.val)
        rest = t[close + 1:]
        partial = any(is_word(x, "where") for x in rest)
        name = (short(iname) if iname else None) or self._index_name(table, cols, "idx")
        self.add_index(Index(name, table, cols, unique=unique, partial=partial, method=method, trgm=trgm,
                             file=ctx.file, line=st.line))
        if ctx.migration and not concurrently and not only and self.existing(table, ctx):
            tab = self.tables.get(table)
            return [Event("create_index", st, table, st.line,
                          {"index": name, "unique": unique, "partitioned": bool(tab and tab.partitioned)})]
        return []

    def create_trigger(self, st: Stmt, ctx: Ctx) -> list[Event]:
        t = st.toks
        for k in range(len(t) - 1):
            if is_word(t[k], "on"):
                name, _ = qualified_name(t, k + 1)
                if name and ctx.migration and self.existing(name, ctx) and name in self.tables:
                    return [Event("lock", st, name, st.line)]
                break
        return []

    def alter_index(self, st: Stmt) -> None:
        t = st.toks
        i = 2
        if is_word(t[i], "if"):
            i += 2
        name, i = qualified_name(t, i)
        if name and i + 2 < len(t) and is_word(t[i], "rename") and is_word(t[i + 1], "to"):
            idx = self.indexes.pop(short(name), None)
            if idx:
                idx.name = t[i + 2].val
                self.add_index(idx)

    def drop_index(self, st: Stmt, ctx: Ctx) -> list[Event]:
        t = st.toks
        i = 2
        concurrently = False
        if i < len(t) and is_word(t[i], "concurrently"):
            concurrently = True
            i += 1
        if i < len(t) and is_word(t[i], "if"):
            i += 2
        events = []
        for el in split_commas(t[i:]):
            name, _ = qualified_name(el, 0)
            if not name:
                continue
            idx = self.indexes.pop(short(name), None)
            if idx and ctx.migration and not concurrently and self.existing(idx.table, ctx):
                events.append(Event("lock", st, idx.table, st.line))
        return events

    # ------------------------------------------------------------ DROP TABLE
    def _drop_table_objects(self, name: str) -> None:
        for n in [n for n, i in self.indexes.items() if i.table == name]:
            del self.indexes[n]
        self.fks = [f for f in self.fks if f.table != name and f.ref_table != name]
        self.checks = [c for c in self.checks if c.table != name]

    def drop_table(self, st: Stmt) -> None:
        t = st.toks
        i = 2
        if i < len(t) and is_word(t[i], "if"):
            i += 2
        for el in split_commas(t[i:]):
            name, _ = qualified_name(el, 0)
            if name:
                self.tables.pop(name, None)
                self._drop_table_objects(name)

    # ------------------------------------------------------------ ALTER TABLE
    def alter_table(self, st: Stmt, ctx: Ctx) -> list[Event]:
        t = st.toks
        i = 2
        if i < len(t) and is_word(t[i], "if") and is_word(t[i + 1], "exists"):
            i += 2
        if i < len(t) and is_word(t[i], "only"):
            i += 1
        name, i = qualified_name(t, i)
        if not name:
            return []
        if i < len(t) and is_op(t[i], "*"):
            i += 1
        existing = ctx.migration and self.existing(name, ctx)
        events: list[Event] = []
        rest = t[i:]
        if not rest:
            return []
        # RENAME forms take the whole statement
        if is_word(rest[0], "rename"):
            self._rename(name, rest)
            return [Event("lock", st, name, st.line)] if existing else []
        if is_word(rest[0], "set") and is_word(rest[1] if len(rest) > 1 else None, "schema"):
            return []
        tab = self.tables.get(name)
        for act in split_commas(rest):
            if not act:
                continue
            ev = self._alter_action(name, tab, act, st, ctx, existing)
            events += ev
        if existing:
            events.append(Event("lock", st, name, st.line))
        return events

    def _rename(self, table: str, rest: list[Tok]) -> None:
        if len(rest) >= 3 and is_word(rest[1], "to"):
            new = rest[2].val
            if "." in table:
                new = table.rsplit(".", 1)[0] + "." + new
            tab = self.tables.pop(table, None)
            if tab:
                tab.name = new
                self.tables[new] = tab
            self.created_in[new] = self.created_in.pop(table, set())
            for idx in self.indexes.values():
                if idx.table == table:
                    idx.table = new
            for f in self.fks:
                if f.table == table:
                    f.table = new
                if f.ref_table == table:
                    f.ref_table = new
            for c in self.checks:
                if c.table == table:
                    c.table = new
            return
        k = 1
        if is_word(rest[k], "constraint"):
            if len(rest) >= 5:
                old, new = rest[2].val, rest[4].val
                idx = self.indexes.pop(old, None)
                if idx:
                    idx.name = new
                    self.add_index(idx)
                for f in self.fks:
                    if f.name == old and f.table == table:
                        f.name = new
            return
        if is_word(rest[k], "column"):
            k += 1
        if k + 2 < len(rest) and is_word(rest[k + 1], "to"):
            old, new = rest[k].val, rest[k + 2].val
            tab = self.tables.get(table)
            if tab and old in tab.columns:
                col = tab.columns.pop(old)
                col.name = new
                tab.columns[new] = col
                if tab.pk:
                    tab.pk = [new if c == old else c for c in tab.pk]
            for idx in self.indexes.values():
                if idx.table == table and idx.cols:
                    idx.cols = [new if c == old else c for c in idx.cols]
                if idx.table == table and old in idx.trgm:
                    idx.trgm = {new if c == old else c for c in idx.trgm}
            for f in self.fks:
                if f.table == table:
                    f.cols = [new if c == old else c for c in f.cols]
                if f.ref_table == table:
                    f.ref_cols = [new if c == old else c for c in f.ref_cols]

    def _alter_action(self, name: str, tab: Table | None, act: list[Tok], st: Stmt, ctx: Ctx,
                      existing: bool) -> list[Event]:
        w0 = act[0].val if act[0].kind == "word" else ""
        events: list[Event] = []
        line = act[0].line
        if w0 == "add":
            k = 1
            is_col = False
            if k < len(act) and is_word(act[k], "column"):
                k += 1
                is_col = True
            if k < len(act) and is_word(act[k], "if") and is_word(act[k + 1] if k + 1 < len(act) else None, "not"):
                k += 3
            if not is_col and k < len(act) and act[k].kind == "word" and act[k].val in CONSTRAINT_START:
                desc = self.table_constraint(tab, act[k:], st, ctx, in_create=False, table_name=name)
                if desc and existing:
                    kind = desc["kind"]
                    if kind == "add_fk" and not desc.get("not_valid"):
                        events.append(Event("add_fk", st, name, line, desc | {"src": source(ctx.text, act[k:])}))
                    elif kind == "add_check" and not desc.get("not_valid"):
                        events.append(Event("add_check", st, name, line, desc | {"src": source(ctx.text, act[k:])}))
                    elif kind in ("add_unique", "add_pk"):
                        events.append(Event(kind, st, name, line, desc))
                return events
            if tab is None:
                tab = Table(name, ctx.file, st.line, derived=True)
                self.tables[name] = tab
            col = self.column_def(tab, act[k:], st, ctx)
            if col is None or not existing:
                return events
            info = {"col": col.name, "type": col.type, "default": col.default}
            if col.volatile_default or col.serial or col.identity or col.generated_stored:
                why = ("volatile default " + col.volatile_default if col.volatile_default else
                       "serial type" if col.serial else "identity column" if col.identity else
                       "stored generated column")
                events.append(Event("add_rewrite", st, name, line, info | {"why": why}))
            elif col.default is not None and ctx.pg_version < 11:
                events.append(Event("add_rewrite", st, name, line, info | {"why": "default (PostgreSQL < 11)"}))
            elif col.not_null and col.default is None:
                events.append(Event("add_notnull", st, name, line, info))
            return events
        if w0 == "drop":
            k = 1
            if k < len(act) and is_word(act[k], "constraint"):
                k += 1
                if is_word(act[k], "if"):
                    k += 2
                cname = act[k].val if k < len(act) else ""
                idx = self.indexes.pop(cname, None)
                if idx and idx.constraint == "pk" and tab:
                    tab.pk = None
                self.fks = [f for f in self.fks if not (f.table == name and f.name == cname)]
                self.checks = [c for c in self.checks if not (c.table == name and c.name == cname)]
                return events
            if k < len(act) and is_word(act[k], "column"):
                k += 1
            if k < len(act) and is_word(act[k], "if"):
                k += 2
            if k < len(act):
                col = act[k].val
                if tab:
                    tab.columns.pop(col, None)
                    if tab.pk and col in tab.pk:
                        tab.pk = None
                for n in [n for n, i in self.indexes.items() if i.table == name and i.cols and col in i.cols]:
                    del self.indexes[n]
                self.fks = [f for f in self.fks if not (f.table == name and col in f.cols)]
            return events
        if w0 == "alter":
            k = 1
            if k < len(act) and is_word(act[k], "column"):
                k += 1
            if k >= len(act):
                return events
            cname = act[k].val
            k += 1
            col = tab.columns.get(cname) if tab else None
            if k < len(act) and (is_word(act[k], "type") or (is_word(act[k], "set") and k + 2 < len(act) and
                                                             is_word(act[k + 1], "data"))):
                k += 1 if is_word(act[k], "type") else 3
                m = k
                while m < len(act) and not is_word(act[m], "using", "collate"):
                    m += 1
                display, base, args, array = parse_type(act[k:m])
                using = m < len(act) and is_word(act[m], "using")
                old = (col.type, col.base, col.args, col.array) if col else None
                if col:
                    col.type, col.base, col.args, col.array = display, base, args, array
                if existing:
                    why = _type_change_rewrites(old, (display, base, args, array), using)
                    if why:
                        events.append(Event("rewrite_type", st, name, line,
                                            {"col": cname, "type": display, "old": old[0] if old else None,
                                             "why": why}))
                return events
            if k + 1 < len(act) and is_word(act[k], "set") and is_word(act[k + 1], "not"):
                if col:
                    col.not_null = True
                if existing:
                    covered = any(c.table == name and c.notnull_col == cname and c.valid for c in self.checks)
                    if not covered:
                        events.append(Event("set_notnull", st, name, line,
                                            {"col": cname, "type": col.type if col else None}))
                return events
            if k + 1 < len(act) and is_word(act[k], "drop") and is_word(act[k + 1], "not"):
                if col:
                    col.not_null = False
                return events
            if k + 1 < len(act) and is_word(act[k], "set") and is_word(act[k + 1], "default"):
                if col:
                    col.default = source(ctx.text, act[k + 2:]) if ctx.text else "?"
                return events
            if k + 1 < len(act) and is_word(act[k], "drop") and is_word(act[k + 1], "default"):
                if col:
                    col.default = None
                return events
            return events
        if w0 == "validate" and len(act) > 2:
            cname = act[2].val
            for c in self.checks:
                if c.table == name and c.name == cname:
                    c.valid = True
            return events
        return events


def _type_change_rewrites(old, new, using: bool) -> str | None:
    """Why ALTER COLUMN TYPE rewrites the table, or None when it is known not to."""
    display, base, args, array = new
    if old is None:
        if base in ("text",) or (base == "varchar" and not args):
            return None
        return "type change"
    odisplay, obase, oargs, oarray = old
    if using:
        return "type change with USING"
    if odisplay == display:
        return None
    if array != oarray:
        return "type change"
    if obase in ("varchar", "text") and (base == "text" or (base == "varchar" and not args)):
        return None
    if obase == "varchar" and base == "varchar" and oargs and args and args[0] >= oargs[0]:
        return None
    if obase == "numeric" and base == "numeric":
        if not args:
            return None
        if oargs and len(args) >= 1:
            os_ = oargs[1] if len(oargs) > 1 else 0
            ns = args[1] if len(args) > 1 else 0
            if ns == os_ and args[0] >= oargs[0]:
                return None
    if {obase, base} == {"timestamp", "timestamptz"}:
        return "timestamp <-> timestamptz (no rewrite on PostgreSQL 12+ only when the session TimeZone is UTC)"
    return "type change"
