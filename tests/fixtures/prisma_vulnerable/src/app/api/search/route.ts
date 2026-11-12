import { NextResponse } from "next/server";
import { prisma } from "@/lib/prisma";

export async function GET(request: Request) {
  const q = new URL(request.url).searchParams.get("q") ?? "";
  const rows = await prisma.$queryRawUnsafe(
    `SELECT * FROM "Post" WHERE title ILIKE '%${q}%' ORDER BY "createdAt" DESC LIMIT 20`,
  );
  return NextResponse.json(rows);
}
