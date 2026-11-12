SET lock_timeout = '5s';--> statement-breakpoint
ALTER TABLE "members" ADD COLUMN "role" text DEFAULT 'member' NOT NULL;
