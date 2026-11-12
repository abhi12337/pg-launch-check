-- CreateIndex (alone in its migration: CONCURRENTLY cannot run inside a transaction)
CREATE INDEX CONCURRENTLY "Post_published_idx" ON "Post"("published");
