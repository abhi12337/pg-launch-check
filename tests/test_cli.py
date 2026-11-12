import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from pg_launch_check.cli import main
from pg_launch_check.findings import PLACEHOLDER_RE, RULES, SEVERITIES

FIX = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).parent.parent
VULNERABLE = ["sql_vulnerable", "prisma_vulnerable", "node_vulnerable", "py_vulnerable"]
CLEAN = ["sql_clean", "prisma_clean", "node_clean", "py_clean"]


def run(capsys, *argv):
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def rules_in(capsys, name, *extra):
    _, out, _ = run(capsys, "scan", str(FIX / name), "--json", *extra)
    return {f["rule_id"] for f in json.loads(out)["findings"]}


# ---------------------------------------------------------------- fixtures
@pytest.mark.parametrize("name", CLEAN)
def test_clean_fixtures_have_no_findings(capsys, name):
    code, out, _ = run(capsys, "scan", str(FIX / name), "--json", "--fail-on", "info")
    doc = json.loads(out)
    assert doc["findings"] == [] and code == 0


def test_vulnerable_fixtures_cover_every_rule(capsys):
    seen = set()
    for name in VULNERABLE:
        seen |= rules_in(capsys, name)
    assert seen == set(RULES)


def test_expected_rules_per_fixture(capsys):
    assert rules_in(capsys, "sql_vulnerable") == {"PGL001", "PGL002", "PGL003", "PGL004", "PGL005", "PGL006",
                                                  "PGL007", "PGL008", "PGL009", "PGL010"}
    assert rules_in(capsys, "prisma_vulnerable") == {"PGL001", "PGL002", "PGL005", "PGL006", "PGL008", "PGL009",
                                                     "PGL010", "PGL011", "PGL012", "PGL013", "PGL014", "PGL015",
                                                     "PGL016", "PGL017", "PGL020"}
    assert rules_in(capsys, "node_vulnerable") == {"PGL001", "PGL002", "PGL006", "PGL010", "PGL011", "PGL012",
                                                   "PGL013", "PGL014", "PGL015", "PGL016", "PGL017", "PGL018",
                                                   "PGL019"}
    assert rules_in(capsys, "py_vulnerable") == {"PGL001", "PGL009", "PGL010", "PGL011", "PGL012", "PGL013",
                                                 "PGL014", "PGL015", "PGL016", "PGL017", "PGL018", "PGL019"}


# ---------------------------------------------------------------- reports
def test_text_report(capsys):
    code, out, _ = run(capsys, "scan", str(FIX / "sql_vulnerable"), "--no-color")
    assert code == 1
    assert "HIGH     PGL004" in out and "20250301000000_order_tracking.sql:5" in out
    assert "why:" in out and "fix:" in out and "CREATE INDEX CONCURRENTLY" in out
    assert "Exit code: 1" in out and "4 table(s) in the schema" in out


def test_json_report_shape(capsys):
    code, out, _ = run(capsys, "scan", str(FIX / "node_vulnerable"), "--json")
    doc = json.loads(out)
    assert code == 1 and doc["exit_code"] == 1 and doc["tool"] == "pg-launch-check"
    assert set(doc["summary"]) == set(SEVERITIES)
    assert doc["migration_files"] == 2 and doc["tables"] == 2 and doc["pg_version"] == 14
    first = doc["findings"][0]
    assert first["severity"] == "high"
    assert {"rule_id", "severity", "message", "file", "line", "evidence", "title", "why", "fix", "heuristic"} <= set(first)
    assert "fixkeys" not in json.dumps(doc)


@pytest.mark.parametrize("name", VULNERABLE)
def test_every_finding_has_location_evidence_why_and_filled_fix(capsys, name):
    _, out, _ = run(capsys, "scan", str(FIX / name), "--json")
    for f in json.loads(out)["findings"]:
        assert f["file"] and f["line"] and f["evidence"] and f["why"] and f["fix"], f
        assert not PLACEHOLDER_RE.search(f["fix"]), f


def test_default_fail_on_is_medium(capsys, tmp_path):
    (tmp_path / "db.ts").write_text('import { Pool } from "pg";\nexport const pool = new Pool();\n')   # low only
    assert run(capsys, "scan", str(tmp_path))[0] == 0
    assert run(capsys, "scan", str(tmp_path), "--fail-on", "low")[0] == 1
    (tmp_path / "001_init.sql").write_text("create table u (id int primary key);\n"
                                           "create table p (id int primary key, u int references u(id));\n")
    assert run(capsys, "scan", str(tmp_path))[0] == 1                    # PGL001 is medium
    assert run(capsys, "scan", str(tmp_path), "--fail-on", "high")[0] == 0
    assert run(capsys, "scan", str(tmp_path), "--fail-on", "never")[0] == 0


def test_min_severity_and_disable(capsys):
    _, out, _ = run(capsys, "scan", str(FIX / "sql_vulnerable"), "--json", "--min-severity", "high")
    assert {f["severity"] for f in json.loads(out)["findings"]} == {"high"}
    code, out, _ = run(capsys, "scan", str(FIX / "sql_vulnerable"), "--json", "--disable", "PGL001",
                       "--disable", "pgl002", "--disable", "PGL003", "--disable", "PGL004", "--disable", "PGL005",
                       "--disable", "PGL007", "--disable", "PGL009")
    doc = json.loads(out)
    assert {f["rule_id"] for f in doc["findings"]} == {"PGL006", "PGL008", "PGL010"}
    assert code == 0


def test_brief_omits_why_and_fix(capsys):
    _, out, _ = run(capsys, "scan", str(FIX / "py_vulnerable"), "--brief", "--no-color")
    assert "why:" not in out and "fix:" not in out and "PGL011" in out


def test_usage_errors(capsys, tmp_path):
    code, _, err = run(capsys, "scan", str(tmp_path / "nope"))
    assert code == 2 and "does not exist" in err
    code, _, err = run(capsys, "scan", str(tmp_path), "--disable", "PGL999")
    assert code == 2 and "unknown rule" in err
    code, _, err = run(capsys, "scan", str(tmp_path), "--pg-version", "3")
    assert code == 2
    with pytest.raises(SystemExit) as e:
        main(["scan", "--fail-on", "sometimes"])
    assert e.value.code == 2


def test_applied_up_to_option(capsys):
    _, out, _ = run(capsys, "scan", str(FIX / "sql_vulnerable"), "--json", "--applied-up-to", "20250301000000")
    got = {f["rule_id"] for f in json.loads(out)["findings"]}
    assert not got & {"PGL002", "PGL003", "PGL004", "PGL005", "PGL006"}
    assert "PGL001" in got


def test_scan_single_files(capsys):
    code, out, _ = run(capsys, "scan", str(FIX / "node_vulnerable" / "src" / "server.ts"), "--json")
    doc = json.loads(out)
    assert code == 1 and doc["files_scanned"] == 1
    assert {f["rule_id"] for f in doc["findings"]} >= {"PGL011", "PGL012", "PGL013"}


def test_multiple_paths_prefix_locations(capsys):
    _, out, _ = run(capsys, "scan", str(FIX / "sql_vulnerable"), str(FIX / "node_vulnerable"), "--json")
    files = {f["file"] for f in json.loads(out)["findings"]}
    assert any(f.endswith("sql_vulnerable/supabase/migrations/20250101000000_init.sql") for f in files)
    assert any(f.endswith("node_vulnerable/src/server.ts") for f in files)


def test_tests_folders_skipped_unless_included(capsys, tmp_path):
    code = (FIX / "node_vulnerable" / "src" / "server.ts").read_text()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "server.ts").write_text(code)
    (tmp_path / "server.test.ts").write_text(code)
    _, out, _ = run(capsys, "scan", str(tmp_path), "--json")
    assert json.loads(out)["findings"] == []
    _, out, _ = run(capsys, "scan", str(tmp_path), "--json", "--include-tests")
    assert len(json.loads(out)["findings"]) > 10


def test_exclude_and_skipped_dirs(capsys, tmp_path):
    for d in ("scripts", "node_modules/pkg", ".venv/lib", "dist"):
        (tmp_path / d).mkdir(parents=True)
        (tmp_path / d / "x.ts").write_text("export const q = 'SELECT * FROM users';\n")
    _, out, _ = run(capsys, "scan", str(tmp_path), "--exclude", "scripts/*", "--json")
    assert json.loads(out)["files_scanned"] == 0


def test_rules_lists_every_rule(capsys):
    code, out, _ = run(capsys, "rules")
    assert code == 0
    for rid, r in RULES.items():
        assert rid in out and r.title in out
    assert "[heuristic]" in out


def test_every_rule_has_title_why_and_fixes():
    for r in RULES.values():
        assert r.title and len(r.why) > 80 and r.fixes
        for text in r.fixes.values():
            assert len(text) > 30


def test_concurrently_fixes_mention_transactions():
    for rid in ("PGL001", "PGL002"):
        assert any("transaction" in t for t in RULES[rid].fixes.values())


def test_module_entry_point():
    env = {"PYTHONPATH": str(ROOT / "src")}
    if sys.platform == "win32":
        env.update({"SYSTEMROOT": "C:\\Windows", "PATH": ""})
    res = subprocess.run([sys.executable, "-m", "pg_launch_check", "scan", str(FIX / "sql_clean"), "--brief"],
                         capture_output=True, text=True, env=env)
    assert res.returncode == 0, res.stderr
    assert "No findings" in res.stdout


def test_no_network_or_database_imports():
    """The scanner must never be able to contact anything."""
    for p in (ROOT / "src" / "pg_launch_check").glob("*.py"):
        text = p.read_text(encoding="utf-8")
        for mod in ("urllib.request", "http.client", "socket", "requests", "httpx", "subprocess", "psycopg",
                    "psycopg2", "asyncpg", "pg8000", "sqlite3"):
            assert not re.search(rf"^\s*(import {re.escape(mod)}\b|from {re.escape(mod)} import)", text, re.M), \
                (p.name, mod)


def test_unparseable_python_file_is_warned_not_silently_clean(capsys, tmp_path):
    (tmp_path / "db.py").write_text("import psycopg\nconn = psycopg.connect()\nx = = 1\n")
    (tmp_path / "migrations").mkdir()
    (tmp_path / "migrations" / "0001.py").write_text("x = = 1\n")      # not scanned anyway: not reported
    code, out, err = run(capsys, "scan", str(tmp_path), "--no-color")
    assert code == 0                                  # exit code still depends on findings only
    assert "warning: db.py: syntax error line 3; file not scanned" in err and "0001.py" not in err
    assert "1 file(s) could not be parsed" in out
    code, out, _ = run(capsys, "scan", str(tmp_path), "--json")
    assert json.loads(out)["skipped"] == [{"path": "db.py", "reason": "syntax error line 3"}]
