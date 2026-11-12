"""Shared pieces for the application-code scanners (Python and JavaScript / TypeScript):
project-wide context and checks on SQL text found in string literals."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SQL_START_RE = re.compile(r"(?is)^\s*\(?\s*(select|insert|update|delete|with|merge|create|alter|drop|truncate|"
                          r"copy|call)\b")
SELECT_STAR_RE = re.compile(r"(?is)\bselect\s+(?:distinct\s+)?(?:[a-z_][\w]*\.)?\*\s*(?:,|\bfrom\b)")
EXISTS_STAR_RE = re.compile(r"(?is)\bexists\s*\(\s*select\s+(?:[a-z_][\w]*\.)?\*")
LIKE_COL_RE = re.compile(r"""(?is)(?:"?([a-z_][\w]*)"?\s*(?:::\s*text\s*)?)\s+(?:not\s+)?i?like\s+"""
                         r"""(?:'%|concat\s*\(\s*'%'|'%'\s*\|\|)""")
LIKE_PARAM_RE = re.compile(r"""(?is)"?([a-z_][\w]*)"?\s+(?:not\s+)?i?like\s+(\$\d+|\?|%s|%\(\w+\)s|:\w+|\$X|\{\})""")
OFFSET_PARAM_RE = re.compile(r"(?is)\boffset\s+(\$\d+|\?|%s|%\(\w+\)s|:\w+|\$X|\{\})")
SMALL_TABLE_RE = re.compile(r"(?i)^(countr(y|ies)|currenc(y|ies)|languages?|locales?|roles?|permissions?|plans?|"
                            r"settings?|configs?|configurations?|feature_?flags?|time_?zones?|states?|tiers?|"
                            r"enums?|lookups?)$")
UNIQUE_KEY_RE = re.compile(r"(?is)\bwhere\b.*?\b(id|uuid|pk|email|slug|token|username|handle|code|key)\b\s*=")


def looks_sql(text: str) -> bool:
    return bool(SQL_START_RE.match(text or ""))


def select_star(text: str) -> bool:
    if not looks_sql(text):
        return False
    t = EXISTS_STAR_RE.sub("exists (select 1", text)
    return bool(SELECT_STAR_RE.search(t))


def norm_col(name: str) -> str:
    return name.lower().replace("_", "")


def from_tables(text: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r'(?i)\b(?:from|join)\s+"?(?:\w+"?\."?)?([A-Za-z_]\w*)', text)]


def unbounded_select(text: str) -> bool:
    """A SELECT that returns all matching rows: no LIMIT / FETCH, not aggregate-only, not a lookup by key."""
    t = text.strip()
    if not re.match(r"(?is)^\(?\s*(select|with)\b", t):
        return False
    if not re.search(r"(?i)\bfrom\b", t):
        return False
    if re.search(r"(?i)\blimit\b|\bfetch\s+(first|next)\b", t):
        return False
    if re.match(r"(?is)^select\s+(count|sum|avg|min|max|exists|bool_or|bool_and|array_agg|json_agg|jsonb_agg|"
                r"string_agg)\s*\(", t) and not re.search(r"(?i)\bgroup\s+by\b", t):
        return False
    if UNIQUE_KEY_RE.search(t) or re.search(r"(?i)=\s*any\s*\(|\bin\s*\(", t):
        return False
    tables = from_tables(t)
    if tables and all(SMALL_TABLE_RE.match(x) for x in tables):
        return False
    return True


def like_columns(text: str) -> list[str]:
    return [m.group(1) for m in LIKE_COL_RE.finditer(text)]


@dataclass
class ProjectCtx:
    trgm_cols: set = field(default_factory=set)
    role_statement_timeout: bool = False
    serverless: bool = False
    pooler_hint: bool = False
    prisma: bool = False
    drizzle: bool = False
    postgres_py: bool = False
    django_models: dict = field(default_factory=dict)    # Model -> {"fk": set, "rev": set, "file": str}
    sqla_models: dict = field(default_factory=dict)      # Model -> {"rels": {name: target}}
    serverless_reported: bool = False

    def has_trgm(self, col: str | None) -> bool:
        return bool(col) and norm_col(col) in self.trgm_cols


def quote_ident(name: str) -> str:
    return name if re.fullmatch(r"[a-z_][a-z0-9_]*", name) else '"' + name.replace('"', '""') + '"'


def trgm_extra(col: str, sql_text: str) -> dict:
    """Fix-text names for a trigram index on `col`, with the table taken from the query."""
    tables = from_tables(sql_text)
    table = tables[0] if tables else None
    return {"col": quote_ident(col), "table": quote_ident(table) if table else None,
            "index": f"{(table or 'your_table').lower()}_{col.lower()}_trgm_idx"}
