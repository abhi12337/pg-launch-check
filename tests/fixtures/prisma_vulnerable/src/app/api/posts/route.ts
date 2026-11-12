import { NextResponse } from "next/server";
import { PrismaClient } from "@prisma/client";

export async function GET(request: Request) {
  const prisma = new PrismaClient();
  const { searchParams } = new URL(request.url);
  const q = searchParams.get("q") ?? "";
  const page = Number(searchParams.get("page") ?? "0");

  const posts = await prisma.post.findMany({
    where: { title: { contains: q, mode: "insensitive" } },
    orderBy: { createdAt: "desc" },
    take: 20,
    skip: page * 20,
  });

  const result = [];
  for (const post of posts) {
    const author = await prisma.user.findUnique({ where: { id: post.authorId } });
    result.push({ ...post, author });
  }
  return NextResponse.json(result);
}
