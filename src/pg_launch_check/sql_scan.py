"""SQL migrations and schema files: ordering, schema replay and the schema / lock rules.

Migration files are replayed in order (Prisma `migrations/<ts>_name/migration.sql`, Supabase
`supabase/migrations/<ts>_name.sql`, Drizzle `NNNN_name.sql`, Flyway `V1__name.sql`, any
`migrations/` folder, `*.up.sql`). Other .sql files (schema dumps such as `schema.sql`) build
the schema but never get lock-risk findings, because they are not run against a live table."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .findings import Finding, clip_line
from .schema import UUID_GEN_RE, Ctx, Schema, short
from .sqllex import split

NON_PG_RE = re.compile(r"(?i)\bAUTO_INCREMENT\b|\bENGINE\s*=\s*\w+|\bAUTOINCREMENT\b|^\s*PRAGMA\s|\bNVARCHAR\s*\(|"
                       r"\[dbo\]|\bIDENTITY\s*\(\s*1\s*,\s*1\s*\)|`\w+`\s*\(", re.M)
SKIP_SQL_RE = re.compile(r"(?i)(^|/)(seed|seeds)(/|\.sql$|_)|(^|[/_.-])down\.sql$|(^|/)rollback|(^|[/_.-])undo")
MIGRATION_DIR_RE = re.compile(r"(?i)(^|/)(migrations?|migrate|db/migrate|flyway|sql/migrations|drizzle)(/|$)")
MIGRATION_NAME_RE = re.compile(r"(?i)^(\d{3,}[_\-.]|V\d+(?:[._]\d+)*__|R__)|\.up\.sql$")
MONEY_RE = re.compile(r"(?i)(^|_)(price|prices|amount|amounts|cost|costs|total|subtotal|grand_total|balance|fee|fees|"
                      r"salary|revenue|payment|payout|paid|refund|refunded|tax|discount|charge|credit|debit|money|"
                      r"wage|budget|earnings|income|spend|spent|mrr|arr)(_|$)")
NOT_MONEY_RE = re.compile(r"(?i)(rate|ratio|percent|pct|score|weight|count|qty|quantity|factor|share|lat|lng|"
                          r"latitude|longitude|duration|seconds|ms|tokens|probability|confidence)")


def snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).lower()


def is_money_name(name: str) -> bool:
    s = snake(name)
    return bool(MONEY_RE.search(s)) and not NOT_MONEY_RE.search(s)


@dataclass
class SqlFile:
    rel: str
    text: str
    migration: bool
    kind: str            # prisma | drizzle | supabase | sql
    set_id: str          # folder of the migration set
    mid: str             # migration id used for ordering / --applied-up-to


@dataclass
class SqlResult:
    schema: Schema
    findings: list[Finding] = field(default_factory=list)
    files: list[SqlFile] = field(default_factory=list)
    trgm_cols: set = field(default_factory=set)        # normalised column names with a trigram index


def classify(rel: str, text: str, prisma_roots: set[str], drizzle_dirs: set[str],
             non_pg_dirs: set[str]) -> SqlFile | None:
    low = rel.lower()
    if SKIP_SQL_RE.search(low):
        return None
    parts = rel.split("/")
    parent = "/".join(parts[:-1])
    for d in non_pg_dirs:
        if rel.startswith(d + "/"):
            return None
    if NON_PG_RE.search(text):
        return None
    name = parts[-1]
    if name == "migration.sql" and len(parts) >= 3 and parts[-3] == "migrations":
        root = "/".join(parts[:-2])
        return SqlFile(rel, text, True, "prisma" if root in prisma_roots else "sql", root, parts[-2])
    if parent in drizzle_dirs:
        return SqlFile(rel, text, True, "drizzle", parent, name[:-4])
    if "supabase/migrations/" in low:
        return SqlFile(rel, text, True, "supabase", parent, name[:-4])
    if MIGRATION_DIR_RE.search(parent) or MIGRATION_NAME_RE.search(name):
        return SqlFile(rel, text, True, "sql", parent, name[:-4])
    return SqlFile(rel, text, False, "sql", parent, name[:-4])


def order(files: list[SqlFile]) -> list[SqlFile]:
    """Migrations first (each set in name order), then schema files."""
    return sorted(files, key=lambda f: (not f.migration, f.set_id, f.mid, f.rel))


def _applied(f: SqlFile, applied_up_to: str | None) -> bool:
    if not applied_up_to or not f.migration:
        return False
    a = applied_up_to.strip().removesuffix(".sql")
    return f.mid <= a or f.mid.startswith(a)


def _lineof(text: str, n: int) -> str:
    lines = text.splitlines()
    return lines[n - 1] if 0 < n <= len(lines) else ""


def scan_sql(files: list[SqlFile], *, pg_version: int = 14, applied_up_to: str | None = None,
             prisma_managed: bool = False) -> SqlResult:
    schema = Schema()
    res = SqlResult(schema, files=files)
    lock_seen: dict[str, int] = {}
    for f in order(files):
        ctx = Ctx(f.rel, f.migration, pg_version, f.text)
        lexed = split(f.text)
        has_lock_timeout = bool(re.search(r"(?i)\bset\s+(?:local\s+|session\s+)?(lock_timeout|statement_timeout)\b|"
                                          r"set_config\s*\(\s*'(lock_timeout|statement_timeout)'", f.text))
        report = f.migration and not _applied(f, applied_up_to)
        for st in lexed.stmts:
            events = schema.apply(st, ctx)
            if not report:
                continue
            for ev in events:
                if ev.kind == "lock":
                    if not has_lock_timeout and f.rel not in lock_seen:
                        lock_seen[f.rel] = ev.line
                    continue
                res.findings += _event_finding(ev, f, pg_version)
        if f.kind == "prisma" and prisma_managed:
            for t in schema.tables.values():
                if t.file == f.rel:
                    t.managed = "prisma"
    for rel, line in lock_seen.items():
        text = next(x.text for x in files if x.rel == rel)
        res.findings.append(Finding(
            "PGL006", "low",
            "This migration takes locks on existing tables but never sets lock_timeout: if a long "
            "transaction holds the table, the migration waits and every query on the table queues behind it.",
            rel, line, clip_line(_lineof(text, line)), extra={"fixkeys": ["sql"]}))
    texts = {f.rel: f.text for f in files}
    res.findings += _schema_findings(schema, texts)
    for idx in schema.indexes.values():
        for c in idx.trgm:
            res.trgm_cols.add(snake(c).replace("_", ""))
    return res


def _event_finding(ev, f: SqlFile, pg_version: int) -> list[Finding]:
    ev_line = _lineof(f.text, ev.line)
    tbl = ev.table
    i = ev.info
    if ev.kind == "create_index":
        keys = ["partitioned"] if i.get("partitioned") else (
            ["prisma-migrate"] if f.kind == "prisma" else ["drizzle"] if f.kind == "drizzle" else ["sql"])
        stmt = re.sub(r"\s+", " ", ev.stmt.text.strip())
        stmt = re.sub(r"(?i)^create\s+(unique\s+)?index\b", lambda m: m.group(0) + " CONCURRENTLY", stmt) + ";"
        return [Finding("PGL002", "medium",
                        f"CREATE {'UNIQUE ' if i.get('unique') else ''}INDEX on {tbl}, which exists before this "
                        f"migration, without CONCURRENTLY: writes to {tbl} are blocked for the whole index build.",
                        f.rel, ev.line, clip_line(ev_line),
                        extra={"fixkeys": keys, "table": tbl, "index": i.get("index"), "unique": i.get("unique"),
                               "stmt": stmt})]
    if ev.kind in ("add_fk", "add_check"):
        what = "FOREIGN KEY" if ev.kind == "add_fk" else "CHECK"
        lock = ("blocks writes on both tables" if ev.kind == "add_fk" else "blocks reads and writes")
        return [Finding("PGL003", "medium",
                        f"{what} constraint {i.get('cname')} is added to existing table {tbl} without NOT VALID: "
                        f"every existing row is checked while holding a lock that {lock}.",
                        f.rel, ev.line, clip_line(ev_line),
                        extra={"fixkeys": ["fk" if ev.kind == "add_fk" else "check"], "table": tbl,
                               "constraint": i.get("cname"), "constraint_src": i.get("src")})]
    if ev.kind in ("add_unique", "add_pk"):
        what = "PRIMARY KEY" if ev.kind == "add_pk" else "UNIQUE"
        return [Finding("PGL003", "high",
                        f"{what} constraint on existing table {tbl} builds its index while holding an ACCESS "
                        f"EXCLUSIVE lock: no reads or writes on {tbl} until it is built.",
                        f.rel, ev.line, clip_line(ev_line),
                        extra={"fixkeys": ["pk" if ev.kind == "add_pk" else "unique"], "table": tbl,
                               "constraint": i.get("cname"), "cols": i.get("cols")})]
    if ev.kind == "rewrite_type":
        old = f" from {i['old']}" if i.get("old") else ""
        return [Finding("PGL004", "high",
                        f"Changing {tbl}.{i['col']}{old} to {i['type']} rewrites the whole table and its indexes "
                        f"under an ACCESS EXCLUSIVE lock ({i['why']}).",
                        f.rel, ev.line, clip_line(ev_line),
                        extra={"fixkeys": ["type"], "table": tbl, "col": i["col"], "type": i["type"]})]
    if ev.kind == "add_rewrite":
        default = i.get("default") or ""
        if "serial" in i["why"]:
            default = f"nextval('{short(tbl)}_{i['col']}_seq')"
        elif "identity" in i["why"] or "generated" in i["why"]:
            default = "..."
        return [Finding("PGL004", "high",
                        f"Adding {tbl}.{i['col']} with a {i['why']} rewrites every row of {tbl} under an ACCESS "
                        f"EXCLUSIVE lock (each row needs its own value).",
                        f.rel, ev.line, clip_line(ev_line),
                        extra={"fixkeys": ["add"], "table": tbl, "col": i["col"],
                               "type": re.sub(r"(?i)^(big|small)?serial\d?$", "bigint", i.get("type") or ""),
                               "default": default})]
    if ev.kind == "add_notnull":
        return [Finding("PGL005", "medium",
                        f"{tbl}.{i['col']} is added as NOT NULL without a DEFAULT: the migration fails with "
                        f"\"column contains null values\" as soon as {tbl} has rows.",
                        f.rel, ev.line, clip_line(ev_line),
                        extra={"fixkeys": ["add"], "table": tbl, "col": i["col"], "type": i.get("type")})]
    if ev.kind == "set_notnull":
        return [Finding("PGL005", "medium",
                        f"SET NOT NULL on {tbl}.{i['col']} scans the whole table under an ACCESS EXCLUSIVE lock "
                        f"(no reads or writes) because no validated CHECK ({i['col']} IS NOT NULL) exists.",
                        f.rel, ev.line, clip_line(ev_line),
                        extra={"fixkeys": ["set"], "table": tbl, "col": i["col"]})]
    return []


def covered(schema: Schema, table: str, cols: list[str]) -> tuple[bool, bool]:
    """(covered by a usable index, a partial index exists that would otherwise cover)."""
    n = len(cols)
    partial = False
    for idx in schema.indexes_on(table):
        if not idx.cols or len(idx.cols) < n:
            continue
        if idx.method not in ("btree", "hash"):
            continue
        if idx.method == "hash" and (n != 1 or idx.cols != cols):
            continue
        if set(idx.cols[:n]) == set(cols):
            if idx.partial:
                partial = True
                continue
            return True, False
    return False, partial


def _schema_findings(schema: Schema, texts: dict[str, str]) -> list[Finding]:
    out: list[Finding] = []
    seen: set = set()

    def ev_line(file: str, line: int) -> str:
        return clip_line(_lineof(texts.get(file, ""), line))

    for fk in schema.fks:
        tab = schema.tables.get(fk.table)
        if tab is None or tab.managed == "prisma":
            continue
        key = ("PGL001", fk.table, tuple(fk.cols))
        if key in seen:
            continue
        seen.add(key)
        ok, partial = covered(schema, fk.table, fk.cols)
        if ok:
            continue
        cols = ", ".join(fk.cols)
        note = " (a partial index exists, but it cannot serve the foreign key checks)" if partial else ""
        idx = f"{short(fk.table)}_{'_'.join(fk.cols)}_idx"
        out.append(Finding(
            "PGL001", "medium",
            f"{fk.table}({cols}) references {fk.ref_table} but no index starts with {cols}{note}: each delete "
            f"from {fk.ref_table} scans {fk.table}, and joins on {cols} do too.",
            fk.file, fk.line, ev_line(fk.file, fk.line),
            extra={"fixkeys": ["sql"], "table": fk.table, "cols": fk.cols, "index": idx}))

    for tab in schema.tables.values():
        if tab.managed == "prisma" or tab.partition_child:
            continue
        cols = tab.columns
        if not tab.derived and cols and not tab.pk:
            uniq = any(i.unique and not i.partial and i.cols and all(
                c in cols and cols[c].not_null for c in i.cols) for i in schema.indexes_on(tab.name))
            if not uniq and ("PGL007", tab.name) not in seen:
                seen.add(("PGL007", tab.name))
                fk_cols = [c for f in schema.fks if f.table == tab.name for c in f.cols]
                out.append(Finding(
                    "PGL007", "medium",
                    f"Table {tab.name} has no primary key (and no unique NOT NULL column that could stand in for one).",
                    tab.file, tab.line, ev_line(tab.file, tab.line),
                    extra={"fixkeys": ["sql"], "table": tab.name, "cols": fk_cols if len(fk_cols) >= 2 else ["id"]}))
        if tab.pk and len(tab.pk) == 1 and tab.pk[0] in cols:
            c = cols[tab.pk[0]]
            if c.base in ("text", "varchar", "char") and c.default and UUID_GEN_RE.search(c.default):
                if ("PGL008", tab.name) not in seen:
                    seen.add(("PGL008", tab.name))
                    out.append(Finding(
                        "PGL008", "low",
                        f"{tab.name}.{c.name} is a {c.type} primary key filled with {c.default}: the same value "
                        f"as a native uuid would take 16 bytes instead of 37 in the key and every index and "
                        f"foreign key that copies it.",
                        c.file, c.line, ev_line(c.file, c.line),
                        extra={"fixkeys": ["sql"], "table": tab.name, "col": c.name}))
        for c in cols.values():
            if c.base == "float" and not c.array and is_money_name(c.name) and ("PGL009", tab.name, c.name) not in seen:
                seen.add(("PGL009", tab.name, c.name))
                out.append(Finding(
                    "PGL009", "medium",
                    f"{tab.name}.{c.name} looks like money but is {c.type} (binary floating point): amounts "
                    f"and sums will be off by fractions of a cent.",
                    c.file, c.line, ev_line(c.file, c.line), heuristic=True,
                    extra={"fixkeys": ["sql"], "table": tab.name, "col": c.name}))
        ts = [c for c in cols.values() if c.base == "timestamp"]
        if ts and ("PGL010", tab.name) not in seen:
            seen.add(("PGL010", tab.name))
            names = ", ".join(c.name for c in ts)
            out.append(Finding(
                "PGL010", "info",
                f"{tab.name}: {names} {'is' if len(ts) == 1 else 'are'} timestamp without time zone; "
                f"timestamptz stores an absolute instant in the same 8 bytes.",
                ts[0].file, ts[0].line, ev_line(ts[0].file, ts[0].line),
                extra={"fixkeys": ["sql"], "table": tab.name, "col": ts[0].name}))
    return out
