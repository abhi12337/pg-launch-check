import { NextResponse } from "next/server";
import { prisma } from "@/lib/prisma";

export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const q = searchParams.get("q") ?? "";
  const cursor = searchParams.get("cursor");

  const posts = await prisma.post.findMany({
    where: { title: { contains: q, mode: "insensitive" } },
    include: { author: { select: { id: true, name: true } } },
    orderBy: { id: "desc" },
    take: 20,
    ...(cursor ? { cursor: { id: Number(cursor) }, skip: 1 } : {}),
  });
  return NextResponse.json(posts);
}
