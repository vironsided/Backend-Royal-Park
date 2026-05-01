-- Run once against PostgreSQL (RoyalPark DB) after deploying model changes.
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS appeal_workflow VARCHAR(40);
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS staff_message TEXT;
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS workflow_updated_at TIMESTAMP WITHOUT TIME ZONE;
