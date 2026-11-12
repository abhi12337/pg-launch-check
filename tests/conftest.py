"""Shared helpers for pg-launch-check's tests."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from pg_launch_check.scanner import scan_path

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def make_repo(tmp_path):
    """make_repo({"rel/path": "content", ...}) -> Path. Content is dedented."""

    def _make(files: dict) -> Path:
        root = tmp_path / "repo"
        root.mkdir(exist_ok=True)
        for rel, content in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
        return root

    return _make


@pytest.fixture
def scan(make_repo):
    """scan({"rel": "content"}, **opts) -> sorted list of (rule_id, file, line, severity)."""

    def _scan(files: dict, **kw):
        root = make_repo(files)
        res = scan_path(str(root), **kw)
        return sorted((f.rule_id, f.file, f.line, f.severity) for f in res.findings)

    return _scan


@pytest.fixture
def scan_findings(make_repo):
    def _scan(files: dict, **kw):
        return scan_path(str(make_repo(files)), **kw).findings

    return _scan


def ids(findings) -> list[str]:
    return sorted(f[0] for f in findings)
