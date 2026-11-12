import express from "express";
import { Pool } from "pg";
import { ilike } from "drizzle-orm";
import { db, pool } from "./db";
import { members } from "./schema";

const app = express();
app.use(express.json());

app.get("/teams/:id/members", async (req, res) => {
  const client = new Pool({ connectionString: process.env.DATABASE_URL });
  const { rows } = await client.query(`SELECT * FROM members WHERE team_id = ${req.params.id}`);
  res.json(rows);
});

app.get("/members", async (_req, res) => {
  const rows = await db.select().from(members);
  res.json(rows);
});

app.get("/members/search", async (req, res) => {
  const q = String(req.query.q ?? "");
  const rows = await db.select().from(members).where(ilike(members.email, `%${q}%`)).limit(50);
  res.json(rows);
});

app.get("/teams", async (req, res) => {
  const page = Number(req.query.page ?? 0);
  const { rows: teamRows } = await pool.query("SELECT id, name FROM teams ORDER BY id LIMIT 20 OFFSET $1", [page * 20]);
  const out = [];
  for (const team of teamRows) {
    const { rows: m } = await pool.query("SELECT id, email FROM members WHERE team_id = $1", [team.id]);
    out.push({ ...team, members: m });
  }
  res.json(out);
});

app.post("/teams/:id/rename", async (req, res) => {
  await pool.query("UPDATE teams SET name = '" + req.body.name + "' WHERE id = $1", [req.params.id]);
  res.sendStatus(204);
});

app.listen(3000);
