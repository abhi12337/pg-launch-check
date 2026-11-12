ALTER TABLE "members" ADD COLUMN "role" text DEFAULT 'member' NOT NULL;--> statement-breakpoint
CREATE INDEX IF NOT EXISTS "members_email_idx" ON "members" USING btree ("email");
