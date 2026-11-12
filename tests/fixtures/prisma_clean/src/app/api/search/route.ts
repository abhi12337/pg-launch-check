import { NextResponse } from "next/server";
import { prisma } from "@/lib/prisma";

export async function GET(request: Request) {
  const q = new URL(request.url).searchParams.get("q") ?? "";
  const pattern = `%${q}%`;
  const rows = await prisma.$queryRaw`
    SELECT id, title, slug FROM "Post" WHERE title ILIKE ${pattern} ORDER BY "createdAt" DESC LIMIT 20`;
  return NextResponse.json(rows);
}
