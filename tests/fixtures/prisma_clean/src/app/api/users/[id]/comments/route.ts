import { NextResponse } from "next/server";
import { prisma } from "@/lib/prisma";

export async function GET(_req: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const comments = await prisma.comment.findMany({
    where: { authorId: id },
    orderBy: { id: "desc" },
    take: 50,
  });
  return NextResponse.json(comments);
}
