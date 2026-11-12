# pg-launch-check

A static PostgreSQL performance and safety lint for backend repos. It reads your SQL
migrations, your Prisma schema and your application code, and finds the database mistakes that
work fine on a laptop and hurt in production:

- **Migrations** that lock a busy table: `CREATE INDEX` without `CONCURRENTLY`, constraints added
  without `NOT VALID`, column type changes and volatile defaults that rewrite the table,
  `NOT NULL` columns that fail on a non-empty table, no `lock_timeout`
- **Schema** problems: foreign keys without an index (PostgreSQL does not create one), tables
  without a primary key, UUIDs stored as text, money stored as floating point, `timestamp`
  without time zone
- **Queries in app code**: N+1 queries in loops (explicit queries and lazy-loaded ORM
  relations), list queries with no limit in request handlers, `OFFSET` pagination, `SELECT *`,
  leading-wildcard `LIKE` with no trigram index, SQL built from strings (SQL injection)
- **Connections**: a pool or ORM client created per request, pool size and statement timeout left
  at driver defaults, serverless functions with no pooler in sight

It understands:

- **SQL migrations**: plain `.sql` files in a `migrations/` folder or with numbered names
  (`001_init.sql`, Flyway `V1__init.sql`, `*.up.sql`), **Supabase** (`supabase/migrations`),
  **Prisma Migrate** (`prisma/migrations/*/migration.sql`), **Drizzle Kit** (`drizzle/*.sql`
  with `meta/_journal.json`), plus schema dumps such as `schema.sql`
- **Prisma** `schema.prisma` (relations, `@@index`, `@db.*` types), PostgreSQL datasources only
- **JavaScript / TypeScript**: Prisma Client, Drizzle ORM, node-postgres (`pg`), postgres.js,
  Knex raw SQL; route handlers in Next.js (App and Pages router), Express, Fastify, Hono, tRPC,
  Remix / SvelteKit loaders, Nuxt event handlers, AWS Lambda
- **Python**: SQLAlchemy (1.x and 2.0 style), Django ORM, psycopg / psycopg2, psycopg_pool,
  asyncpg; FastAPI / Flask / Django views

Every finding has a rule id, a severity, `file:line`, the offending line, why it matters, and an
exact fix: SQL for migrations, and code for the ORM or driver it was found in, with your table,
column and model names filled in.

Zero dependencies (Python 3.10+ standard library only). **Static only:** it never connects to a
database, never imports or runs your code and makes no network requests. The schema is rebuilt
by replaying your migrations in order, in memory. CI-friendly exit codes.

---

## Quick start

```bash
pipx install git+https://github.com/abhi12337/pg-launch-check   # straight from GitHub
pipx install .                          # from a clone of this repo (or: pip install .)

pg-launch-check scan                    # current folder
pg-launch-check scan services/api services/worker
pg-launch-check scan supabase/migrations/20250301000000_add_orders.sql   # a single file
pg-launch-check scan . --json > report.json
pg-launch-check rules                   # every rule, why it matters, how to fix it
```

Try it on the demo monorepo (Supabase migrations, a Next.js + Prisma app, an Express +
node-postgres + Drizzle API and a FastAPI / Django worker, all taken from `tests/fixtures`).
`before/` has the mistakes, `after/` is the same code written carefully:

```bash
python examples/make_demo.py demo-pg-app
pg-launch-check scan demo-pg-app/before
pg-launch-check scan demo-pg-app/after
```

Exit codes: `0` = nothing at or above `--fail-on` (default `medium`), `1` = findings at or above
it, `2` = usage error. Use `--fail-on critical|high|medium|low|info|never` to tune CI.

**Why the default is `medium`:** `high` findings are outages or breaches waiting to happen (SQL
injection, a pool created per request, a migration that rewrites a table under an exclusive
lock). `medium` findings are the ones that reliably turn into incidents as data grows: an
unindexed foreign key, a migration that blocks writes or fails on a non-empty table, a table
without a primary key, money in floating point, an N+1 query. Those should block a merge.
`low` findings are driver defaults and heuristics worth a look (pool size, statement timeout,
unbounded lists, `SELECT *`), and `info` findings are advice (`timestamp` without time zone,
`OFFSET` pagination, serverless pooling); they are reported but do not fail the build unless you
ask with `--fail-on low` or `--fail-on info`.

## Example output

All output below was produced by running this version of the tool on the demo project (nothing
is hand-edited).

`pg-launch-check scan demo-pg-app/before/api --brief` (the Express + node-postgres + Drizzle API
only):

```text
pg-launch-check 0.1.1
Scanned demo-pg-app/before/api
5 file(s): 2 SQL (2 migrations), 0 Prisma schema, 0 Python, 3 JS/TS; 2 table(s) in the schema

HIGH     PGL011  SQL built from strings (SQL injection)
      at: src/server.ts:12
      query() receives SQL built from req.params.id by string interpolation or concatenation: a
      value containing a quote changes the query (SQL injection). Pass it as a parameter.
      code: const { rows } = await client.query(`SELECT * FROM members WHERE team_id = ${req.params.id}`);

HIGH     PGL011  SQL built from strings (SQL injection)
      at: src/server.ts:39
      query() receives SQL built from req.body.name by string interpolation or concatenation: a
      value containing a quote changes the query (SQL injection). Pass it as a parameter.
      code: await pool.query("UPDATE teams SET name = '" + req.body.name + "' WHERE id = $1", [req.params.id]);

HIGH     PGL012  Database client or pool created per request
      at: src/server.ts:11
      new Pool() is created inside a request handler: it opens new connections on every request
      and they are never closed, so they pile up until max_connections.
      code: const client = new Pool({ connectionString: process.env.DATABASE_URL });

MEDIUM   PGL001  Foreign key column without an index
      at: drizzle/0000_init.sql:14
      members(team_id) references teams but no index starts with team_id: each delete from teams
      scans members, and joins on team_id do too.
      code: ALTER TABLE "members" ADD CONSTRAINT "members_team_id_teams_id_fk" FOREIGN KEY ("team_id") REFERENCES "public"."teams"("id") ON DELETE cascade ON UPDATE no a...

MEDIUM   PGL002  CREATE INDEX without CONCURRENTLY on an existing table
      at: drizzle/0001_members_role.sql:2
      CREATE INDEX on members, which exists before this migration, without CONCURRENTLY: writes to
      members are blocked for the whole index build.
      code: CREATE INDEX IF NOT EXISTS "members_email_idx" ON "members" USING btree ("email");

MEDIUM   PGL013  Query inside a loop over rows (N+1) [heuristic]
      at: src/server.ts:32
      This SQL query runs once per item of teamRows (loop at line 31): one query per row.
      code: const { rows: m } = await pool.query("SELECT id, email FROM members WHERE team_id = $1", [team.id]);

LOW      PGL006  Migration without lock_timeout
      at: drizzle/0001_members_role.sql:1
      This migration takes locks on existing tables but never sets lock_timeout: if a long
      transaction holds the table, the migration waits and every query on the table queues behind
      it.
      code: ALTER TABLE "members" ADD COLUMN "role" text DEFAULT 'member' NOT NULL;--> statement-breakpoint

LOW      PGL014  Unbounded query in a request handler [heuristic]
      at: src/server.ts:12
      SELECT without LIMIT in a request handler: it returns every matching row, however large the
      table grows.
      code: const { rows } = await client.query(`SELECT * FROM members WHERE team_id = ${req.params.id}`);

LOW      PGL014  Unbounded query in a request handler [heuristic]
      at: src/server.ts:17
      db.select().from(members) in a request handler has no .limit(): it returns every matching
      row.
      code: const rows = await db.select().from(members);

LOW      PGL014  Unbounded query in a request handler [heuristic]
      at: src/server.ts:32
      SELECT without LIMIT in a request handler: it returns every matching row, however large the
      table grows.
      code: const { rows: m } = await pool.query("SELECT id, email FROM members WHERE team_id = $1", [team.id]);

LOW      PGL016  SELECT * in application code
      at: src/server.ts:12
      SELECT * in application SQL: the query reads and returns every column, including ones this
      code never uses.
      code: const { rows } = await client.query(`SELECT * FROM members WHERE team_id = ${req.params.id}`);

LOW      PGL017  Leading-wildcard LIKE / ILIKE without a trigram index [heuristic]
      at: src/server.ts:23
      ilike(members.email, '%...') starts with a wildcard: without a trigram index every search
      scans the whole table.
      code: const rows = await db.select().from(members).where(ilike(members.email, `%${q}%`)).limit(50);

LOW      PGL018  Connection pool size left at the driver default
      at: src/db.ts:4
      new Pool() uses the driver's default pool size (10 connections per instance): set max so
      instances x pool size stays below max_connections.
      code: export const pool = new Pool({ connectionString: process.env.DATABASE_URL });

LOW      PGL019  No statement timeout on the database client
      at: src/db.ts:4
      new Pool() sets no statement_timeout: a runaway query keeps running and holds the connection
      long after the request gave up.
      code: export const pool = new Pool({ connectionString: process.env.DATABASE_URL });

INFO     PGL010  timestamp without time zone
      at: drizzle/0000_init.sql:10
      members: joined_at is timestamp without time zone; timestamptz stores an absolute instant in
      the same 8 bytes.
      code: "joined_at" timestamp DEFAULT now() NOT NULL

INFO     PGL015  OFFSET pagination
      at: src/server.ts:29
      OFFSET with a request-dependent value: Postgres reads and discards every skipped row, so
      deep pages get slower.
      code: const { rows: teamRows } = await pool.query("SELECT id, name FROM teams ORDER BY id LIMIT 20 OFFSET $1", [page * 20]);

Summary: 0 critical, 3 high, 3 medium, 8 low, 2 info
Exit code: 1
```

A full finding (without `--brief`) carries the reason once per rule and a fix written for the
tool it was found in, with your names filled in:

```text
MEDIUM   PGL002  CREATE INDEX without CONCURRENTLY on an existing table
      at: api/drizzle/0001_members_role.sql:2
      CREATE INDEX on members, which exists before this migration, without CONCURRENTLY: writes to
      members are blocked for the whole index build.
      code: CREATE INDEX IF NOT EXISTS "members_email_idx" ON "members" USING btree ("email");
      why: A plain CREATE INDEX holds a SHARE lock on the table for the whole build: reads
           continue, but every INSERT, UPDATE and DELETE on the table waits until the index
           is finished, which on a large table means minutes of failed or stuck writes.
           CREATE INDEX CONCURRENTLY builds the index without blocking writes (it takes
           longer and scans the table twice).
      fix:
        CREATE INDEX CONCURRENTLY IF NOT EXISTS "members_email_idx" ON "members" USING btree ("email");

        CONCURRENTLY cannot run inside a transaction block: keep the statement alone in its own
        migration file and make sure the migration tool does not wrap that file in BEGIN/COMMIT
        (several statements sent as one query string also run as one implicit transaction). If the
        build fails it leaves an INVALID index behind: DROP INDEX CONCURRENTLY members_email_idx; then retry.

        drizzle-orm's migrate() applies pending migrations inside a transaction, where CONCURRENTLY
        fails: remove the statement from the generated migration and run it as a separate step,
        e.g. psql "$DATABASE_URL" -c 'CREATE INDEX CONCURRENTLY ...'.

MEDIUM   PGL013  Query inside a loop over rows (N+1) [heuristic]
      at: web/src/app/api/posts/route.ts:19
      prisma.user.findUnique() runs once per item of posts (loop at line 18): one query per row.
      Load it with the first query (include) or with one { in: [...] } query.
      code: const author = await prisma.user.findUnique({ where: { id: post.authorId } });
      fix:
        Load the relation with the list:

          const rows = await prisma.post.findMany({ include: { author: true } });

        or fetch all related rows in one query and join them in memory:

          const related = await prisma.user.findMany({ where: { id: { in: rows.map((r) => r.authorId) } } });

MEDIUM   PGL013  Query inside a loop over rows (N+1) [heuristic]
      at: worker/library/views.py:11
      book.author is a ForeignKey read inside a loop over Book.objects... without
      select_related('author'): Django runs one query per row to load it.
      code: data.append({"title": book.title, "author": book.author.name})
      fix:
        Join it into the first query:

          Book.objects.select_related("author")
```

<details>
<summary>The whole demo monorepo: <code>pg-launch-check scan demo-pg-app/before --brief</code></summary>

```text
pg-launch-check 0.1.1
Scanned demo-pg-app/before
21 file(s): 6 SQL (6 migrations), 1 Prisma schema, 7 Python, 7 JS/TS; 10 table(s) in the schema

HIGH     PGL003  Constraint added with a full check while holding a lock
      at: supabase/migrations/20250301000000_order_tracking.sql:12
      UNIQUE constraint on existing table products builds its index while holding an ACCESS
      EXCLUSIVE lock: no reads or writes on products until it is built.
      code: alter table public.products add constraint products_sku_key unique (sku);

HIGH     PGL004  Table rewrite while holding an ACCESS EXCLUSIVE lock
      at: supabase/migrations/20250301000000_order_tracking.sql:5
      Adding orders.tracking_id with a volatile default gen_random_uuid() rewrites every row of
      orders under an ACCESS EXCLUSIVE lock (each row needs its own value).
      code: alter table public.orders add column tracking_id uuid default gen_random_uuid();

HIGH     PGL004  Table rewrite while holding an ACCESS EXCLUSIVE lock
      at: supabase/migrations/20250301000000_order_tracking.sql:8
      Changing orders.status from text to varchar(20) rewrites the whole table and its indexes
      under an ACCESS EXCLUSIVE lock (type change).
      code: alter table public.orders alter column status type varchar(20);

HIGH     PGL011  SQL built from strings (SQL injection)
      at: api/src/server.ts:12
      query() receives SQL built from req.params.id by string interpolation or concatenation: a
      value containing a quote changes the query (SQL injection). Pass it as a parameter.
      code: const { rows } = await client.query(`SELECT * FROM members WHERE team_id = ${req.params.id}`);

HIGH     PGL011  SQL built from strings (SQL injection)
      at: api/src/server.ts:39
      query() receives SQL built from req.body.name by string interpolation or concatenation: a
      value containing a quote changes the query (SQL injection). Pass it as a parameter.
      code: await pool.query("UPDATE teams SET name = '" + req.body.name + "' WHERE id = $1", [req.params.id]);

HIGH     PGL011  SQL built from strings (SQL injection)
      at: web/src/app/api/search/route.ts:6
      $queryRawUnsafe() receives SQL built from q by string interpolation or concatenation: a
      value containing a quote changes the query (SQL injection). Pass it as a parameter.
      code: const rows = await prisma.$queryRawUnsafe(

HIGH     PGL011  SQL built from strings (SQL injection)
      at: worker/billing/main.py:42
      text() receives SQL built from q with an f-string, + or % / .format(): a value containing a
      quote changes the query (SQL injection). Pass it as a parameter.
      code: return [dict(r._mapping) for r in session.execute(text(sql))]

HIGH     PGL011  SQL built from strings (SQL injection)
      at: worker/billing/main.py:49
      cur.execute() receives SQL built from email with an f-string, + or % / .format(): a value
      containing a quote changes the query (SQL injection). Pass it as a parameter.
      code: cur.execute("SELECT id, name FROM customers WHERE email = '%s'" % email)

HIGH     PGL011  SQL built from strings (SQL injection)
      at: worker/library/views.py:24
      cur.execute() receives SQL built from name with an f-string, + or % / .format(): a value
      containing a quote changes the query (SQL injection). Pass it as a parameter.
      code: cur.execute(f"SELECT b.id, b.title FROM library_book b JOIN library_author a ON a.id = b.author_id "

HIGH     PGL012  Database client or pool created per request
      at: api/src/server.ts:11
      new Pool() is created inside a request handler: it opens new connections on every request
      and they are never closed, so they pile up until max_connections.
      code: const client = new Pool({ connectionString: process.env.DATABASE_URL });

HIGH     PGL012  Database client or pool created per request
      at: web/src/app/api/posts/route.ts:5
      new PrismaClient() is created inside a request handler: it opens new connections on every
      request and they are never closed, so they pile up until max_connections.
      code: const prisma = new PrismaClient();

MEDIUM   PGL001  Foreign key column without an index
      at: api/drizzle/0000_init.sql:14
      members(team_id) references teams but no index starts with team_id: each delete from teams
      scans members, and joins on team_id do too.
      code: ALTER TABLE "members" ADD CONSTRAINT "members_team_id_teams_id_fk" FOREIGN KEY ("team_id") REFERENCES "public"."teams"("id") ON DELETE cascade ON UPDATE no a...

MEDIUM   PGL001  Foreign key column without an index
      at: supabase/migrations/20250101000000_init.sql:18
      orders(profile_id) references profiles but no index starts with profile_id: each delete from
      profiles scans orders, and joins on profile_id do too.
      code: profile_id uuid not null references public.profiles (id),

MEDIUM   PGL001  Foreign key column without an index
      at: supabase/migrations/20250101000000_init.sql:25
      order_items(order_id) references orders but no index starts with order_id: each delete from
      orders scans order_items, and joins on order_id do too.
      code: order_id bigint not null references public.orders (id) on delete cascade,

MEDIUM   PGL001  Foreign key column without an index
      at: supabase/migrations/20250101000000_init.sql:26
      order_items(product_id) references products but no index starts with product_id: each delete
      from products scans order_items, and joins on product_id do too.
      code: product_id text not null references public.products (id),

MEDIUM   PGL001  Foreign key column without an index
      at: web/prisma/schema.prisma:25
      Post.author is a relation on authorId but no @@index, @@id or @unique starts with authorId
      (Prisma does not index relation fields): each delete from User and every join on it scans
      Post.
      code: author    User      @relation(fields: [authorId], references: [id])

MEDIUM   PGL001  Foreign key column without an index
      at: web/prisma/schema.prisma:34
      Comment.post is a relation on postId but no @@index, @@id or @unique starts with postId
      (Prisma does not index relation fields): each delete from Post and every join on it scans
      Comment.
      code: post     Post   @relation(fields: [postId], references: [id], onDelete: Cascade)

MEDIUM   PGL001  Foreign key column without an index
      at: web/prisma/schema.prisma:36
      Comment.author is a relation on authorId but no @@index, @@id or @unique starts with
      authorId (Prisma does not index relation fields): each delete from User and every join on it
      scans Comment.
      code: author   User   @relation(fields: [authorId], references: [id])

MEDIUM   PGL001  Foreign key column without an index
      at: web/prisma/schema.prisma:43
      Invoice.user is a relation on userId but no @@index, @@id or @unique starts with userId
      (Prisma does not index relation fields): each delete from User and every join on it scans
      Invoice.
      code: user   User   @relation(fields: [userId], references: [id])

MEDIUM   PGL001  Foreign key column without an index
      at: worker/billing/models.py:25
      Invoice.customer_id is a ForeignKey column without index=True (and no Index() in
      __table_args__ starts with it): SQLAlchemy does not index foreign keys, so deletes of the
      parent row and joins on customer_id scan the table.
      code: customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))

MEDIUM   PGL002  CREATE INDEX without CONCURRENTLY on an existing table
      at: api/drizzle/0001_members_role.sql:2
      CREATE INDEX on members, which exists before this migration, without CONCURRENTLY: writes to
      members are blocked for the whole index build.
      code: CREATE INDEX IF NOT EXISTS "members_email_idx" ON "members" USING btree ("email");

MEDIUM   PGL002  CREATE INDEX without CONCURRENTLY on an existing table
      at: supabase/migrations/20250301000000_order_tracking.sql:3
      CREATE INDEX on orders, which exists before this migration, without CONCURRENTLY: writes to
      orders are blocked for the whole index build.
      code: create index orders_status_idx on public.orders (status);

MEDIUM   PGL002  CREATE INDEX without CONCURRENTLY on an existing table
      at: web/prisma/migrations/20250315000000_post_slug/migration.sql:5
      CREATE UNIQUE INDEX on Post, which exists before this migration, without CONCURRENTLY:
      writes to Post are blocked for the whole index build.
      code: CREATE UNIQUE INDEX "Post_slug_key" ON "Post"("slug");

MEDIUM   PGL003  Constraint added with a full check while holding a lock
      at: supabase/migrations/20250301000000_order_tracking.sql:14
      CHECK constraint order_items_quantity_check is added to existing table order_items without
      NOT VALID: every existing row is checked while holding a lock that blocks reads and writes.
      code: alter table public.order_items add constraint order_items_quantity_check check (quantity > 0);

MEDIUM   PGL003  Constraint added with a full check while holding a lock
      at: supabase/migrations/20250301000000_order_tracking.sql:15
      FOREIGN KEY constraint orders_profile_fk is added to existing table orders without NOT
      VALID: every existing row is checked while holding a lock that blocks writes on both tables.
      code: alter table public.orders add constraint orders_profile_fk foreign key (profile_id) references public.profiles (id);

MEDIUM   PGL005  NOT NULL added to an existing table
      at: supabase/migrations/20250301000000_order_tracking.sql:7
      orders.currency is added as NOT NULL without a DEFAULT: the migration fails with "column
      contains null values" as soon as orders has rows.
      code: alter table public.orders add column currency text not null;

MEDIUM   PGL005  NOT NULL added to an existing table
      at: supabase/migrations/20250301000000_order_tracking.sql:11
      SET NOT NULL on products.sku scans the whole table under an ACCESS EXCLUSIVE lock (no reads
      or writes) because no validated CHECK (sku IS NOT NULL) exists.
      code: alter table public.products alter column sku set not null;

MEDIUM   PGL005  NOT NULL added to an existing table
      at: web/prisma/migrations/20250315000000_post_slug/migration.sql:2
      Post.slug is added as NOT NULL without a DEFAULT: the migration fails with "column contains
      null values" as soon as Post has rows.
      code: ALTER TABLE "Post" ADD COLUMN "slug" TEXT NOT NULL;

MEDIUM   PGL007  Table without a primary key
      at: supabase/migrations/20250101000000_init.sql:24
      Table order_items has no primary key (and no unique NOT NULL column that could stand in for
      one).
      code: create table public.order_items (

MEDIUM   PGL009  Money stored as a floating-point type [heuristic]
      at: supabase/migrations/20250101000000_init.sql:12
      products.price looks like money but is double precision (binary floating point): amounts and
      sums will be off by fractions of a cent.
      code: price double precision not null,

MEDIUM   PGL009  Money stored as a floating-point type [heuristic]
      at: supabase/migrations/20250101000000_init.sql:19
      orders.total looks like money but is real (binary floating point): amounts and sums will be
      off by fractions of a cent.
      code: total real not null,

MEDIUM   PGL009  Money stored as a floating-point type [heuristic]
      at: web/prisma/schema.prisma:45
      Invoice.amount looks like money but is Float (double precision): amounts and sums will be
      off by fractions of a cent.
      code: amount Float

MEDIUM   PGL009  Money stored as a floating-point type [heuristic]
      at: worker/billing/models.py:26
      Invoice.amount looks like money but is mapped as Float (floating point): amounts and sums
      will be off by fractions of a cent.
      code: amount: Mapped[float] = mapped_column(Float)

MEDIUM   PGL009  Money stored as a floating-point type [heuristic]
      at: worker/library/models.py:11
      Book.price looks like money but is a FloatField (double precision): amounts and sums will be
      off by fractions of a cent.
      code: price = models.FloatField()

MEDIUM   PGL012  Database client or pool created per request
      at: worker/billing/main.py:47
      psycopg.connect() is called inside a request handler: it opens new database connections on
      every request instead of reusing a pool.
      code: conn = psycopg.connect(DATABASE_URL)

MEDIUM   PGL013  Query inside a loop over rows (N+1) [heuristic]
      at: api/src/server.ts:32
      This SQL query runs once per item of teamRows (loop at line 31): one query per row.
      code: const { rows: m } = await pool.query("SELECT id, email FROM members WHERE team_id = $1", [team.id]);

MEDIUM   PGL013  Query inside a loop over rows (N+1) [heuristic]
      at: web/src/app/api/posts/route.ts:19
      prisma.user.findUnique() runs once per item of posts (loop at line 18): one query per row.
      Load it with the first query (include) or with one { in: [...] } query.
      code: const author = await prisma.user.findUnique({ where: { id: post.authorId } });

MEDIUM   PGL013  Query inside a loop over rows (N+1) [heuristic]
      at: worker/billing/main.py:23
      session.scalars(select(Invoice)) runs once per item of
      session.scalars(select(Customer).order_by(Customer.id).li... (loop at line 22): one query
      per row.
      code: invoices = session.scalars(select(Invoice).where(Invoice.customer_id == customer.id).limit(20)).all()

MEDIUM   PGL013  Query inside a loop over rows (N+1) [heuristic]
      at: worker/billing/main.py:34
      inv.customer is a lazy-loaded relationship read inside a loop over Invoice rows: SQLAlchemy
      runs one SELECT per row to load it.
      code: result.append({"id": inv.id, "customer": inv.customer.name})

MEDIUM   PGL013  Query inside a loop over rows (N+1) [heuristic]
      at: worker/library/views.py:11
      book.author is a ForeignKey read inside a loop over Book.objects... without
      select_related('author'): Django runs one query per row to load it.
      code: data.append({"title": book.title, "author": book.author.name})

LOW      PGL006  Migration without lock_timeout
      at: api/drizzle/0001_members_role.sql:1
      This migration takes locks on existing tables but never sets lock_timeout: if a long
      transaction holds the table, the migration waits and every query on the table queues behind
      it.
      code: ALTER TABLE "members" ADD COLUMN "role" text DEFAULT 'member' NOT NULL;--> statement-breakpoint

LOW      PGL006  Migration without lock_timeout
      at: supabase/migrations/20250301000000_order_tracking.sql:5
      This migration takes locks on existing tables but never sets lock_timeout: if a long
      transaction holds the table, the migration waits and every query on the table queues behind
      it.
      code: alter table public.orders add column tracking_id uuid default gen_random_uuid();

LOW      PGL006  Migration without lock_timeout
      at: web/prisma/migrations/20250315000000_post_slug/migration.sql:2
      This migration takes locks on existing tables but never sets lock_timeout: if a long
      transaction holds the table, the migration waits and every query on the table queues behind
      it.
      code: ALTER TABLE "Post" ADD COLUMN "slug" TEXT NOT NULL;

LOW      PGL008  UUID primary key stored as text
      at: supabase/migrations/20250101000000_init.sql:10
      products.id is a text primary key filled with gen_random_uuid()::text: the same value as a
      native uuid would take 16 bytes instead of 37 in the key and every index and foreign key
      that copies it.
      code: id text primary key default gen_random_uuid()::text,

LOW      PGL008  UUID primary key stored as text
      at: web/prisma/schema.prisma:11
      User.id holds UUIDs but is stored as text (no @db.Uuid): 37 bytes instead of 16 in the key
      and in every index and foreign key that copies it.
      code: id        String    @id @default(uuid())

LOW      PGL014  Unbounded query in a request handler [heuristic]
      at: api/src/server.ts:12
      SELECT without LIMIT in a request handler: it returns every matching row, however large the
      table grows.
      code: const { rows } = await client.query(`SELECT * FROM members WHERE team_id = ${req.params.id}`);

LOW      PGL014  Unbounded query in a request handler [heuristic]
      at: api/src/server.ts:17
      db.select().from(members) in a request handler has no .limit(): it returns every matching
      row.
      code: const rows = await db.select().from(members);

LOW      PGL014  Unbounded query in a request handler [heuristic]
      at: api/src/server.ts:32
      SELECT without LIMIT in a request handler: it returns every matching row, however large the
      table grows.
      code: const { rows: m } = await pool.query("SELECT id, email FROM members WHERE team_id = $1", [team.id]);

LOW      PGL014  Unbounded query in a request handler [heuristic]
      at: web/src/app/api/users/[id]/comments/route.ts:6
      prisma.comment.findMany() in a request handler has no take: it returns every matching row,
      however many the table grows to.
      code: const comments = await prisma.comment.findMany({

LOW      PGL014  Unbounded query in a request handler [heuristic]
      at: worker/billing/main.py:14
      session.scalars(select(Customer)).all() in a request handler has no .limit(): it returns
      every matching row.
      code: customers = session.scalars(select(Customer)).all()

LOW      PGL014  Unbounded query in a request handler [heuristic]
      at: worker/library/views.py:8
      Book.objects.order_by() in a request handler has no slice or pagination: it returns every
      matching row, however large the table grows.
      code: books = Book.objects.order_by("-published_at")

LOW      PGL016  SELECT * in application code
      at: api/src/server.ts:12
      SELECT * in application SQL: the query reads and returns every column, including ones this
      code never uses.
      code: const { rows } = await client.query(`SELECT * FROM members WHERE team_id = ${req.params.id}`);

LOW      PGL016  SELECT * in application code
      at: web/src/app/api/search/route.ts:7
      SELECT * in application SQL: the query reads and returns every column, including ones this
      code never uses.
      code: `SELECT * FROM "Post" WHERE title ILIKE '%${q}%' ORDER BY "createdAt" DESC LIMIT 20`,

LOW      PGL016  SELECT * in application code
      at: worker/billing/main.py:41
      SELECT * in application SQL: the query reads and returns every column, including ones this
      code never uses.
      code: sql = f"SELECT * FROM invoices WHERE note LIKE '%{q}%' ORDER BY id LIMIT 50 OFFSET {page * 50}"

LOW      PGL017  Leading-wildcard LIKE / ILIKE without a trigram index [heuristic]
      at: api/src/server.ts:23
      ilike(members.email, '%...') starts with a wildcard: without a trigram index every search
      scans the whole table.
      code: const rows = await db.select().from(members).where(ilike(members.email, `%${q}%`)).limit(50);

LOW      PGL017  Leading-wildcard LIKE / ILIKE without a trigram index [heuristic]
      at: web/src/app/api/posts/route.ts:11
      Prisma contains on title becomes LIKE '%...%' / ILIKE: without a trigram index every search
      scans the whole table.
      code: where: { title: { contains: q, mode: "insensitive" } },

LOW      PGL017  Leading-wildcard LIKE / ILIKE without a trigram index [heuristic]
      at: web/src/app/api/search/route.ts:7
      title LIKE '%...' starts with a wildcard: without a trigram index every search scans the
      whole table.
      code: `SELECT * FROM "Post" WHERE title ILIKE '%${q}%' ORDER BY "createdAt" DESC LIMIT 20`,

LOW      PGL017  Leading-wildcard LIKE / ILIKE without a trigram index [heuristic]
      at: worker/billing/main.py:41
      note LIKE '%...' starts with a wildcard: without a trigram index every search scans the
      whole table.
      code: sql = f"SELECT * FROM invoices WHERE note LIKE '%{q}%' ORDER BY id LIMIT 50 OFFSET {page * 50}"

LOW      PGL017  Leading-wildcard LIKE / ILIKE without a trigram index [heuristic]
      at: worker/library/views.py:17
      title__icontains becomes LIKE/ILIKE '%...%': without a trigram index on title every search
      scans the whole table.
      code: books = Book.objects.filter(title__icontains=q)[:20]

LOW      PGL018  Connection pool size left at the driver default
      at: api/src/db.ts:4
      new Pool() uses the driver's default pool size (10 connections per instance): set max so
      instances x pool size stays below max_connections.
      code: export const pool = new Pool({ connectionString: process.env.DATABASE_URL });

LOW      PGL018  Connection pool size left at the driver default
      at: worker/billing/db.py:8
      create_engine() uses SQLAlchemy's default pool (pool_size=5 plus max_overflow=10 per
      process): set it so processes x pool size stays below max_connections.
      code: engine = create_engine(DATABASE_URL)

LOW      PGL019  No statement timeout on the database client
      at: api/src/db.ts:4
      new Pool() sets no statement_timeout: a runaway query keeps running and holds the connection
      long after the request gave up.
      code: export const pool = new Pool({ connectionString: process.env.DATABASE_URL });

LOW      PGL019  No statement timeout on the database client
      at: worker/billing/db.py:8
      create_engine() sets no statement_timeout: a runaway query keeps running and holds the
      connection long after the request gave up.
      code: engine = create_engine(DATABASE_URL)

INFO     PGL010  timestamp without time zone
      at: api/drizzle/0000_init.sql:10
      members: joined_at is timestamp without time zone; timestamptz stores an absolute instant in
      the same 8 bytes.
      code: "joined_at" timestamp DEFAULT now() NOT NULL

INFO     PGL010  timestamp without time zone
      at: supabase/migrations/20250101000000_init.sql:6
      profiles: created_at is timestamp without time zone; timestamptz stores an absolute instant
      in the same 8 bytes.
      code: created_at timestamp not null default now()

INFO     PGL010  timestamp without time zone
      at: supabase/migrations/20250101000000_init.sql:21
      orders: created_at is timestamp without time zone; timestamptz stores an absolute instant in
      the same 8 bytes.
      code: created_at timestamp not null default now()

INFO     PGL010  timestamp without time zone
      at: web/prisma/schema.prisma:17
      User: createdAt is DateTime without @db.Timestamptz, which Prisma stores as timestamp(3)
      without time zone.
      code: createdAt DateTime  @default(now())

INFO     PGL010  timestamp without time zone
      at: web/prisma/schema.prisma:28
      Post: createdAt, updatedAt are DateTime without @db.Timestamptz, which Prisma stores as
      timestamp(3) without time zone.
      code: createdAt DateTime  @default(now())

INFO     PGL010  timestamp without time zone
      at: worker/billing/models.py:17
      Customer: created_at is DateTime without timezone=True, which SQLAlchemy creates as
      timestamp without time zone.
      code: created_at: Mapped[datetime] = mapped_column(server_default=func.now())

INFO     PGL015  OFFSET pagination
      at: api/src/server.ts:29
      OFFSET with a request-dependent value: Postgres reads and discards every skipped row, so
      deep pages get slower.
      code: const { rows: teamRows } = await pool.query("SELECT id, name FROM teams ORDER BY id LIMIT 20 OFFSET $1", [page * 20]);

INFO     PGL015  OFFSET pagination
      at: web/src/app/api/posts/route.ts:10
      prisma.post.findMany() pages with skip: page * 20: Postgres reads and discards every skipped
      row, so deep pages get slower.
      code: const posts = await prisma.post.findMany({

INFO     PGL015  OFFSET pagination
      at: worker/billing/main.py:41
      OFFSET with a request-dependent value: Postgres reads and discards every skipped row, so
      deep pages get slower.
      code: sql = f"SELECT * FROM invoices WHERE note LIKE '%{q}%' ORDER BY id LIMIT 50 OFFSET {page * 50}"

INFO     PGL015  OFFSET pagination
      at: worker/library/views.py:31
      Slicing a queryset with [(n - 1) * 20:n * 20] pages with OFFSET: Postgres reads and discards
      every skipped row, so deep pages get slower.
      code: books = Book.objects.order_by("id")[(n - 1) * 20:n * 20]

INFO     PGL020  Serverless functions connecting without a pooler hint
      at: api/src/db.ts:4
      This project deploys to a serverless platform and new Pool() connects without any pooler
      hint in the repo (no pooled URL, pgbouncer=true, Neon / Vercel Postgres driver or Prisma
      directUrl): each function instance opens its own connections.
      code: export const pool = new Pool({ connectionString: process.env.DATABASE_URL });

Summary: 0 critical, 11 high, 29 medium, 23 low, 11 info
Exit code: 1
```

</details>

The same monorepo written carefully (`after/`: indexed foreign keys, `NOT VALID` + `VALIDATE`,
`CONCURRENTLY` in its own migration, `lock_timeout`, `timestamptz`, `numeric`, eager loading,
limits and keyset pagination, parameterized SQL, one configured pool per process, a trigram
index for search, a pooled connection string) produces no findings:

```text
pg-launch-check 0.1.1
Scanned demo-pg-app/after
25 file(s): 10 SQL (10 migrations), 1 Prisma schema, 7 Python, 7 JS/TS; 10 table(s) in the schema

No findings. (This is not a guarantee: see 'Known limitations' in the README.)
Summary: 0 critical, 0 high, 0 medium, 0 low, 0 info
Exit code: 0
```

## What it checks

| Rule | Severity | What it finds |
|---|---|---|
| PGL001 | Medium | A foreign key whose column(s) are not the leading column(s) of any index. Resolved across all migrations in order, including composite and multi-column indexes (an index on `(user_id, created_at)` covers `user_id`; one on `(created_at, user_id)` does not), primary keys, `UNIQUE` constraints, renames and drops. Partial and expression indexes do not count. In Prisma, relation fields (`@relation(fields: [...])`) need `@@index`, `@@id`, `@@unique`, `@id` or `@unique` starting with those fields (or an index created in a Prisma `migration.sql`). In SQLAlchemy, `ForeignKey()` columns without `index=True` or a matching `Index()`. |
| PGL002 | Medium | `CREATE INDEX` without `CONCURRENTLY` on a table that existed before the migration (blocks writes for the whole build). The fix includes your statement rewritten, notes that `CONCURRENTLY` cannot run inside a transaction block, and has variants for Prisma Migrate, Drizzle and partitioned tables. |
| PGL003 | Medium / High | On an existing table: `ADD CONSTRAINT ... FOREIGN KEY` or `CHECK` without `NOT VALID` (medium: every row is checked under a lock), `ADD CONSTRAINT ... UNIQUE` / `PRIMARY KEY` without `USING INDEX` (high: the index is built under an `ACCESS EXCLUSIVE` lock). |
| PGL004 | High | Table rewrites under `ACCESS EXCLUSIVE`: `ALTER COLUMN ... TYPE` (except changes that are known not to rewrite: `varchar(n)` to a longer `varchar` or `text`, `varchar` to `text`, `numeric(p,s)` to `numeric(q,s)` with `q >= p`; `timestamp` / `timestamptz` is reported with the PostgreSQL 12+ UTC exception), and `ADD COLUMN` with a volatile default (`gen_random_uuid()`, `random()`, `clock_timestamp()`, `nextval()`, ...), a `serial` type, an identity column or a `STORED` generated column. Constant and stable defaults (`now()`, `'{}'::jsonb`, `false`) are instant since PostgreSQL 11 and are not reported, unless `--pg-version` is below 11. |
| PGL005 | Medium | `ADD COLUMN ... NOT NULL` without a `DEFAULT` on an existing table (fails as soon as the table has rows), and `ALTER COLUMN ... SET NOT NULL` without a validated `CHECK (col IS NOT NULL)` before it (full scan under `ACCESS EXCLUSIVE`). |
| PGL006 | Low | A migration that takes locks on existing tables (`ALTER TABLE`, non-concurrent `CREATE` / `DROP INDEX`, `CREATE TRIGGER`, `LOCK`) and never sets `lock_timeout` (or `statement_timeout`). Reported once per file. |
| PGL007 | Medium | A table without a primary key and without a unique index on `NOT NULL` columns that could stand in for one. Partitions, temporary tables and `CREATE TABLE ... AS / LIKE / OF` are skipped. Prisma models without `@id` / `@@id`. |
| PGL008 | Low | A `text` / `varchar` primary key filled with a UUID generator (`gen_random_uuid()::text`, `uuid_generate_v4()`), or a Prisma `String @id @default(uuid())` without `@db.Uuid`. Text keys holding other values (codes, slugs, cuids) are not reported. |
| PGL009 | Medium | Heuristic: a `real` / `double precision` / `float` column (Prisma `Float`, SQLAlchemy `Float`, Django `FloatField`) whose name looks like money (`price`, `amount`, `total`, `balance`, `fee`, `tax`, ...) and not like a rate or ratio. |
| PGL010 | Info | `timestamp` without time zone, including Prisma `DateTime` without `@db.Timestamptz` and SQLAlchemy `DateTime` without `timezone=True`. One finding per table. |
| PGL011 | High | SQL built from variables with template literals, f-strings, `+`, `%` or `.format()` and passed to `query()`, `execute()`, `$queryRawUnsafe()`, `$executeRawUnsafe()`, `sql.raw()`, postgres.js `unsafe()`, Knex `raw()` / `whereRaw()`, psycopg / asyncpg / Django `cursor.execute()`, `fetch()`, `Model.objects.raw()`, SQLAlchemy `text()` (directly, or through one variable). Constants, numbers (`Number()`, `int()`, `%d`), and generated placeholder lists (`$1, $2`, `%s, %s`) are recognised as safe. Tagged templates (Prisma `$queryRaw`, Drizzle's `sql` tag, postgres.js's `sql` tag) are parameterized and never reported. |
| PGL012 | High / Medium | `new Pool()`, `new PrismaClient()`, `postgres()`, `create_engine()`, `asyncpg.create_pool()`, `ConnectionPool()` created inside a request handler (high), or a single `new Client()` / `psycopg.connect()` / `asyncpg.connect()` per request (medium). Neon's serverless driver, which is designed to be created per request, is not reported. |
| PGL013 | Medium | Heuristic: a database read inside a loop that uses the loop variable: Prisma, Drizzle (`select()` chains and relational queries), node-postgres / postgres.js SQL, SQLAlchemy (`session.get`, `session.query`, `select()`), Django `objects`, psycopg / asyncpg; `for`, `for...of`, `forEach`, `map` (including `Promise.all`) and comprehensions. Also lazy loading: a Django `ForeignKey` read (or reverse / many-to-many `.all()`) in a loop over a queryset without `select_related` / `prefetch_related`, and a SQLAlchemy `relationship()` with default lazy loading read in a loop over query results without `.options(...)`. Prisma `findUnique` inside `Promise.all(map(...))` is not reported (Prisma batches those). `while` loops are not reported. |
| PGL014 | Low | Heuristic: a list query with no limit inside a request handler: Prisma `findMany` without `take`, Drizzle without `.limit()` / `limit:`, SQLAlchemy `.all()` without `.limit()`, Django querysets that are never sliced or paginated, raw `SELECT` without `LIMIT`. Lookups by key (`id`, `IN (...)`, `= ANY(...)`, `email`, `slug`), aggregates and small lookup tables (`plans`, `roles`, `countries`, ...) are skipped. |
| PGL015 | Info | `OFFSET` with a request-dependent value: Prisma `skip` without `cursor`, Drizzle / SQLAlchemy `.offset(x)`, Django slices starting at a variable, `OFFSET $1` in SQL. |
| PGL016 | Low | `SELECT *` (or `SELECT t.*`) in SQL strings in application code. `count(*)` and `EXISTS (SELECT * ...)` are fine. |
| PGL017 | Low | Heuristic: a pattern with a leading wildcard, `LIKE '%...'`, `ILIKE '%' \|\| $1`, Prisma `contains` / `endsWith`, Drizzle `like()` / `ilike()` with a pattern starting with `%`, Django `__contains` / `__icontains` / `__endswith`, SQLAlchemy `.contains()` / `.ilike("%...")`, when no `gin_trgm_ops` / `gist_trgm_ops` index on a column of that name exists in the migrations, the Prisma schema, Django `GinIndex(opclasses=...)` or SQLAlchemy `Index(postgresql_ops=...)`. |
| PGL018 | Low | A node-postgres `Pool`, postgres.js instance, SQLAlchemy engine, asyncpg pool or psycopg_pool pool with no explicit maximum size. |
| PGL019 | Low | The same pools (and node-postgres `Client`) with no `statement_timeout` (or `query_timeout` / `command_timeout`). Not reported when the file sets `statement_timeout` some other way or a scanned migration runs `ALTER ROLE / DATABASE ... SET statement_timeout`. |
| PGL020 | Info | A project that deploys to a serverless platform (Next.js, Vercel, Netlify, Lambda / Serverless / SST) and creates a module-level pool or Prisma client, with no pooler hint anywhere in the repo (a pooled host or port in `.env*` files, `pgbouncer=true`, Prisma `directUrl`, Prisma Accelerate, Neon serverless or Vercel Postgres drivers). Reported once per project. `.env` files are only searched for these markers; their values are never printed. |

Findings marked `[heuristic]` (PGL009, PGL013, PGL014, PGL017) are pattern-based guesses about
intent or data size; treat them as review prompts.

### How migrations are read

Migration files are replayed in name order within each folder (timestamps and sequence numbers
sort correctly), then other `.sql` files (schema dumps) are added. A table counts as **existing**
for the lock rules (PGL002 to PGL006) when the migration that changes it did not create it.
Schema dumps never get lock findings. Statements inside `DO $$ BEGIN ... END $$` blocks
(drizzle-kit and hand-written idempotent migrations use them) are read too; function bodies are
not. Seed files and `down` migrations are skipped, and files with MySQL, SQLite or SQL Server
syntax (or Prisma / Drizzle folders whose provider is not PostgreSQL) are ignored.

Migrations that already ran in production cannot be changed any more. Pass
`--applied-up-to <migration name>` (for example `--applied-up-to 20250301000000`) and older
migrations still build the schema, but lock-risk rules only judge the newer ones.

## Known limitations

Be clear about the limits before relying on it:

- **It is static.** It never sees your database: not the table sizes, not the indexes someone
  created by hand, not role or database settings. A lock that is harmless on an empty table is
  reported the same way as one on a table with 100 million rows. An index created outside the
  scanned migrations makes PGL001 a false positive (use an inline ignore). A clean report is no
  guarantee.
- **The SQL reader is not a full PostgreSQL parser.** It understands the DDL that migrations
  use (`CREATE` / `ALTER` / `DROP TABLE`, `CREATE` / `DROP` / `ALTER INDEX`, constraints,
  renames, `DO` blocks) and ignores what it does not understand. Dynamic SQL (`EXECUTE` in
  functions) is not read. Several migration sets in one folder tree are merged into one schema;
  scan separate projects separately.
- **ORM migration files are not read.** Django, Alembic, Knex, TypeORM and Sequelize migrations
  are code; generate their SQL (`manage.py sqlmigrate`, `alembic upgrade --sql`) and scan that.
  Drizzle is read from the SQL that drizzle-kit generates, not from `pgTable()` definitions, so
  `drizzle-kit push` projects without migration files get only the app-code rules.
- **App-code rules work per file.** Loops, handlers and variables are followed within one file
  (one assignment deep for SQL strings). JavaScript / TypeScript is analysed without a full
  parser: comments and strings are lexed out and patterns are matched on the rest. Python uses
  the real `ast` module and is more precise.
- **N+1 detection needs the loop variable in the query.** Queries hidden behind helper functions
  or another module, Django template lookups (`{{ book.author.name }}`), DRF nested serializers
  and GraphQL resolvers are not seen. Async SQLAlchemy lazy loads are not reported (they raise
  instead of querying).
- **Some rules guess.** "Likely to grow", "looks like money" and "is a request handler" are
  decided from names and patterns. PGL020 cannot see the connection string your deployment
  uses.
- **PostgreSQL facts are for PostgreSQL 11 and later** unless you pass `--pg-version`. Lock
  levels and rewrite rules are described as the PostgreSQL documentation states them for
  current releases.
- **Not covered:** Rails / ActiveRecord, Go, Java / JPA, TypeORM / Sequelize / Kysely / MikroORM
  query builders (their raw SQL strings still get the SQL text rules), supabase-js / PostgREST
  queries, missing indexes on `WHERE` columns other than foreign keys, query plans, row-level
  security, and anything that needs a live database.

## How to fix each finding

`pg-launch-check rules` prints every rule with all its fix variants. In a report, each finding
shows only the fix for where it was found (SQL migration, Prisma, Drizzle, node-postgres,
postgres.js, SQLAlchemy, Django, psycopg, asyncpg), with your names filled in. The short version:

**PGL001 - foreign key without an index.** `CREATE INDEX CONCURRENTLY ... ON orders (user_id);`
alone in its own migration; Prisma `@@index([userId])`; SQLAlchemy `index=True`.

**PGL002 - CREATE INDEX without CONCURRENTLY.** Add `CONCURRENTLY` and keep it alone in a
migration that does not run inside a transaction (Drizzle's migrator always uses one: run it as a
separate step). If it fails, `DROP INDEX CONCURRENTLY` the invalid index and retry.

**PGL003 - constraint checked under a lock.** `ADD CONSTRAINT ... NOT VALID`, then
`VALIDATE CONSTRAINT` in a later migration; for `UNIQUE` / `PRIMARY KEY`, `CREATE UNIQUE INDEX
CONCURRENTLY` first, then `ADD CONSTRAINT ... USING INDEX`.

**PGL004 - table rewrite.** Expand and contract: add a new column, backfill in batches, switch,
drop the old one. For a volatile default: add the column, `SET DEFAULT` for new rows, backfill
old rows in batches.

**PGL005 - NOT NULL.** A constant `DEFAULT` (instant since PostgreSQL 11), or
`CHECK (col IS NOT NULL) NOT VALID` -> `VALIDATE` -> `SET NOT NULL` -> drop the check.

**PGL006 - no lock_timeout.** `SET lock_timeout = '5s';` at the top of the migration, and retry
when it times out.

**PGL007 / PGL008 / PGL009 / PGL010 - schema.** `id bigint GENERATED ALWAYS AS IDENTITY PRIMARY
KEY` (or a composite key for join tables); `uuid` (Prisma `@db.Uuid`); `numeric(12, 2)` or
integer cents (Prisma `Decimal`, Django `DecimalField`, SQLAlchemy `Numeric`); `timestamptz`
(Prisma `@db.Timestamptz(3)`, SQLAlchemy `DateTime(timezone=True)`).

**PGL011 - SQL injection.** Placeholders (`$1`, `%s`, `:name`, `?`) with values passed
separately, or the tagged templates of Prisma, Drizzle and postgres.js; identifiers from an
allow-list (`psycopg.sql.Identifier`, `sql.identifier`, Knex `??`).

**PGL012 - client per request.** One pool / client per process at module level (the Prisma
`globalThis` singleton for Next.js dev), borrowed per request.

**PGL013 - N+1.** Prisma `include`, Drizzle `with` or `inArray`, SQL `WHERE id = ANY($1)` or a
join, SQLAlchemy `selectinload` / `joinedload`, Django `select_related` / `prefetch_related`.

**PGL014 / PGL015 - unbounded lists and OFFSET.** A `LIMIT` / `take` on every list endpoint,
and keyset pagination (`WHERE id > $last ORDER BY id LIMIT n`, Prisma `cursor`).

**PGL016 / PGL017 - SELECT * and LIKE '%...'.** Name the columns; `CREATE EXTENSION pg_trgm`
and a `gin (col gin_trgm_ops)` index (Prisma `@@index([col(ops: raw("gin_trgm_ops"))], type: Gin)`).

**PGL018 / PGL019 / PGL020 - connection settings.** Set the pool maximum so instances x pool size
stays below `max_connections`; set `statement_timeout` on the pool or the role; on serverless,
connect through a pooler (Supabase port 6543, Neon `-pooler` host, `pgbouncer=true` with Prisma,
RDS Proxy).

## Suppressing a finding

Add `pg-launch-check: ignore` as a comment on the reported line or on the line above it (`--`
in SQL, `//` in TypeScript and Prisma, `#` in Python). Add rule ids to silence only those:

```sql
-- pg-launch-check: ignore PGL002   (table is tiny and only written by an admin job)
CREATE INDEX settings_key_idx ON settings (key);
```

```ts
const rows = await prisma.auditLog.findMany(); // pg-launch-check: ignore PGL014
```

To turn a rule off for a whole run: `--disable PGL010` (repeatable).

## Use in CI

See [`examples/github-actions/pg-launch-check.yml`](examples/github-actions/pg-launch-check.yml).
It runs on every pull request, fails the build on medium findings, and uploads the JSON report
as an artifact.

## JSON output

`--json` prints (one finding shown, from the demo project; `targets` shortened):

```json
{
  "tool": "pg-launch-check",
  "version": "0.1.1",
  "targets": [
    "/abs/path/demo-pg-app/before"
  ],
  "files_scanned": 21,
  "sql_files": 6,
  "migration_files": 6,
  "prisma_files": 1,
  "py_files": 7,
  "js_files": 7,
  "tables": 10,
  "foreign_keys": 10,
  "pg_version": 14,
  "skipped": [],
  "exit_code": 1,
  "summary": {
    "critical": 0,
    "high": 11,
    "medium": 29,
    "low": 23,
    "info": 11
  },
  "findings": [
    {
      "rule_id": "PGL001",
      "severity": "medium",
      "message": "Post.author is a relation on authorId but no @@index, @@id or @unique starts with authorId (Prisma does not index relation fields): each delete from User and every join on it scans Post.",
      "file": "web/prisma/schema.prisma",
      "line": 25,
      "evidence": "author    User      @relation(fields: [authorId], references: [id])",
      "heuristic": false,
      "title": "Foreign key column without an index",
      "why": "PostgreSQL creates an index for PRIMARY KEY and UNIQUE constraints, but not for the referencing column(s) of a FOREIGN KEY. Without one, every DELETE (or key UPDATE) on the referenced table has to scan the referencing table to check or cascade matching rows, and joins and filters on the column (WHERE user_id = $1, the usual access path) scan it too. It is invisible on small tables and turns into slow deletes, long lock waits and slow pages as the table grows.",
      "fix": "Add an index on the relation scalar field(s) in model Post:\n\n  model Post {\n    ...\n    @@index([authorId])\n  }\n\nThen `npx prisma migrate dev --create-only`. If the table already holds data, change the\ngenerated CREATE INDEX to CREATE INDEX CONCURRENTLY and keep it alone in that migration.sql.",
      "extra": {
        "model": "Post",
        "fields": [
          "authorId"
        ],
        "table": "Post"
      }
    }
  ]
}
```

`"skipped"` lists Python files that could not be parsed (`{"path": ..., "reason": "syntax error
line 12"}`). Such files are never treated as clean: each is also reported on stderr
(`warning: app/db.py: syntax error line 12; file not scanned`) and counted in the text header.
They do not change the exit code.

## Options

```text
pg-launch-check scan [PATH ...]
  --exclude GLOB              skip paths (repeatable)
  --include-tests             also scan test / fixture files (skipped by default)
  --disable RULE              ignore a rule id (repeatable)
  --pg-version N              PostgreSQL major version the migrations run on (default 14)
  --applied-up-to MIGRATION   older migrations build the schema but get no lock-risk findings
  --max-file-mb N             skip files larger than this (default 2)
  --json                      JSON report
  --fail-on SEVERITY          exit 1 at or above this severity (default medium; or never)
  --min-severity SEVERITY     hide findings below this severity
  --brief                     omit the why / fix text
  --no-color                  disable ANSI colours (also honours NO_COLOR)
pg-launch-check rules
```

`node_modules`, `.git`, virtualenvs, build output (`dist`, `build`, `.next`, ...) and hidden
folders are always skipped.

## Development

```bash
pip install -e ".[test]"
pytest
```

Every rule has tests for code that must be reported and for well-designed code that must not
be (`tests/test_sql.py`, `tests/test_prisma.py`, `tests/test_js.py`, `tests/test_python.py`),
plus four fixture pairs (`tests/fixtures/*_vulnerable` and `*_clean`) used by the demo: the
vulnerable ones together trigger every rule, and the clean ones must produce no findings at all.

## License

MIT, Copyright (c) 2026 Abheesht Tripathi. See [LICENSE](LICENSE).

---

Questions, or want a second pair of eyes on your schema and migrations before launch? abheeshttripathi890@gmail.com
