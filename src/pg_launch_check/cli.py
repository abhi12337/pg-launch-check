"""Command line interface.

  pg-launch-check scan [PATH ...]
  pg-launch-check rules
"""

from __future__ import annotations

import argparse
import os
import sys
import textwrap

from . import __version__
from .findings import RULES, SEVERITIES, SEVERITY_RANK, Finding
from .report import render_json, render_text, use_color
from .scanner import MAX_BYTES_DEFAULT, ScanResult, scan_path

EXIT_OK, EXIT_FINDINGS, EXIT_ERROR = 0, 1, 2


def _exit_code(findings: list[Finding], fail_on: str) -> int:
    if fail_on == "never":
        return EXIT_OK
    lim = SEVERITY_RANK[fail_on]
    return EXIT_FINDINGS if any(SEVERITY_RANK[f.severity] <= lim for f in findings) else EXIT_OK


def _filter_min(findings: list[Finding], min_sev: str) -> list[Finding]:
    lim = SEVERITY_RANK[min_sev]
    return [f for f in findings if SEVERITY_RANK[f.severity] <= lim]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pg-launch-check",
        description="Static PostgreSQL performance and safety lint for backend repos: SQL migrations (plain, "
                    "Supabase, Prisma, Drizzle), schema.prisma, and app code (Prisma, Drizzle, node-postgres, "
                    "postgres.js, SQLAlchemy, Django, psycopg, asyncpg). Reads files only: never connects to a "
                    "database, never runs your code, no network.",
        epilog="A clean report is not a guarantee. See 'Known limitations' in the README.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    s = sub.add_parser("scan", help="scan folders / files")
    s.add_argument("paths", nargs="*", default=None, help="folders or files to scan (default: .)")
    s.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                   help="glob of paths to skip (repeatable), e.g. --exclude 'scripts/*'")
    s.add_argument("--include-tests", action="store_true", help="also scan test / fixture files")
    s.add_argument("--disable", action="append", default=[], metavar="RULE",
                   help="ignore a rule id (repeatable), e.g. --disable PGL010")
    s.add_argument("--pg-version", type=int, default=14, metavar="N",
                   help="major PostgreSQL version the migrations run on (default: 14). Below 11, ADD COLUMN with "
                        "any DEFAULT rewrites the table")
    s.add_argument("--applied-up-to", metavar="MIGRATION",
                   help="migrations whose name sorts at or before this one are already applied: they still build "
                        "the schema, but lock-risk rules (PGL002-PGL006) are not reported for them")
    s.add_argument("--max-file-mb", type=float, default=MAX_BYTES_DEFAULT / 1024 / 1024,
                   help="skip files larger than this (default: 2)")
    s.add_argument("--json", action="store_true", help="print a JSON report instead of text")
    s.add_argument("--fail-on", choices=[*SEVERITIES, "never"], default="medium",
                   help="exit 1 if any finding is at or above this severity (default: medium)")
    s.add_argument("--min-severity", choices=SEVERITIES, default="info",
                   help="hide findings below this severity (default: info = show all)")
    s.add_argument("--no-color", action="store_true", help="disable ANSI colours")
    s.add_argument("--brief", action="store_true", help="omit the why/fix text")

    sub.add_parser("rules", help="list every rule with why it matters and how to fix it")
    return parser


def cmd_scan(args) -> int:
    paths = args.paths or ["."]
    for p in paths:
        if not os.path.exists(p):
            print(f"error: {p} does not exist", file=sys.stderr)
            return EXIT_ERROR
    unknown = [r for r in args.disable if r.upper() not in RULES]
    if unknown:
        print(f"error: unknown rule id(s): {', '.join(unknown)} (see `pg-launch-check rules`)", file=sys.stderr)
        return EXIT_ERROR
    if not 9 <= args.pg_version <= 30:
        print("error: --pg-version must be a PostgreSQL major version such as 13, 16 or 17", file=sys.stderr)
        return EXIT_ERROR
    max_bytes = int(args.max_file_mb * 1024 * 1024)
    results: list[ScanResult] = [scan_path(p, excludes=args.exclude, include_tests=args.include_tests,
                                           max_bytes=max_bytes, pg_version=args.pg_version,
                                           applied_up_to=args.applied_up_to) for p in paths]
    if len(results) > 1:   # make locations unambiguous
        for r, label in zip(results, paths):
            if os.path.isfile(label) or label == ".":
                continue
            prefix = label.replace("\\", "/").rstrip("/")
            for f in r.findings:
                f.file = f"{prefix}/{f.file}"
    disabled = {r.upper() for r in args.disable}
    findings = [f for r in results for f in r.findings if f.rule_id not in disabled]
    exit_code = _exit_code(findings, args.fail_on)
    shown = _filter_min(findings, args.min_severity)
    tot = {k: sum(getattr(r, k) for r in results) for k in
           ("files_scanned", "sql_files", "migration_files", "prisma_files", "py_files", "js_files", "tables",
            "foreign_keys")}
    if args.json:
        sys.stdout.write(render_json(shown, meta={
            "targets": [r.root for r in results], **tot, "pg_version": args.pg_version, "exit_code": exit_code,
        }))
    else:
        header = [f"Scanned {', '.join(paths)}",
                  f"{tot['files_scanned']} file(s): {tot['sql_files']} SQL ({tot['migration_files']} migrations), "
                  f"{tot['prisma_files']} Prisma schema, {tot['py_files']} Python, {tot['js_files']} JS/TS; "
                  f"{tot['tables']} table(s) in the schema"]
        if not tot["files_scanned"]:
            header.append("No SQL, Prisma, Python or JS/TS files found.")
        sys.stdout.write(render_text(shown, header=header, color=use_color(force=False if args.no_color else None),
                                     verbose_fix=not args.brief, footer=[f"Exit code: {exit_code}"]))
    return exit_code


def cmd_rules(_args) -> int:
    for r in RULES.values():
        tag = "  [heuristic]" if r.heuristic else ""
        print(f"{r.id}  {r.title}{tag}")
        print(textwrap.fill(r.why, 92, initial_indent="    ", subsequent_indent="    "))
        print("    Fix:")
        print(textwrap.indent(r.fix, "      "))
        print()
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
    args = build_parser().parse_args(argv)
    return {"scan": cmd_scan, "rules": cmd_rules}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
