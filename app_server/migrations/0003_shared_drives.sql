CREATE TABLE drive_capacity (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    max_bytes INTEGER NOT NULL CHECK (max_bytes >= 0),
    allocated_bytes INTEGER NOT NULL CHECK (allocated_bytes >= 0)
) STRICT;

INSERT INTO drive_capacity (id, max_bytes, allocated_bytes)
VALUES (1, 1000000000, 0);

CREATE TABLE shared_drives (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    quota_bytes INTEGER NOT NULL CHECK (quota_bytes > 0),
    used_bytes INTEGER NOT NULL DEFAULT 0 CHECK (used_bytes >= 0),
    reserved_bytes INTEGER NOT NULL DEFAULT 0 CHECK (reserved_bytes >= 0),
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE drive_directories (
    drive_id TEXT NOT NULL,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (drive_id, path),
    FOREIGN KEY (drive_id) REFERENCES shared_drives (id)
) STRICT;

CREATE TABLE drive_files (
    drive_id TEXT NOT NULL,
    path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    sha256 TEXT NOT NULL,
    object_id TEXT NOT NULL UNIQUE,
    chunk_count INTEGER NOT NULL CHECK (chunk_count > 0),
    deleting INTEGER NOT NULL DEFAULT 0 CHECK (deleting IN (0, 1)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (drive_id, path),
    FOREIGN KEY (drive_id) REFERENCES shared_drives (id)
) STRICT;

CREATE TABLE drive_garbage (
    drive_id TEXT NOT NULL,
    object_id TEXT PRIMARY KEY,
    chunk_count INTEGER NOT NULL CHECK (chunk_count > 0),
    created_at TEXT NOT NULL,
    FOREIGN KEY (drive_id) REFERENCES shared_drives (id)
) STRICT;

CREATE TABLE drive_uploads (
    id TEXT PRIMARY KEY,
    drive_id TEXT NOT NULL,
    path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    sha256 TEXT NOT NULL,
    object_id TEXT NOT NULL UNIQUE,
    chunk_count INTEGER NOT NULL CHECK (chunk_count > 0),
    expires_at INTEGER NOT NULL,
    FOREIGN KEY (drive_id) REFERENCES shared_drives (id)
) STRICT;

CREATE INDEX drive_uploads_expiration
    ON drive_uploads (drive_id, expires_at);

CREATE TABLE drive_upload_chunks (
    upload_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    sha256 TEXT NOT NULL,
    PRIMARY KEY (upload_id, chunk_index),
    FOREIGN KEY (upload_id) REFERENCES drive_uploads (id)
) STRICT;
