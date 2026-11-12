"""SQL migrations: schema replay, foreign key indexes, lock risks and schema design rules."""

from conftest import ids

from pg_launch_check.schema import Ctx, Schema
from pg_launch_check.sqllex import split

M1 = "db/migrations/001_init.sql"
M2 = "db/migrations/002_change.sql"

BASE = """
create table users (id bigint generated always as identity primary key, email text not null unique);
create table posts (
  id bigint generated always as identity primary key,
  user_id bigint not null references users (id),
  title text not null,
  body text,
  score int
);
create index posts_user_id_idx on posts (user_id);
"""


def run(scan, change: str, **kw):
    return scan({M1: BASE, M2: change}, **kw)


# ---------------------------------------------------------------- lexer
def test_splitter_handles_strings_comments_dollar_quotes():
    sql = """
    -- a comment; with a semicolon
    create table a (x text default 'it''s; fine');
    /* block ; /* nested ; */ still comment */
    create function f() returns int language sql as $body$ select 1; select 2; $body$;
    insert into a values (E'semi\\'; colon');
    """
    stmts = split(sql).stmts
    assert [s.words(2)[:2] for s in stmts] == [["create", "table"], ["create", "function"], ["insert", "into"]]


def test_do_block_statements_are_read():
    sql = """
    create table t (id int primary key, ref int);
    DO $$ BEGIN
      ALTER TABLE "t" ADD CONSTRAINT "t_ref_fk" FOREIGN KEY ("ref") REFERENCES "t"("id");
    EXCEPTION WHEN duplicate_object THEN null;
    END $$;
    DO $$
    BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 't_ref_idx') THEN
        CREATE INDEX t_ref_idx ON t (ref);
      END IF;
    END $$;
    """
    schema = Schema()
    for st in split(sql).stmts:
        schema.apply(st, Ctx("x.sql", False, text=sql))
    assert [(f.table, f.cols, f.ref_table) for f in schema.fks] == [("t", ["ref"], "t")]
    assert "t_ref_idx" in schema.indexes


# ---------------------------------------------------------------- PGL001
def test_fk_without_index(scan):
    res = scan({M1: """
        create table users (id bigint primary key);
        create table orders (id bigint primary key, user_id bigint references users(id));
        """})
    assert ids(res) == ["PGL001"]
    assert res[0][1:3] == (M1, 2)


def test_fk_covered_by_leading_or_composite_index(scan):
    res = scan({M1: """
        create table users (id bigint primary key);
        create table teams (id bigint primary key);
        create table a (id bigint primary key, user_id bigint references users(id));
        create index on a (user_id, id);
        create table m (team_id bigint not null references teams(id), user_id bigint not null references users(id),
                        primary key (user_id, team_id));
        create index m_team on m (team_id);
        create table c (x bigint, y bigint, id bigint primary key, unique (y, x));
        alter table c add constraint c_fk foreign key (x, y) references c2 (a, b);
        """})
    assert res == []


def test_fk_not_covered_by_non_leading_partial_or_expression_index(scan):
    res = scan({M1: """
        create table users (id bigint primary key, email text);
        create table a (id bigint primary key, user_id bigint references users(id), created_at timestamptz);
        create index on a (created_at, user_id);
        create table b (id bigint primary key, user_id bigint references users(id), deleted boolean);
        create index on b (user_id) where not deleted;
        create table c (id bigint primary key, user_id bigint references users(id));
        create index on c ((user_id + 0));
        """})
    assert [r[0] for r in res] == ["PGL001", "PGL001", "PGL001"]


def test_fk_resolved_across_migrations_in_order(scan):
    res = scan({
        M1: "create table users (id bigint primary key);\ncreate table a (id bigint primary key, user_id bigint);\n",
        M2: "set lock_timeout = '5s';\nalter table a add constraint a_user_fk foreign key (user_id) references users(id) not valid;\n",
        "db/migrations/003_idx.sql": "create index concurrently a_user_idx on a (user_id);\n",
    })
    assert res == []


def test_dropped_index_and_renamed_column_are_tracked(scan):
    res = scan({
        M1: "create table users (id bigint primary key);\n"
            "create table a (id bigint primary key, owner bigint references users(id));\n"
            "create index a_owner_idx on a (owner);\n",
        M2: "set lock_timeout = '5s';\nalter table a rename column owner to owner_id;\ndrop index a_owner_idx;\n",
    })
    assert ids(res) == ["PGL001"]


def test_drop_table_removes_its_foreign_keys(scan):
    res = scan({
        M1: "create table users (id bigint primary key);\ncreate table a (id bigint primary key, u bigint references users(id));\n",
        M2: "drop table a;\n",
    })
    assert res == []


# ---------------------------------------------------------------- PGL002
def test_create_index_on_existing_table(scan):
    res = run(scan, "set lock_timeout = '5s';\ncreate index posts_title_idx on posts (title);\n")
    assert res == [("PGL002", M2, 2, "medium")]


def test_create_index_in_same_migration_or_concurrently_is_fine(scan):
    res = scan({M1: BASE + "create index posts_title_idx on posts (title);\n",
                M2: "create index concurrently posts_body_idx on posts (body);\n"})
    assert res == []


def test_create_index_fix_mentions_transaction_and_tool(scan_findings):
    fs = scan_findings({
        "prisma/schema.prisma": 'datasource db {\n  provider = "postgresql"\n  url = env("DATABASE_URL")\n}\n'
                                "model A {\n  id Int @id\n  t String\n  @@index([t])\n}\n",
        "prisma/migrations/migration_lock.toml": 'provider = "postgresql"\n',
        "prisma/migrations/1_init/migration.sql": 'CREATE TABLE "A" ("id" INTEGER NOT NULL, "t" TEXT NOT NULL, '
                                                  'CONSTRAINT "A_pkey" PRIMARY KEY ("id"));\n',
        "prisma/migrations/2_idx/migration.sql": 'CREATE INDEX "A_t_idx" ON "A"("t");\n',
        "drizzle/meta/_journal.json": '{"dialect": "postgresql", "entries": []}',
        "drizzle/0000_a.sql": 'CREATE TABLE "b" ("id" serial PRIMARY KEY NOT NULL, "t" text);\n',
        "drizzle/0001_b.sql": 'CREATE INDEX "b_t_idx" ON "b" ("t");\n',
    })
    fixes = {f.file: f.fix for f in fs if f.rule_id == "PGL002"}
    prisma_fix = fixes["prisma/migrations/2_idx/migration.sql"]
    assert "CREATE INDEX CONCURRENTLY" in prisma_fix and "transaction block" in prisma_fix
    assert "--create-only" in prisma_fix
    assert "migrate() applies pending migrations inside a transaction" in fixes["drizzle/0001_b.sql"]


def test_partitioned_parent_gets_partition_fix(scan_findings):
    fs = scan_findings({
        M1: "create table ev (id bigint, at timestamptz, primary key (id, at)) partition by range (at);\n",
        M2: "set lock_timeout='1s';\ncreate index ev_at on ev (at);\n"})
    f = next(f for f in fs if f.rule_id == "PGL002")
    assert "ATTACH PARTITION" in f.fix


# ---------------------------------------------------------------- PGL003
def test_constraints_without_not_valid(scan):
    res = run(scan, """
        set lock_timeout = '5s';
        alter table posts add constraint posts_score_chk check (score >= 0);
        alter table posts add constraint posts_user_fk2 foreign key (user_id) references users (id);
        alter table posts add constraint posts_title_key unique (title);
        """)
    assert [(r[0], r[2], r[3]) for r in res] == [("PGL003", 2, "medium"), ("PGL003", 3, "medium"),
                                                 ("PGL003", 4, "high")]


def test_constraints_done_safely(scan):
    res = run(scan, """
        set lock_timeout = '5s';
        alter table posts add constraint posts_score_chk check (score >= 0) not valid;
        alter table posts validate constraint posts_score_chk;
        alter table posts add constraint posts_title_key unique using index posts_title_key;
        """)
    assert res == []


def test_fk_fix_keeps_the_original_definition(scan_findings):
    fs = scan_findings({M1: BASE, M2: "set lock_timeout='5s';\nalter table posts add constraint p_fk foreign key "
                                      "(user_id) references users (id) on delete cascade;\n"})
    f = next(f for f in fs if f.rule_id == "PGL003")
    assert "ADD CONSTRAINT p_fk FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE NOT VALID" in \
        f.fix.replace("constraint", "CONSTRAINT").replace("foreign key", "FOREIGN KEY").replace(
            "references", "REFERENCES").replace("on delete cascade", "ON DELETE CASCADE")
    assert "VALIDATE CONSTRAINT p_fk" in f.fix


# ---------------------------------------------------------------- PGL004
def test_volatile_default_serial_identity_and_stored_generated_rewrite(scan):
    res = run(scan, """
        set lock_timeout = '5s';
        alter table posts add column ref uuid default gen_random_uuid();
        alter table posts add column seq bigserial;
        alter table posts add column n int generated always as identity;
        alter table posts add column t2 text generated always as (upper(title)) stored;
        """)
    assert [(r[0], r[2], r[3]) for r in res] == [("PGL004", n, "high") for n in (2, 3, 4, 5)]


def test_constant_and_stable_defaults_are_instant_since_pg11(scan):
    res = run(scan, """
        set lock_timeout = '5s';
        alter table posts add column a boolean not null default false;
        alter table posts add column b timestamptz not null default now();
        alter table posts add column c jsonb not null default '{}'::jsonb;
        alter table posts add column d text;
        """)
    assert res == []


def test_any_default_rewrites_before_pg11(scan):
    res = run(scan, "set lock_timeout = '5s';\nalter table posts add column a boolean not null default false;\n",
              pg_version=10)
    assert ids(res) == ["PGL004"]


def test_type_changes(scan):
    res = scan({
        M1: "create table t (id int primary key, a varchar(50), b text, c numeric(10,2), d int, e timestamp);\n",
        M2: """
            set lock_timeout = '5s';
            alter table t alter column a type varchar(100);
            alter table t alter column a type text;
            alter table t alter column c type numeric(12,2);
            alter table t alter column b type varchar(20);
            alter table t alter column d type bigint;
            alter table t alter column e type timestamptz;
            alter table t alter column c type numeric(12,4);
            """})
    assert [(r[0], r[2]) for r in res if r[0] != "PGL010"] == [("PGL004", 5), ("PGL004", 6), ("PGL004", 7),
                                                              ("PGL004", 8)]


# ---------------------------------------------------------------- PGL005
def test_add_not_null_without_default_and_set_not_null(scan):
    res = run(scan, """
        set lock_timeout = '5s';
        alter table posts add column status text not null;
        alter table posts alter column body set not null;
        """)
    assert [(r[0], r[2]) for r in res] == [("PGL005", 2), ("PGL005", 3)]


def test_set_not_null_after_validated_check_is_fine(scan):
    res = run(scan, """
        set lock_timeout = '5s';
        alter table posts add constraint body_nn check (body is not null) not valid;
        alter table posts validate constraint body_nn;
        alter table posts alter column body set not null;
        """)
    assert res == []


def test_not_null_with_default_or_on_new_table_is_fine(scan):
    res = scan({M1: BASE + "alter table posts add column status text not null;\n",
                M2: "set lock_timeout = '5s';\nalter table posts add column kind text not null default 'post';\n"})
    assert res == []


# ---------------------------------------------------------------- PGL006
def test_lock_timeout(scan):
    assert ids(run(scan, "alter table posts enable row level security;\n")) == ["PGL006"]
    assert run(scan, "SET LOCAL lock_timeout = '2s';\nalter table posts enable row level security;\n") == []
    assert run(scan, "create index concurrently x on posts (score);\n") == []


def test_schema_dump_files_get_no_lock_findings(scan):
    res = scan({"db/schema.sql": BASE + "create index posts_title_idx on posts (title);\n"
                                        "alter table posts add column z text not null;\n"})
    assert res == []


def test_applied_up_to_skips_lock_rules_for_old_migrations(scan):
    change = "create index posts_title_idx on posts (title);\n"
    assert ids(run(scan, change)) == ["PGL002"]
    assert run(scan, change, applied_up_to="002_change") == []
    assert ids(run(scan, change, applied_up_to="001")) == ["PGL002"]


# ---------------------------------------------------------------- PGL007 / 008 / 009 / 010
def test_missing_primary_key(scan):
    res = scan({M1: """
        create table logs (at timestamptz not null, msg text);
        create table tagged (post_id bigint not null, tag text not null, unique (post_id, tag));
        create table ev (id bigint, at timestamptz) partition by range (at);
        create table ev_2025 partition of ev for values from ('2025-01-01') to ('2026-01-01');
        create table copy as select 1 as x;
        create temp table scratch (x int);
        """})
    assert [(r[0], r[2]) for r in res] == [("PGL007", 1), ("PGL007", 3)]


def test_uuid_as_text_primary_key(scan):
    res = scan({M1: """
        create table a (id text primary key default gen_random_uuid()::text);
        create table b (id uuid primary key default gen_random_uuid());
        create table c (code text primary key);
        create table d (id varchar(36) primary key default uuid_generate_v4());
        """})
    assert [(r[0], r[2]) for r in res] == [("PGL008", 1), ("PGL008", 4)]


def test_money_as_float_heuristic(scan_findings):
    fs = scan_findings({M1: """
        create table p (
          id int primary key,
          price double precision,
          total_amount real,
          discount_rate double precision,
          price_cents bigint,
          weight float8,
          balance numeric(12,2)
        );
        """})
    assert sorted((f.rule_id, f.line) for f in fs) == [("PGL009", 3), ("PGL009", 4)]
    assert all(f.heuristic for f in fs)


def test_timestamp_without_time_zone_one_finding_per_table(scan):
    res = scan({M1: """
        create table a (id int primary key, created_at timestamp, updated_at timestamp(3) without time zone,
                        seen timestamptz, other timestamp with time zone);
        """})
    assert res == [("PGL010", M1, 1, "info")]


# ---------------------------------------------------------------- dialects and file selection
def test_non_postgres_sql_and_seeds_are_ignored(scan):
    res = scan({
        "db/migrations/001_mysql.sql": "CREATE TABLE a (id INT AUTO_INCREMENT PRIMARY KEY, u INT) ENGINE=InnoDB;\n",
        "supabase/seed.sql": "create table s (u int references users(id));\n",
        "db/migrations/002_x.down.sql": "create table d (u int references users(id));\n",
    })
    assert res == []


def test_prisma_mysql_migrations_are_ignored(scan):
    res = scan({
        "prisma/migrations/migration_lock.toml": 'provider = "mysql"\n',
        "prisma/migrations/1_init/migration.sql": "CREATE TABLE `a` (`id` INT NOT NULL, `u` INT, PRIMARY KEY (`id`));\n"
                                                  "CREATE TABLE b (u int references a(id));\n",
    })
    assert res == []


def test_inline_ignore_in_sql(scan):
    res = run(scan, "set lock_timeout = '5s';\n"
                    "-- pg-launch-check: ignore PGL002\n"
                    "create index posts_title_idx on posts (title);\n"
                    "create index posts_body_idx on posts (body); -- pg-launch-check: ignore\n"
                    "create index posts_score_idx on posts (score); -- pg-launch-check: ignore PGL001\n")
    assert res == [("PGL002", M2, 5, "medium")]
