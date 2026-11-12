"""JavaScript / TypeScript application code rules."""

from conftest import ids

PRISMA_PKG = '{"dependencies": {"@prisma/client": "^6", "express": "^4"}}'
PG_PKG = '{"dependencies": {"pg": "^8", "express": "^4"}}'


def js(scan, code: str, name: str = "src/server.ts", pkg: str = PG_PKG, **files):
    return scan({name: code, "package.json": pkg, **files})


# ---------------------------------------------------------------- PGL011
def test_sql_injection_template_and_concat(scan):
    res = js(scan, """
        import { pool } from "./db";
        export async function a(id: string) {
          return pool.query(`SELECT id FROM users WHERE id = ${id}`);
        }
        export async function b(name: string) {
          const sql = "SELECT id FROM users WHERE name = '" + name + "'";
          return pool.query(sql);
        }
        export async function c(prisma: any, q: string) {
          return prisma.$queryRawUnsafe(`DELETE FROM sessions WHERE token = '${q}'`);
        }
        export async function d(knex: any, col: string) {
          return knex("users").whereRaw(`lower(email) = '${col}'`);
        }
        """)
    assert [(r[0], r[2], r[3]) for r in res if r[0] == "PGL011"] == [
        ("PGL011", 3, "high"), ("PGL011", 7, "high"), ("PGL011", 10, "high"), ("PGL011", 13, "high")]


def test_parameterized_and_tagged_templates_are_safe(scan):
    res = js(scan, """
        import { pool } from "./db";
        import { sql } from "drizzle-orm";
        const TABLE = "users";
        export async function a(id: string, ids: number[], limit: string, prisma: any, db: any, email: string) {
          await pool.query("SELECT id FROM users WHERE id = $1", [id]);
          await pool.query(`SELECT id FROM ${TABLE} WHERE id = $1 LIMIT ${Number(limit)}`, [id]);
          const marks = ids.map((_, i) => "$" + (i + 1)).join(", ");
          await pool.query(`SELECT id FROM users WHERE id IN (${marks})`, ids);
          await prisma.$queryRaw`SELECT id FROM users WHERE email = ${email}`;
          await db.execute(sql`SELECT id FROM users WHERE email = ${email}`);
          await prisma.$queryRawUnsafe("SELECT id FROM users WHERE email = $1", email);
        }
        """)
    assert [r for r in res if r[0] == "PGL011"] == []


def test_injection_fix_matches_library(scan_findings):
    fs = scan_findings({"src/a.ts": "export const f = (prisma: any, q: string) => "
                                    "prisma.$queryRawUnsafe(`SELECT id FROM t WHERE x = '${q}'`);\n",
                        "package.json": PRISMA_PKG})
    f = next(f for f in fs if f.rule_id == "PGL011")
    assert "prisma.$queryRaw`" in f.fix


# ---------------------------------------------------------------- PGL012 / 018 / 019 / 020
def test_client_per_request(scan):
    res = js(scan, """
        import express from "express";
        import { Pool, Client } from "pg";
        const app = express();
        app.get("/a", async (req, res) => {
          const pool = new Pool();
          res.json((await pool.query("SELECT 1")).rows);
        });
        app.get("/b", async (req, res) => {
          const client = new Client();
          await client.connect();
          res.json((await client.query("SELECT 1")).rows);
          await client.end();
        });
        """)
    assert [(r[0], r[2], r[3]) for r in res] == [("PGL012", 5, "high"), ("PGL012", 9, "medium")]


def test_prisma_client_per_request_and_module_level(scan):
    res = js(scan, """
        import { PrismaClient } from "@prisma/client";
        export async function GET() {
          const prisma = new PrismaClient();
          return Response.json(await prisma.user.count());
        }
        """, name="app/api/users/route.ts", pkg=PRISMA_PKG)
    assert [(r[0], r[3]) for r in res] == [("PGL012", "high")]


def test_neon_serverless_pool_per_request_is_fine(scan):
    res = js(scan, """
        import { Pool } from "@neondatabase/serverless";
        export async function GET() {
          const pool = new Pool({ connectionString: process.env.DATABASE_URL });
          const { rows } = await pool.query("SELECT id FROM t WHERE id = 1");
          await pool.end();
          return Response.json(rows);
        }
        """, name="app/api/x/route.ts")
    assert res == []


def test_pool_defaults_and_timeouts(scan):
    res = js(scan, """
        import { Pool } from "pg";
        import postgres from "postgres";
        export const pool = new Pool({ connectionString: process.env.DATABASE_URL });
        export const sql = postgres(process.env.DATABASE_URL!);
        """, name="src/db.ts")
    assert [(r[0], r[2]) for r in res] == [("PGL018", 3), ("PGL018", 4), ("PGL019", 3), ("PGL019", 4)]
    ok = js(scan, """
        import { Pool } from "pg";
        import postgres from "postgres";
        export const pool = new Pool({ connectionString: process.env.DATABASE_URL, max: 5, statement_timeout: 5000 });
        export const sql = postgres(process.env.DATABASE_URL!, { max: 5, connection: { statement_timeout: 5000 } });
        """, name="src/db.ts")
    assert ok == []


def test_role_statement_timeout_in_migrations_counts(scan):
    res = js(scan, 'import { Pool } from "pg";\nexport const pool = new Pool({ max: 5 });\n', name="src/db.ts",
             **{"db/migrations/001.sql": "alter role app set statement_timeout = '15s';\n"})
    assert res == []


def test_serverless_pooler_hint(scan):
    pkg = '{"dependencies": {"pg": "^8", "next": "^15"}}'
    code = ('import { Pool } from "pg";\n'
            "export const pool = new Pool({ max: 1, statement_timeout: 5000 });\n")
    assert ids(js(scan, code, name="lib/db.ts", pkg=pkg)) == ["PGL020"]
    hinted = js(scan, code, name="lib/db.ts", pkg=pkg,
                **{".env.example": "DATABASE_URL=postgres://u:p@ep-x-pooler.eu-central-1.aws.neon.tech/db\n"})
    assert hinted == []


# ---------------------------------------------------------------- PGL013
def test_n_plus_one_prisma_drizzle_and_raw(scan):
    res = js(scan, """
        import { prisma } from "@/lib/prisma";
        import { db } from "./db";
        import { eq } from "drizzle-orm";
        import { users } from "./schema";
        export async function a(posts: any[]) {
          for (const post of posts) {
            post.author = await prisma.user.findFirst({ where: { id: post.authorId } });
          }
          await Promise.all(posts.map(async (p) => db.select().from(users).where(eq(users.id, p.userId))));
          posts.forEach(async ({ id }) => { await db.query.comments.findMany({ where: eq(comments.postId, id) }); });
          for (let i = 0; i < posts.length; i++) {
            await pool.query("SELECT id FROM tags WHERE post_id = $1", [posts[i].id]);
          }
        }
        """, pkg='{"dependencies": {"@prisma/client": "^6", "drizzle-orm": "^0.36", "pg": "^8"}}')
    assert [(r[0], r[2]) for r in res] == [("PGL013", 7), ("PGL013", 9), ("PGL013", 10), ("PGL013", 12)]


def test_not_n_plus_one(scan):
    res = js(scan, """
        import { prisma } from "@/lib/prisma";
        export async function a(posts: any[], ids: string[]) {
          // Prisma batches findUnique calls made in the same tick (dataloader)
          const authors = await Promise.all(ids.map((id) => prisma.user.findUnique({ where: { id } })));
          // query does not depend on the loop variable
          for (const p of posts) {
            console.log(p.id);
          }
          const total = await prisma.post.count();
          // retry loop
          for (let attempt = 0; attempt < 3; attempt++) {
            try { return await prisma.post.findFirst({ where: { id: attempt } }); } catch {}
          }
          // pagination loop
          let cursor = 0;
          while (cursor >= 0) {
            const page = await prisma.post.findMany({ take: 100, where: { id: { gt: cursor } } });
            for (const row of page) { cursor = row.id; }
            if (page.length < 100) break;
          }
          return { authors, total };
        }
        """, pkg=PRISMA_PKG)
    assert res == []


# ---------------------------------------------------------------- PGL014 / 015
def test_unbounded_queries_in_handlers(scan):
    res = js(scan, """
        import express from "express";
        import { prisma } from "./prisma";
        const app = express();
        app.get("/posts", async (req, res) => {
          res.json(await prisma.post.findMany({ where: { published: true } }));
        });
        app.get("/all", async (req, res) => {
          res.json(await prisma.post.findMany());
        });
        app.get("/raw", async (req, res) => {
          res.json((await pool.query("SELECT id, title FROM posts WHERE published")).rows);
        });
        app.get("/ok", async (req, res) => {
          const a = await prisma.post.findMany({ take: 20 });
          const b = await prisma.post.findMany({ where: { id: { in: req.body.ids } } });
          const c = await prisma.plan.findMany();
          const d = (await pool.query("SELECT id FROM posts WHERE id = $1", [req.params.id])).rows;
          const e = (await pool.query("SELECT count(*) FROM posts")).rows;
          const f = (await pool.query("SELECT id FROM posts ORDER BY id LIMIT 20")).rows;
          res.json({ a, b, c, d, e, f });
        });
        export async function backgroundJob() {
          return prisma.post.findMany();
        }
        """, pkg=PRISMA_PKG)
    assert [(r[0], r[2]) for r in res] == [("PGL014", 5), ("PGL014", 8), ("PGL014", 11)]


def test_offset_pagination(scan):
    res = js(scan, """
        import { prisma } from "./prisma";
        export async function list(page: number, cursorId?: number) {
          await prisma.post.findMany({ take: 20, skip: page * 20 });
          await prisma.post.findMany({ take: 20, skip: 1, cursor: { id: cursorId } });
          await prisma.post.findMany({ take: 20, skip: 0 });
          await pool.query("SELECT id FROM posts ORDER BY id LIMIT 20 OFFSET $1", [page * 20]);
          await pool.query("SELECT id FROM posts ORDER BY id LIMIT 20 OFFSET 0");
        }
        """, pkg=PRISMA_PKG)
    assert [(r[0], r[2], r[3]) for r in res] == [("PGL015", 3, "info"), ("PGL015", 6, "info")]


# ---------------------------------------------------------------- PGL016 / 017
def test_select_star(scan):
    res = js(scan, """
        export const a = "SELECT * FROM users WHERE id = $1";
        export const b = `select u.* from users u join teams t on t.id = u.team_id`;
        export const c = "SELECT count(*) FROM users";
        export const d = "SELECT id FROM users WHERE EXISTS (SELECT * FROM teams WHERE teams.id = users.team_id)";
        export const e = "Select * of the options below";
        """)
    assert [(r[0], r[2]) for r in res] == [("PGL016", 1), ("PGL016", 2)]


def test_leading_wildcard_like(scan):
    res = js(scan, """
        import { prisma } from "./prisma";
        import { ilike } from "drizzle-orm";
        export async function s(q: string, db: any, users: any) {
          await prisma.user.findMany({ where: { name: { contains: q } }, take: 10 });
          await prisma.user.findMany({ where: { email: { endsWith: "@x.com" } }, take: 10 });
          await prisma.user.findMany({ where: { name: { startsWith: q } }, take: 10 });
          await db.select().from(users).where(ilike(users.bio, `%${q}%`)).limit(5);
          await db.select().from(users).where(ilike(users.handle, `${q}%`)).limit(5);
          await pool.query("SELECT id FROM users WHERE title ILIKE '%' || $1 || '%' LIMIT 5", [q]);
          await pool.query("SELECT id FROM users WHERE slug LIKE $1 LIMIT 5", [`%${q}%`]);
        }
        """, pkg='{"dependencies": {"@prisma/client": "^6", "drizzle-orm": "^0.36", "pg": "^8"}}')
    assert [(r[0], r[2]) for r in res] == [("PGL017", 4), ("PGL017", 5), ("PGL017", 7), ("PGL017", 9),
                                           ("PGL017", 10)]


def test_leading_wildcard_suppressed_by_trigram_index(scan):
    res = js(scan, """
        export const q = "SELECT id FROM users WHERE title ILIKE '%' || $1 || '%' LIMIT 5";
        """, **{"db/migrations/001.sql": "create extension if not exists pg_trgm;\n"
                                         "create table users (id int primary key, title text);\n"
                                         "create index users_title_trgm on users using gin (title gin_trgm_ops);\n"})
    assert res == []


def test_mysql_files_are_skipped(scan):
    res = js(scan, """
        import mysql from "mysql2/promise";
        export const run = (c: any, id: string) => c.query(`SELECT * FROM t WHERE id = ${id}`);
        """)
    assert res == []


def test_inline_ignore_in_js(scan):
    res = js(scan, """
        // pg-launch-check: ignore PGL016
        export const a = "SELECT * FROM users";
        export const b = "SELECT * FROM teams"; // pg-launch-check: ignore
        export const c = "SELECT * FROM orgs"; // pg-launch-check: ignore PGL011
        """)
    assert [(r[0], r[2]) for r in res] == [("PGL016", 4)]
