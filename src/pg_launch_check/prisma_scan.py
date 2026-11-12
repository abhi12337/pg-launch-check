"""schema.prisma reader: models, fields, relations and indexes (PostgreSQL datasources only).

Prisma does not create an index for a relation's scalar field(s) (only @id / @unique /
@@id / @@unique / @@index do), so PGL001 is checked here, at the relation field, with an
@@index fix. Custom indexes created in Prisma migration.sql files are taken into account."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .findings import Finding, clip_line
from .schema import Schema
from .sql_scan import covered, is_money_name

BLOCK_RE = re.compile(r"^\s*(model|view|enum|type|datasource|generator)\s+(\w+)\s*\{", re.M)
FIELD_RE = re.compile(r"^\s*(\w+)\s+([\w.]+)(\[\])?(\?)?\s*(.*)$")
LIST_RE = re.compile(r"\[([^\]]*)\]")
SCALARS = {"String", "Boolean", "Int", "BigInt", "Float", "Decimal", "DateTime", "Json", "Bytes", "Unsupported"}


@dataclass
class PField:
    name: str
    type: str
    line: int
    attrs: str
    list: bool = False
    optional: bool = False

    @property
    def db_name(self) -> str:
        m = re.search(r'@map\(\s*(?:name\s*:\s*)?"([^"]+)"', self.attrs)
        return m.group(1) if m else self.name


@dataclass
class PModel:
    name: str
    file: str
    line: int
    fields: dict = field(default_factory=dict)
    block_attrs: list = field(default_factory=list)     # (line, text)
    view: bool = False

    @property
    def db_name(self) -> str:
        for _, a in self.block_attrs:
            m = re.match(r'@@map\(\s*(?:name\s*:\s*)?"([^"]+)"', a)
            if m:
                return m.group(1)
        return self.name


@dataclass
class PrismaResult:
    postgres: bool = False
    direct_url: bool = False
    relation_mode_prisma: bool = False
    models: dict = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    trgm_cols: set = field(default_factory=set)


def _strip_comment(line: str) -> str:
    out, q = [], False
    i = 0
    while i < len(line):
        c = line[i]
        if c == '"' and (i == 0 or line[i - 1] != "\\"):
            q = not q
        if not q and line.startswith("//", i):
            break
        out.append(c)
        i += 1
    return "".join(out).rstrip()


def _list_fields(text: str) -> list[str]:
    """[a, b(sort: Desc), c(ops: raw("gin_trgm_ops"))] -> ['a', 'b', 'c']"""
    m = LIST_RE.search(text)
    if not m:
        return []
    inner = m.group(1)
    names, depth, cur = [], 0, ""
    for ch in inner:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            names.append(cur)
            cur = ""
            continue
        cur += ch
    names.append(cur)
    return [re.match(r"\s*(\w+)", n).group(1) for n in names if re.match(r"\s*(\w+)", n)]


def parse(files: dict[str, str]) -> PrismaResult:
    res = PrismaResult()
    for rel, text in files.items():
        lines = text.splitlines()
        for m in BLOCK_RE.finditer(text):
            kind, name = m.group(1), m.group(2)
            start_line = text.count("\n", 0, m.start(1)) + 1
            depth, k = 1, m.end()
            while k < len(text) and depth:
                if text[k] == "{":
                    depth += 1
                elif text[k] == "}":
                    depth -= 1
                k += 1
            end_line = text.count("\n", 0, k) + 1
            body = lines[start_line:end_line - 1]
            if kind == "datasource":
                src = "\n".join(_strip_comment(x) for x in body)
                pm = re.search(r'provider\s*=\s*"([^"]+)"', src)
                if pm and pm.group(1) in ("postgresql", "postgres"):
                    res.postgres = True
                if re.search(r"^\s*directUrl\s*=", src, re.M):
                    res.direct_url = True
                if re.search(r'relationMode\s*=\s*"prisma"', src):
                    res.relation_mode_prisma = True
                continue
            if kind not in ("model", "view"):
                continue
            model = PModel(name, rel, start_line, view=kind == "view")
            for off, raw in enumerate(body):
                line = _strip_comment(raw).strip()
                ln = start_line + 1 + off
                if not line:
                    continue
                if line.startswith("@@"):
                    model.block_attrs.append((ln, line))
                    continue
                fm = FIELD_RE.match(line)
                if fm:
                    model.fields[fm.group(1)] = PField(fm.group(1), fm.group(2), ln, fm.group(5) or "",
                                                       bool(fm.group(3)), bool(fm.group(4)))
            res.models[name] = model
    return res


def check(res: PrismaResult, texts: dict[str, str], sql_schema: Schema | None) -> None:
    if not res.postgres:
        return

    def ev(file: str, line: int) -> str:
        ls = texts.get(file, "").splitlines()
        return clip_line(ls[line - 1]) if 0 < line <= len(ls) else ""

    for model in res.models.values():
        if model.view:
            continue
        idx_lists: list[list[str]] = []
        has_id = False
        for f in model.fields.values():
            if re.search(r"@id\b", f.attrs):
                idx_lists.append([f.name])
                has_id = True
            elif re.search(r"@unique\b", f.attrs):
                idx_lists.append([f.name])
        for _, a in model.block_attrs:
            am = re.match(r"@@(id|unique|index)\s*\(", a)
            if am:
                idx_lists.append(_list_fields(a))
                if am.group(1) == "id":
                    has_id = True
                if "_trgm_ops" in a:
                    for n in _list_fields(a):
                        fld = model.fields.get(n)
                        res.trgm_cols.add((fld.db_name if fld else n).lower().replace("_", ""))
        # PGL001: relation scalar fields without an index
        for f in model.fields.values():
            rm = re.search(r"@relation\((.*)\)", f.attrs)
            if not rm or "fields" not in rm.group(1):
                continue
            fm = re.search(r"fields\s*:\s*(\[[^\]]*\])", rm.group(1))
            if not fm:
                continue
            fields = _list_fields(fm.group(1))
            if not fields:
                continue
            n = len(fields)
            if any(len(lst) >= n and set(lst[:n]) == set(fields) for lst in idx_lists):
                continue
            if sql_schema is not None:
                db_cols = [model.fields[x].db_name if x in model.fields else x for x in fields]
                if covered(sql_schema, model.db_name, db_cols)[0]:
                    continue
            how = ("relationMode = \"prisma\" creates no foreign keys and no indexes" if res.relation_mode_prisma
                   else "Prisma does not index relation fields")
            res.findings.append(Finding(
                "PGL001", "medium",
                f"{model.name}.{f.name} is a relation on {', '.join(fields)} but no @@index, @@id or @unique starts "
                f"with {', '.join(fields)} ({how}): each delete from {f.type} and every join on it scans "
                f"{model.db_name}.",
                model.file, f.line, ev(model.file, f.line),
                extra={"fixkeys": ["prisma"], "model": model.name, "fields": fields, "table": model.db_name}))
        if not has_id:
            res.findings.append(Finding(
                "PGL007", "medium",
                f"Model {model.name} has no @id or @@id, so table {model.db_name} has no primary key.",
                model.file, model.line, ev(model.file, model.line),
                extra={"fixkeys": ["prisma"], "model": model.name,
                       "fields": [x.name for x in model.fields.values() if re.search(r"@unique\b", x.attrs)][:2]
                       or ["aId", "bId"]}))
        for f in model.fields.values():
            if re.search(r"@id\b", f.attrs) and f.type == "String" and "@db.Uuid" not in f.attrs and \
                    re.search(r'@default\(\s*(uuid\(\s*\d*\s*\)|dbgenerated\(\s*"[^"]*uuid)', f.attrs):
                res.findings.append(Finding(
                    "PGL008", "low",
                    f"{model.name}.{f.name} holds UUIDs but is stored as text (no @db.Uuid): 37 bytes instead of 16 "
                    f"in the key and in every index and foreign key that copies it.",
                    model.file, f.line, ev(model.file, f.line),
                    extra={"fixkeys": ["prisma"], "model": model.name, "field": f.name}))
            if f.type == "Float" and not f.list and is_money_name(f.name):
                res.findings.append(Finding(
                    "PGL009", "medium",
                    f"{model.name}.{f.name} looks like money but is Float (double precision): amounts and sums will "
                    f"be off by fractions of a cent.",
                    model.file, f.line, ev(model.file, f.line), heuristic=True,
                    extra={"fixkeys": ["prisma"], "model": model.name, "field": f.name}))
        ts = [f for f in model.fields.values() if f.type == "DateTime" and not re.search(
            r"@db\.(Timestamptz|Date|Time|Timetz)\b", f.attrs)]
        if ts:
            names = ", ".join(f.name for f in ts)
            res.findings.append(Finding(
                "PGL010", "info",
                f"{model.name}: {names} {'is' if len(ts) == 1 else 'are'} DateTime without @db.Timestamptz, which "
                f"Prisma stores as timestamp(3) without time zone.",
                model.file, ts[0].line, ev(model.file, ts[0].line),
                extra={"fixkeys": ["prisma"], "model": model.name, "field": ts[0].name}))
