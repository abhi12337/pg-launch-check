# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-11-11

First release.

### Added

- `pg-launch-check scan [PATH ...]`: static scan of folders or single files. Never connects to a
  database, never imports or runs the scanned code; no network access.
- `pg-launch-check rules`: every rule with why it matters and its fix for each stack.
- Schema replay from SQL migrations in order (plain `migrations/` folders, numbered and Flyway
  names, `*.up.sql`, Supabase, Prisma Migrate, Drizzle Kit, statements inside `DO` blocks) and
  schema dumps; `schema.prisma` reader; SQLAlchemy and Django model reader.
- App-code analysis: JavaScript / TypeScript via a lexer (Prisma Client, Drizzle, node-postgres,
  postgres.js, Knex raw SQL) and Python via `ast` (SQLAlchemy, Django ORM, psycopg / psycopg2,
  psycopg_pool, asyncpg).
- Rules:
  - PGL001 foreign key column without an index (medium)
  - PGL002 `CREATE INDEX` without `CONCURRENTLY` on an existing table (medium)
  - PGL003 constraint added with a full check while holding a lock (medium; high for
    `UNIQUE` / `PRIMARY KEY` without `USING INDEX`)
  - PGL004 table rewrite under `ACCESS EXCLUSIVE`: type changes, volatile defaults, serial /
    identity / stored generated columns (high)
  - PGL005 `NOT NULL` added to an existing table (medium)
  - PGL006 migration without `lock_timeout` (low)
  - PGL007 table without a primary key (medium)
  - PGL008 UUID primary key stored as text (low)
  - PGL009 money stored as floating point (medium, heuristic)
  - PGL010 `timestamp` without time zone (info)
  - PGL011 SQL built from strings, SQL injection (high)
  - PGL012 database client or pool created per request (high; medium for a single connection)
  - PGL013 query inside a loop over rows, N+1, including Django and SQLAlchemy lazy loading
    (medium, heuristic)
  - PGL014 unbounded query in a request handler (low, heuristic)
  - PGL015 `OFFSET` pagination (info)
  - PGL016 `SELECT *` in application code (low)
  - PGL017 leading-wildcard `LIKE` / `ILIKE` without a trigram index (low, heuristic)
  - PGL018 connection pool size left at the driver default (low)
  - PGL019 no statement timeout on the database client (low)
  - PGL020 serverless functions connecting without a pooler hint (info)
- Text and JSON reports, a stack-specific fix for every finding, `--fail-on` (default `medium`),
  `--min-severity`, `--disable`, `--exclude`, `--include-tests`, `--pg-version`,
  `--applied-up-to`, `--brief`, `--no-color`, `pg-launch-check: ignore [RULE ...]` comments,
  exit codes `0` / `1` / `2`.
- Demo monorepo generator (`examples/make_demo.py`) and a GitHub Actions example.
