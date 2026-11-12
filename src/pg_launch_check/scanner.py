"""Walks a folder (or takes a single file), builds the schema from SQL migrations and
schema.prisma, then runs the application-code rules with that context.
Reads local files only. Never imports, runs or connects to anything it scans."""

from __future__ import annotations

import ast
import fnmatch
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import prisma_scan
from .appcommon import ProjectCtx
from .findings import Finding
from .js_scan import JsScan
from .py_scan import PyScan, collect_models
from .sql_scan import classify, scan_sql

MAX_BYTES_DEFAULT = 2 * 1024 * 1024
SKIP_DIRS = {"node_modules", ".git", ".hg", ".svn", "dist", "build", "out", ".next", ".nuxt", ".output", ".svelte-kit",
             ".venv", "venv", "env", "__pycache__", "site-packages", ".tox", ".nox", ".mypy_cache", ".pytest_cache",
             ".ruff_cache", "coverage", ".turbo", ".cache", ".idea", "target", "vendor", ".gradle", ".vercel",
             "generated"}
TEST_DIRS = {"test", "tests", "__tests__", "spec", "specs", "fixtures", "__fixtures__", "testdata", "e2e", "__mocks__"}
PY_EXT = {".py"}
JS_EXT = {".ts", ".mts", ".cts", ".js", ".mjs", ".cjs", ".tsx", ".jsx"}
CONFIG_NAMES = {"package.json", "vercel.json", "netlify.toml", "serverless.yml", "serverless.yaml", "template.yaml",
                "template.yml", "migration_lock.toml", "_journal.json", "requirements.txt", "pyproject.toml",
                "Pipfile", "requirements-prod.txt", "fly.toml", "Procfile"}
APP_SKIP_RE = re.compile(r"(?i)(^|/)(migrations?|migrate|alembic/versions|versions|seeds?|seeders)/")
POOLER_RE = re.compile(r"(?i)pooler\.supabase\.com|:6543\b|-pooler\.|pgbouncer=true|connection_limit=|prisma://|"
                       r"prisma\+postgres://|\.proxy-[a-z0-9]+\.|rds-proxy|POOLE[DR]_URL|attachDatabasePool|"
                       r"@neondatabase/serverless|@vercel/postgres|@prisma/extension-accelerate|hyperdrive")
SERVERLESS_DEPS = ("next", "@vercel/node", "@netlify/functions", "@types/aws-lambda", "aws-lambda", "serverless",
                   "sst", "@sveltejs/adapter-vercel", "@sveltejs/adapter-netlify")
IGNORE_RE = re.compile(r"pg-launch-check:\s*ignore\b([\w ,]*)", re.I)


@dataclass
class ScanResult:
    root: str
    findings: list[Finding] = field(default_factory=list)
    files_scanned: int = 0
    sql_files: int = 0
    migration_files: int = 0
    prisma_files: int = 0
    py_files: int = 0
    js_files: int = 0
    tables: int = 0
    foreign_keys: int = 0


def _is_test_file(name: str) -> bool:
    n = name.lower()
    return n.startswith("test_") or n == "conftest.py" or n.endswith((
        "_test.py", ".test.ts", ".test.js", ".spec.ts", ".spec.js", ".test.mjs", ".spec.mjs", ".test.tsx",
        ".spec.tsx", ".test.jsx", ".spec.jsx"))


def _read(p: Path, max_bytes: int) -> str | None:
    try:
        if p.stat().st_size > max_bytes:
            return None
        data = p.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:4096]:
        return None
    return data.decode("utf-8", errors="replace")


def _wanted(name: str) -> bool:
    low = name.lower()
    suffix = os.path.splitext(low)[1]
    if name.endswith((".d.ts", ".min.js")):
        return False
    return suffix in PY_EXT | JS_EXT | {".sql", ".prisma"} or name in CONFIG_NAMES or low.startswith(".env") or \
        low.startswith("sst.config")


def collect(target: str, excludes: list[str], include_tests: bool, max_bytes: int) -> tuple[str, dict[str, str]]:
    root = Path(target)
    files: dict[str, str] = {}
    if root.is_file():
        text = _read(root, max_bytes)
        if text is not None:
            files[root.name] = text
        return str(root.resolve().parent), files
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
        rel_dir = "" if rel_dir == "." else rel_dir + "/"
        keep = []
        for d in sorted(dirnames):
            if d in SKIP_DIRS or d.startswith(".") or (not include_tests and d.lower() in TEST_DIRS):
                continue
            if any(fnmatch.fnmatch(rel_dir + d, pat) or fnmatch.fnmatch(rel_dir + d + "/", pat) for pat in excludes):
                continue
            keep.append(d)
        dirnames[:] = keep
        for fn in sorted(filenames):
            rel = rel_dir + fn
            if not _wanted(fn):
                continue
            if any(fnmatch.fnmatch(rel, pat) for pat in excludes):
                continue
            if not include_tests and _is_test_file(fn):
                continue
            text = _read(Path(dirpath) / fn, max_bytes)
            if text is not None:
                files[rel] = text
    return str(root.resolve()), files


def scan_path(target: str, *, excludes: list[str] | None = None, include_tests: bool = False,
              max_bytes: int = MAX_BYTES_DEFAULT, pg_version: int = 14, applied_up_to: str | None = None) -> ScanResult:
    root, files = collect(target, excludes or [], include_tests, max_bytes)
    res = ScanResult(root)
    proj = ProjectCtx()

    # ---------------------------------------------------------- project facts
    prisma_roots: set[str] = set()
    drizzle_dirs: set[str] = set()
    non_pg_dirs: set[str] = set()
    for rel, text in files.items():
        name = rel.rsplit("/", 1)[-1]
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        if name == "migration_lock.toml":
            m = re.search(r'provider\s*=\s*"([^"]+)"', text)
            (prisma_roots if m and m.group(1) in ("postgresql", "postgres") else non_pg_dirs).add(parent)
        elif name == "_journal.json" and parent.endswith("meta"):
            d = parent[:-len("meta")].rstrip("/")
            try:
                dialect = json.loads(text).get("dialect", "postgresql")
            except (ValueError, AttributeError):
                dialect = "postgresql"
            (drizzle_dirs if dialect in ("postgresql", "pg", "postgres") else non_pg_dirs).add(d)
        elif name == "package.json":
            try:
                pkg = json.loads(text)
            except ValueError:
                continue
            deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
            if any(d in deps for d in SERVERLESS_DEPS):
                proj.serverless = True
            if "@prisma/client" in deps or "prisma" in deps:
                proj.prisma = True
            if "drizzle-orm" in deps:
                proj.drizzle = True
        elif name in ("vercel.json", "netlify.toml", "serverless.yml", "serverless.yaml") or \
                name.startswith("sst.config") or (name.startswith("template.y") and "AWS::Serverless" in text):
            proj.serverless = True
        if name.lower().startswith(".env") or name in ("fly.toml",):
            if POOLER_RE.search(text):
                proj.pooler_hint = True
        elif rel.endswith(tuple(JS_EXT)) and POOLER_RE.search(text):
            proj.pooler_hint = True
        if name in ("requirements.txt", "requirements-prod.txt", "pyproject.toml", "Pipfile") and \
                re.search(r"(?i)psycopg|asyncpg|postgres", text):
            proj.postgres_py = True
        if rel.endswith(".py") and re.search(r"(?i)\bpsycopg2?\b|\basyncpg\b|postgres(ql)?(\+\w+)?://|"
                                             r"django\.db\.backends\.postgresql|dialects\.postgresql", text):
            proj.postgres_py = True

    # ---------------------------------------------------------- schema.prisma
    prisma_files = {rel: t for rel, t in files.items() if rel.endswith(".prisma")}
    pres = prisma_scan.parse(prisma_files)
    res.prisma_files = len(prisma_files)
    if pres.postgres:
        proj.prisma = True
        if pres.direct_url:
            proj.pooler_hint = True
        for rel in prisma_files:
            d = rel.rsplit("/", 1)[0] if "/" in rel else ""
            prisma_roots.add((d + "/migrations").lstrip("/"))
            if d.endswith("/schema") or d == "schema":       # prismaSchemaFolder layout
                prisma_roots.add((d.rsplit("/", 1)[0] + "/migrations").lstrip("/") if "/" in d else "migrations")

    # ---------------------------------------------------------- SQL
    sql_files = []
    for rel, text in files.items():
        if rel.endswith(".sql"):
            f = classify(rel, text, prisma_roots, drizzle_dirs, non_pg_dirs)
            if f is not None:
                sql_files.append(f)
    sres = scan_sql(sql_files, pg_version=pg_version, applied_up_to=applied_up_to, prisma_managed=pres.postgres)
    res.sql_files = len(sql_files)
    res.migration_files = sum(1 for f in sql_files if f.migration)
    res.findings += sres.findings
    if sql_files:
        proj.postgres_py = True
    prisma_scan.check(pres, prisma_files, sres.schema)
    res.findings += pres.findings
    proj.trgm_cols = sres.trgm_cols | pres.trgm_cols
    proj.role_statement_timeout = sres.schema.role_statement_timeout
    res.tables = len([t for t in sres.schema.tables.values() if not t.partition_child and t.managed != "prisma"]) + \
        (len([m for m in pres.models.values() if not m.view]) if pres.postgres else 0)
    res.foreign_keys = len(sres.schema.fks)

    # ---------------------------------------------------------- application code
    py_trees = {}
    for rel, text in files.items():
        if rel.endswith(".py"):
            res.py_files += 1
            try:
                py_trees[rel] = ast.parse(text)
            except (SyntaxError, ValueError):
                continue
    for rel, tree in py_trees.items():
        if not APP_SKIP_RE.search(rel):
            res.findings += collect_models(rel, tree, files[rel], proj)
    for rel, tree in py_trees.items():
        if not APP_SKIP_RE.search(rel):
            res.findings += PyScan(rel, files[rel], tree, proj).run()
    for rel, text in files.items():
        if rel.endswith(tuple(JS_EXT)) and not rel.endswith((".d.ts", ".min.js")):
            res.js_files += 1
            if APP_SKIP_RE.search(rel) or re.search(r"(^|/)[\w.-]*\.config\.[cm]?[jt]s$", rel):
                continue
            res.findings += JsScan(rel, text, proj).run()
    res.files_scanned = res.sql_files + res.prisma_files + res.py_files + res.js_files

    # ---------------------------------------------------------- inline ignores + dedupe
    out, seen = [], set()
    for f in res.findings:
        key = (f.rule_id, f.file, f.line, f.message)
        if key in seen:
            continue
        seen.add(key)
        if f.file and f.line and _ignored(files.get(f.file, ""), f.line, f.rule_id):
            continue
        out.append(f)
    res.findings = out
    return res


def _ignored(text: str, line: int, rule: str) -> bool:
    lines = text.splitlines()
    cands = []
    if 0 < line <= len(lines):
        cands.append(lines[line - 1])
    if 1 < line <= len(lines) + 1:
        prev = lines[line - 2].strip()
        if prev.startswith(("--", "#", "//", "/*", "*")):
            cands.append(prev)
    for c in cands:
        m = IGNORE_RE.search(c)
        if m:
            ids = re.findall(r"PGL\d{3}", m.group(1).upper())
            if not ids or rule in ids:
                return True
    return False
