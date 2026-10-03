ALTER TABLE app_versions
ADD COLUMN storage_shard INTEGER NOT NULL DEFAULT -1
CHECK (storage_shard BETWEEN -1 AND 6);
