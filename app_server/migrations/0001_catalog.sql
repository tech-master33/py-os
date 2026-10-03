CREATE TABLE apps (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    version TEXT NOT NULL,
    min_pyos_version TEXT NOT NULL,
    published_at TEXT NOT NULL
) STRICT;

CREATE TABLE app_versions (
    app_id TEXT NOT NULL,
    version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'published')),
    manifest TEXT NOT NULL,
    signature TEXT NOT NULL,
    package_size INTEGER NOT NULL,
    expanded_size INTEGER NOT NULL,
    chunk_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    published_at TEXT,
    PRIMARY KEY (app_id, version)
) STRICT;

CREATE INDEX app_versions_published
    ON app_versions (app_id, status, published_at DESC);

CREATE TABLE app_chunks (
    app_id TEXT NOT NULL,
    version TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    content BLOB NOT NULL,
    PRIMARY KEY (app_id, version, chunk_index),
    FOREIGN KEY (app_id, version) REFERENCES app_versions (app_id, version)
) STRICT;
