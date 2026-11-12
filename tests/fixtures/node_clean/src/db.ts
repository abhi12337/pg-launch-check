import { Pool } from "pg";
import { drizzle } from "drizzle-orm/node-postgres";

export const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
  max: 10,
  idleTimeoutMillis: 30_000,
  statement_timeout: 15_000,
});
export const db = drizzle(pool);
