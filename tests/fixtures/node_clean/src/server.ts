import express from "express";
import { and, asc, eq, gt, ilike } from "drizzle-orm";
import { db, pool } from "./db";
import { members } from "./schema";

const app = express();
app.use(express.json());

app.get("/teams/:id/members", async (req, res) => {
  const { rows } = await pool.query(
    "SELECT id, email, role FROM members WHERE team_id = $1 ORDER BY id LIMIT 100",
    [req.params.id],
  );
  res.json(rows);
});

app.get("/members", async (req, res) => {
  const after = Number(req.query.after ?? 0);
  const rows = await db.select().from(members).where(gt(members.id, after)).orderBy(asc(members.id)).limit(50);
  res.json(rows);
});

app.get("/members/search", async (req, res) => {
  const q = String(req.query.q ?? "");
  const rows = await db
    .select({ id: members.id, email: members.email })
    .from(members)
    .where(and(eq(members.role, "member"), ilike(members.email, `%${q}%`)))
    .limit(50);
  res.json(rows);
});

app.get("/teams", async (req, res) => {
  const after = Number(req.query.after ?? 0);
  const { rows } = await pool.query(
    `SELECT t.id, t.name, coalesce(json_agg(json_build_object('id', m.id, 'email', m.email))
            FILTER (WHERE m.id IS NOT NULL), '[]') AS members
       FROM teams t LEFT JOIN members m ON m.team_id = t.id
      WHERE t.id > $1
      GROUP BY t.id ORDER BY t.id LIMIT 20`,
    [after],
  );
  res.json(rows);
});

app.post("/teams/:id/rename", async (req, res) => {
  await pool.query("UPDATE teams SET name = $1 WHERE id = $2", [req.body.name, req.params.id]);
  res.sendStatus(204);
});

app.listen(3000);
