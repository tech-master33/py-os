CREATE TABLE app_chunks (
    app_id TEXT NOT NULL,
    version TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    content BLOB NOT NULL,
    PRIMARY KEY (app_id, version, chunk_index)
) STRICT;
