SET lock_timeout = '5s';

-- AlterTable
ALTER TABLE "Post" ADD COLUMN "published" BOOLEAN NOT NULL DEFAULT false;
