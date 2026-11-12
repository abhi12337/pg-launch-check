"""schema.prisma: relation indexes and schema design rules."""

from conftest import ids

DS = 'datasource db {\n  provider = "postgresql"\n  url      = env("DATABASE_URL")\n}\n\n'


def prisma(scan, models: str, **extra):
    return scan({"prisma/schema.prisma": DS + models, **extra})


def test_relation_without_index(scan):
    res = prisma(scan, """
model User {
  id    Int    @id @default(autoincrement())
  posts Post[]
}

model Post {
  id       Int  @id @default(autoincrement())
  author   User @relation(fields: [authorId], references: [id])
  authorId Int
}
""")
    assert [(r[0], r[2]) for r in res] == [("PGL001", 14)]


def test_relation_covered_by_index_id_unique_or_compound(scan):
    res = prisma(scan, """
model User {
  id      Int      @id @default(autoincrement())
  posts   Post[]
  profile Profile?
  members Member[]
}

model Team {
  id      Int      @id
  members Member[]
}

model Post {
  id       Int  @id @default(autoincrement())
  author   User @relation(fields: [authorId], references: [id])
  authorId Int

  @@index([authorId, id(sort: Desc)], map: "post_author")
}

model Profile {
  id     Int  @id
  user   User @relation(fields: [userId], references: [id])
  userId Int  @unique
}

model Member {
  user   User @relation(fields: [userId], references: [id])
  userId Int
  team   Team @relation(fields: [teamId], references: [id])
  teamId Int

  @@id([userId, teamId])
  @@index([teamId])
}
""")
    assert res == []


def test_relation_index_created_in_migration_sql_counts(scan):
    res = prisma(scan, """
model User {
  id    Int    @id
  posts Post[]
}

model Post {
  id       Int  @id
  author   User @relation(fields: [authorId], references: [id])
  authorId Int  @map("author_id")

  @@map("posts")
}
""", **{
        "prisma/migrations/migration_lock.toml": 'provider = "postgresql"\n',
        "prisma/migrations/1_init/migration.sql": 'CREATE TABLE "posts" ("id" INTEGER NOT NULL, "author_id" INTEGER NOT '
                                                  'NULL, CONSTRAINT "posts_pkey" PRIMARY KEY ("id"));\n'
                                                  'CREATE INDEX "posts_author_id_idx" ON "posts"("author_id");\n',
    })
    assert res == []


def test_prisma_managed_sql_does_not_double_report(scan):
    res = prisma(scan, """
model User {
  id    Int    @id
  posts Post[]
}

model Post {
  id       Int  @id
  author   User @relation(fields: [authorId], references: [id])
  authorId Int
}
""", **{
        "prisma/migrations/migration_lock.toml": 'provider = "postgresql"\n',
        "prisma/migrations/1_init/migration.sql": 'CREATE TABLE "Post" ("id" INTEGER NOT NULL, "authorId" INTEGER NOT '
                                                  'NULL, "createdAt" TIMESTAMP(3) NOT NULL, CONSTRAINT "Post_pkey" '
                                                  'PRIMARY KEY ("id"));\nALTER TABLE "Post" ADD CONSTRAINT '
                                                  '"Post_authorId_fkey" FOREIGN KEY ("authorId") REFERENCES "User"("id");\n',
    })
    assert [(r[0], r[1]) for r in res] == [("PGL001", "prisma/schema.prisma")]


def test_design_rules(scan_findings):
    fs = scan_findings({"prisma/schema.prisma": DS + """
model Account {
  id        String   @id @default(uuid())
  balance   Float
  ratio     Float
  createdAt DateTime @default(now())
  seenAt    DateTime @db.Timestamptz(3)
  birthday  DateTime @db.Date
}

model Clean {
  id        String   @id @default(uuid()) @db.Uuid
  amount    Decimal  @db.Decimal(12, 2)
  createdAt DateTime @default(now()) @db.Timestamptz(3)
}

model Lookup {
  code String @unique
}

view Totals {
  total Float
}
"""})
    got = sorted((f.rule_id, f.line) for f in fs)
    assert got == [("PGL007", 22), ("PGL008", 8), ("PGL009", 9), ("PGL010", 11)]
    fix = next(f.fix for f in fs if f.rule_id == "PGL008")
    assert "@db.Uuid" in fix and "id String @id" in fix


def test_mysql_datasource_is_ignored(scan):
    res = scan({"prisma/schema.prisma": 'datasource db {\n  provider = "mysql"\n  url = env("X")\n}\n'
                                        "model P {\n  id Int @id\n  u U @relation(fields: [uid], references: [id])\n"
                                        "  uid Int\n  price Float\n}\n"})
    assert res == []


def test_relation_mode_prisma_message(scan_findings):
    fs = scan_findings({"prisma/schema.prisma": 'datasource db {\n  provider = "postgresql"\n  url = env("X")\n'
                                                '  relationMode = "prisma"\n}\n'
                                                "model P {\n  id Int @id\n  u U @relation(fields: [uid], references: [id])\n"
                                                "  uid Int\n}\n"})
    assert ids([(f.rule_id,) for f in fs]) == ["PGL001"]
    assert 'relationMode = "prisma"' in fs[0].message
    assert "@@index([uid])" in fs[0].fix


def test_trigram_index_in_schema_suppresses_contains(scan):
    res = scan({
        "prisma/schema.prisma": DS + "model Post {\n  id Int @id\n  title String\n"
                                     '  @@index([title(ops: raw("gin_trgm_ops"))], type: Gin)\n}\n',
        "src/app/api/route.ts": 'import { prisma } from "@/lib/prisma";\n'
                                "export async function GET() {\n"
                                '  return prisma.post.findMany({ where: { title: { contains: "x" } }, take: 10 });\n}\n',
    })
    assert res == []


def test_inline_ignore_in_prisma(scan):
    res = prisma(scan, """
model User {
  id    Int    @id
  posts Post[]
}

model Post {
  id       Int  @id
  // pg-launch-check: ignore PGL001
  author   User @relation(fields: [authorId], references: [id])
  authorId Int
}
""")
    assert res == []
