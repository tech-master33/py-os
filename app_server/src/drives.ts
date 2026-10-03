const JSON_HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "no-store",
};

const DRIVE_ID_PATTERN = /^[A-Za-z0-9_-]{32}$/;
const MiB = 1024 * 1024;
const MAX_STORAGE_BUDGET = 1_000_000_000;
const MAX_NAME_LENGTH = 64;
const MAX_PATH_BYTES = 1024;
const MAX_JSON_BYTES = 64 * 1024;
const MAX_LIST_ENTRIES = 1000;
const MAX_CHUNK_BYTES = 20 * MiB;
const MAX_FILE_BYTES = 1_000_000_000;
const UPLOAD_TTL_SECONDS = 24 * 60 * 60;
const SHA256_PATTERN = /^[a-f0-9]{64}$/;
const utf8 = new TextEncoder();

export interface DriveEnv {
  DB: D1Database;
  DRIVE_FILES?: KVNamespace;
  DRIVE_STORAGE_LIMIT_BYTES?: string;
}

interface DriveRow {
  id: string;
  name: string;
  quota_bytes: number;
  used_bytes: number;
}

interface DirectoryRow {
  path: string;
  created_at: string;
}

interface FileRow {
  path: string;
  size_bytes: number;
  sha256: string;
  object_id: string;
  chunk_count: number;
  deleting: number;
  updated_at: string;
}

interface UploadRow {
  id: string;
  drive_id: string;
  path: string;
  size_bytes: number;
  sha256: string;
  object_id: string;
  chunk_count: number;
  expires_at: number;
}

interface UploadChunkRow {
  chunk_index: number;
  size_bytes: number;
  sha256: string;
}

interface GarbageRow {
  object_id: string;
  chunk_count: number;
}

function json(data: unknown, status = 200): Response {
  return Response.json(data, { status, headers: JSON_HEADERS });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function validDriveId(value: string): boolean {
  return DRIVE_ID_PATTERN.test(value);
}

function createDriveId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(24));
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_");
}

function createObjectId(): string {
  return createDriveId();
}

function configuredBudget(env: DriveEnv): number | null {
  const raw = env.DRIVE_STORAGE_LIMIT_BYTES;
  if (typeof raw !== "string" || !/^(0|[1-9]\d*)$/.test(raw)) {
    return null;
  }
  const value = Number(raw);
  return Number.isSafeInteger(value) && value > 0 && value <= MAX_STORAGE_BUDGET
    ? value
    : null;
}

function normalizePath(value: unknown, allowRoot = false): string | null {
  if (
    typeof value !== "string" ||
    utf8.encode(value).byteLength > MAX_PATH_BYTES ||
    value.includes("\\") ||
    value.startsWith("/") ||
    /[\u0000-\u001f\u007f]/.test(value)
  ) {
    return null;
  }
  if (allowRoot && value === "") {
    return "";
  }
  const parts = value.split("/");
  if (
    parts.some(
      (part) =>
        !part ||
        part === "." ||
        part === ".." ||
        part.endsWith(".") ||
        part.endsWith(" ") ||
        /[<>:"|?*]/.test(part),
    )
  ) {
    return null;
  }
  return value;
}

async function readJson(request: Request): Promise<Record<string, unknown> | null> {
  const bytes = await readRequestBytes(request, MAX_JSON_BYTES);
  if (!bytes) {
    return null;
  }
  try {
    const value: unknown = JSON.parse(
      new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes),
    );
    return isRecord(value) ? value : null;
  } catch {
    return null;
  }
}

async function readRequestBytes(request: Request, limit: number): Promise<Uint8Array | null> {
  const contentLength = request.headers.get("content-length");
  if (contentLength !== null) {
    const declaredLength = Number(contentLength);
    if (!Number.isSafeInteger(declaredLength) || declaredLength < 0 || declaredLength > limit) {
      return null;
    }
  }
  if (!request.body) {
    return new Uint8Array();
  }
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let length = 0;
  while (true) {
    const result = await reader.read();
    if (result.done) {
      break;
    }
    length += result.value.byteLength;
    if (length > limit) {
      await reader.cancel();
      return null;
    }
    chunks.push(result.value);
  }
  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return bytes;
}

function validDriveName(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.trim().length > 0 &&
    value.trim().length <= MAX_NAME_LENGTH &&
    !/[\u0000-\u001f\u007f]/.test(value)
  );
}

async function getDrive(env: DriveEnv, id: string): Promise<DriveRow | null> {
  return env.DB.prepare(
    `SELECT id, name, quota_bytes, used_bytes
     FROM shared_drives
     WHERE id = ?`,
  )
    .bind(id)
    .first<DriveRow>();
}

function expectedChunkSize(upload: UploadRow, index: number): number {
  if (upload.size_bytes === 0) {
    return index === 0 ? 0 : -1;
  }
  const offset = index * MAX_CHUNK_BYTES;
  if (offset >= upload.size_bytes) {
    return -1;
  }
  return Math.min(MAX_CHUNK_BYTES, upload.size_bytes - offset);
}

async function getUpload(
  env: DriveEnv,
  driveId: string,
  uploadId: string,
): Promise<UploadRow | null> {
  return env.DB.prepare(
    `SELECT id, drive_id, path, size_bytes, sha256, object_id, chunk_count, expires_at
     FROM drive_uploads
     WHERE drive_id = ? AND id = ?`,
  )
    .bind(driveId, uploadId)
    .first<UploadRow>();
}

async function expireUploads(env: DriveEnv, driveId: string): Promise<void> {
  const expired = await env.DB.prepare(
    `SELECT id, object_id, chunk_count, size_bytes
     FROM drive_uploads
     WHERE drive_id = ? AND expires_at <= ?`,
  )
    .bind(driveId, Math.floor(Date.now() / 1000))
    .all<Pick<UploadRow, "id" | "object_id" | "chunk_count" | "size_bytes">>();

  for (const upload of expired.results) {
    for (let index = 0; index < upload.chunk_count; index += 1) {
      await env.DRIVE_FILES!.delete(`drive:${upload.object_id}:${index}`);
    }
    await env.DB.batch([
      env.DB.prepare(
        `UPDATE shared_drives
         SET reserved_bytes = MAX(0, reserved_bytes - ?)
         WHERE id = ?`,
      ).bind(upload.size_bytes, driveId),
      env.DB.prepare(`DELETE FROM drive_upload_chunks WHERE upload_id = ?`).bind(upload.id),
      env.DB.prepare(`DELETE FROM drive_uploads WHERE id = ?`).bind(upload.id),
    ]);
  }
}

async function cleanupGarbage(
  env: DriveEnv,
  driveId: string,
  limit = 10,
): Promise<boolean> {
  const garbage = await env.DB.prepare(
    `SELECT object_id, chunk_count
     FROM drive_garbage
     WHERE drive_id = ?
     ORDER BY created_at
     LIMIT ?`,
  )
    .bind(driveId, limit)
    .all<GarbageRow>();

  for (const item of garbage.results) {
    for (let index = 0; index < item.chunk_count; index += 1) {
      await env.DRIVE_FILES!.delete(`drive:${item.object_id}:${index}`);
    }
    await env.DB.prepare(
      `DELETE FROM drive_garbage WHERE drive_id = ? AND object_id = ?`,
    )
      .bind(driveId, item.object_id)
      .run();
  }
  const remaining = await env.DB.prepare(
    `SELECT 1 AS present FROM drive_garbage WHERE drive_id = ? LIMIT 1`,
  )
    .bind(driveId)
    .first();
  return Boolean(remaining);
}

function driveSummary(drive: DriveRow) {
  return {
    id: drive.id,
    name: drive.name,
    quota_bytes: drive.quota_bytes,
    used_bytes: drive.used_bytes,
  };
}

async function createDrive(request: Request, env: DriveEnv): Promise<Response> {
  const budget = configuredBudget(env);
  if (budget === null) {
    return json({ error: "drive_storage_unavailable" }, 503);
  }
  const body = await readJson(request);
  if (
    !body ||
    !validDriveName(body.name) ||
    !Number.isSafeInteger(body.quota_bytes) ||
    typeof body.quota_bytes !== "number" ||
    body.quota_bytes <= 0 ||
    body.quota_bytes % MiB !== 0 ||
    body.quota_bytes > budget
  ) {
    return json({ error: "invalid_drive_request" }, 400);
  }

  const id = createDriveId();
  const timestamp = new Date().toISOString();
  const quota = body.quota_bytes;
  const results = await env.DB.batch([
    env.DB.prepare(
      `UPDATE drive_capacity
       SET max_bytes = ?
       WHERE id = 1 AND allocated_bytes <= ?`,
    ).bind(budget, budget),
    env.DB.prepare(
      `UPDATE drive_capacity
       SET allocated_bytes = allocated_bytes + ?
       WHERE id = 1
         AND max_bytes = ?
         AND allocated_bytes + ? <= ?`,
    ).bind(quota, budget, quota, budget),
    env.DB.prepare(
      `INSERT INTO shared_drives (id, name, quota_bytes, created_at)
       SELECT ?, ?, ?, ?
       WHERE changes() = 1`,
    ).bind(id, body.name.trim(), quota, timestamp),
  ]);

  if (results[1].meta.changes !== 1) {
    return json({ error: "drive_quota_exhausted" }, 409);
  }
  if (results[2].meta.changes !== 1) {
    throw new Error("Drive quota was allocated without creating its drive record");
  }

  return json(
    { id, name: body.name.trim(), quota_bytes: quota, used_bytes: 0 },
    201,
  );
}

async function listEntries(
  env: DriveEnv,
  drive: DriveRow,
  path: string,
): Promise<Response> {
  const [directoryRows, fileRows] = await Promise.all([
    env.DB.prepare(
      `SELECT path, created_at FROM drive_directories
       WHERE drive_id = ?
       LIMIT 5001`,
    )
      .bind(drive.id)
      .all<DirectoryRow>(),
    env.DB.prepare(
      `SELECT path, size_bytes, updated_at FROM drive_files
       WHERE drive_id = ? AND deleting = 0
       LIMIT 5001`,
    )
      .bind(drive.id)
      .all<FileRow>(),
  ]);
  if (directoryRows.results.length > 5000 || fileRows.results.length > 5000) {
    return json({ error: "drive_directory_too_large" }, 413);
  }

  const prefix = path ? `${path}/` : "";
  const entries = new Map<
    string,
    { name: string; type: "directory" | "file"; size_bytes: number; updated_at: string }
  >();

  for (const directory of directoryRows.results) {
    if (!directory.path.startsWith(prefix)) {
      continue;
    }
    const remainder = directory.path.slice(prefix.length);
    const name = remainder.split("/", 1)[0];
    if (name && !entries.has(name)) {
      entries.set(name, {
        name,
        type: "directory",
        size_bytes: 0,
        updated_at: directory.created_at,
      });
    }
  }
  for (const file of fileRows.results) {
    if (!file.path.startsWith(prefix)) {
      continue;
    }
    const remainder = file.path.slice(prefix.length);
    if (remainder.includes("/")) {
      const name = remainder.split("/", 1)[0];
      if (name && !entries.has(name)) {
        const directory = directoryRows.results.find(
          (row) => row.path === (path ? `${path}/${name}` : name),
        );
        entries.set(name, {
          name,
          type: "directory",
          size_bytes: 0,
          updated_at: directory?.created_at ?? file.updated_at,
        });
      }
      continue;
    }
    if (remainder) {
      entries.set(remainder, {
        name: remainder,
        type: "file",
        size_bytes: file.size_bytes,
        updated_at: file.updated_at,
      });
    }
  }

  const ordered = [...entries.values()].sort((left, right) =>
    left.type === right.type
      ? left.name.localeCompare(right.name)
      : left.type === "directory"
        ? -1
        : 1,
  );
  if (ordered.length > MAX_LIST_ENTRIES) {
    return json({ error: "drive_directory_too_large" }, 413);
  }
  return json({ entries: ordered });
}

async function createDirectory(
  request: Request,
  env: DriveEnv,
  drive: DriveRow,
): Promise<Response> {
  const body = await readJson(request);
  const path = normalizePath(body?.path);
  if (path === null || path === "") {
    return json({ error: "invalid_drive_path" }, 400);
  }
  const parent = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
  if (parent) {
    const parentExists = await env.DB.prepare(
      `SELECT 1 AS present FROM drive_directories
       WHERE drive_id = ? AND path = ?`,
    )
      .bind(drive.id, parent)
      .first();
    if (!parentExists) {
      return json({ error: "drive_parent_not_found" }, 404);
    }
  }

  const fileCollision = await env.DB.prepare(
    `SELECT 1 AS present FROM drive_files WHERE drive_id = ? AND path = ?`,
  )
    .bind(drive.id, path)
    .first();
  if (fileCollision) {
    return json({ error: "drive_path_conflict" }, 409);
  }

  const result = await env.DB.prepare(
    `INSERT INTO drive_directories (drive_id, path, created_at)
     SELECT ?, ?, ?
     WHERE EXISTS (SELECT 1 FROM shared_drives WHERE id = ?)
       AND NOT EXISTS (
         SELECT 1 FROM drive_directories WHERE drive_id = ? AND path = ?
       )
       AND NOT EXISTS (
         SELECT 1 FROM drive_files WHERE drive_id = ? AND path = ?
       )
       AND NOT EXISTS (
         SELECT 1 FROM drive_uploads WHERE drive_id = ? AND path = ?
       )
       AND (? = '' OR EXISTS (
         SELECT 1 FROM drive_directories WHERE drive_id = ? AND path = ?
       ))
       AND NOT EXISTS (
         SELECT 1 FROM drive_files WHERE drive_id = ? AND path = ?
       )`,
  )
    .bind(
      drive.id,
      path,
      new Date().toISOString(),
      drive.id,
      drive.id,
      path,
      drive.id,
      path,
      drive.id,
      path,
      parent,
      drive.id,
      parent,
      drive.id,
      parent,
    )
    .run();
  if (result.meta.changes !== 1) {
    return json({ error: "drive_path_conflict" }, 409);
  }
  return json({ status: "created" }, 201);
}

async function createUpload(
  request: Request,
  env: DriveEnv,
  drive: DriveRow,
): Promise<Response> {
  await expireUploads(env, drive.id);
  const body = await readJson(request);
  const path = normalizePath(body?.path);
  const size = body?.size_bytes;
  const digest = body?.sha256;
  if (
    path === null ||
    path === "" ||
    typeof size !== "number" ||
    !Number.isSafeInteger(size) ||
    size < 0 ||
    size > MAX_FILE_BYTES ||
    typeof digest !== "string" ||
    !SHA256_PATTERN.test(digest)
  ) {
    return json({ error: "invalid_upload_request" }, 400);
  }
  const parent = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
  if (parent) {
    const directory = await env.DB.prepare(
      `SELECT 1 AS present FROM drive_directories WHERE drive_id = ? AND path = ?`,
    )
      .bind(drive.id, parent)
      .first();
    if (!directory) {
      return json({ error: "drive_parent_not_found" }, 404);
    }
  }
  const directoryCollision = await env.DB.prepare(
    `SELECT 1 AS present FROM drive_directories WHERE drive_id = ? AND path = ?`,
  )
    .bind(drive.id, path)
    .first();
  if (directoryCollision) {
    return json({ error: "drive_path_conflict" }, 409);
  }
  const objectId = createObjectId();
  const uploadId = createObjectId();
  const chunkCount = Math.max(1, Math.ceil(size / MAX_CHUNK_BYTES));
  const expiration = Math.floor(Date.now() / 1000) + UPLOAD_TTL_SECONDS;
  const result = await env.DB.batch([
    env.DB.prepare(
      `UPDATE shared_drives
       SET reserved_bytes = reserved_bytes + ?
       WHERE id = ?
         AND used_bytes + reserved_bytes -
           COALESCE((SELECT size_bytes FROM drive_files
                     WHERE drive_id = ? AND path = ? AND deleting = 0), 0) + ?
           <= quota_bytes
         AND NOT EXISTS (
           SELECT 1 FROM drive_uploads WHERE drive_id = ? AND path = ?
         )
         AND NOT EXISTS (
           SELECT 1 FROM drive_directories WHERE drive_id = ? AND path = ?
         )
         AND (? = '' OR EXISTS (
           SELECT 1 FROM drive_directories WHERE drive_id = ? AND path = ?
         ))
         AND NOT EXISTS (
           SELECT 1 FROM drive_files WHERE drive_id = ? AND path = ?
         )`,
    ).bind(
      size,
      drive.id,
      drive.id,
      path,
      size,
      drive.id,
      path,
      drive.id,
      path,
      parent,
      drive.id,
      parent,
      drive.id,
      parent,
    ),
    env.DB.prepare(
      `INSERT INTO drive_uploads
         (id, drive_id, path, size_bytes, sha256, object_id, chunk_count, expires_at)
       SELECT ?, ?, ?, ?, ?, ?, ?, ?
       WHERE changes() = 1`,
    ).bind(uploadId, drive.id, path, size, digest, objectId, chunkCount, expiration),
  ]);
  if (result[0].meta.changes !== 1) {
    const pending = await env.DB.prepare(
      `SELECT 1 AS present FROM drive_uploads WHERE drive_id = ? AND path = ?`,
    )
      .bind(drive.id, path)
      .first();
    return pending
      ? json({ error: "drive_path_busy" }, 409)
      : json({ error: "drive_quota_exhausted" }, 409);
  }
  if (result[1].meta.changes !== 1) {
    throw new Error("Drive upload reservation succeeded without a session record");
  }
  return json(
    { upload_id: uploadId, chunk_size: MAX_CHUNK_BYTES, chunk_count: chunkCount },
    201,
  );
}

async function putUploadChunk(
  request: Request,
  env: DriveEnv,
  driveId: string,
  uploadId: string,
  chunkIndex: number,
): Promise<Response> {
  const upload = await getUpload(env, driveId, uploadId);
  if (!upload) {
    return json({ error: "drive_upload_not_found" }, 404);
  }
  if (upload.expires_at <= Math.floor(Date.now() / 1000)) {
    await expireUploads(env, driveId);
    return json({ error: "drive_upload_expired" }, 410);
  }
  if (chunkIndex < 0 || chunkIndex >= upload.chunk_count) {
    return json({ error: "invalid_chunk_index" }, 400);
  }
  const expectedSize = expectedChunkSize(upload, chunkIndex);
  const contentLength = request.headers.get("content-length");
  const bytes = await readRequestBytes(request, MAX_CHUNK_BYTES);
  if (!bytes) {
    return json({ error: "chunk_too_large" }, 413);
  }
  const suppliedDigest = request.headers.get("x-chunk-sha256") ?? "";
  if (
    expectedSize < 0 ||
    bytes.byteLength !== expectedSize ||
    (contentLength !== null && Number(contentLength) !== expectedSize) ||
    bytes.byteLength > MAX_CHUNK_BYTES ||
    !SHA256_PATTERN.test(suppliedDigest)
  ) {
    return json({ error: "invalid_chunk" }, 400);
  }
  const actualDigest = Array.from(
    new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)),
    (byte) => byte.toString(16).padStart(2, "0"),
  ).join("");
  if (actualDigest !== suppliedDigest) {
    return json({ error: "chunk_digest_mismatch" }, 422);
  }

  const existing = await env.DB.prepare(
    `SELECT size_bytes, sha256 FROM drive_upload_chunks
     WHERE upload_id = ? AND chunk_index = ?`,
  )
    .bind(uploadId, chunkIndex)
    .first<UploadChunkRow>();
  if (existing) {
    return existing.size_bytes === bytes.byteLength && existing.sha256 === actualDigest
      ? json({ status: "already_uploaded" })
      : json({ error: "chunk_conflict" }, 409);
  }

  const objectKey = `drive:${upload.object_id}:${chunkIndex}`;
  await env.DRIVE_FILES!.put(objectKey, bytes);
  await env.DB.prepare(
    `INSERT INTO drive_upload_chunks (upload_id, chunk_index, size_bytes, sha256)
     VALUES (?, ?, ?, ?)`,
  )
    .bind(uploadId, chunkIndex, bytes.byteLength, actualDigest)
    .run();
  return json({ status: "uploaded" });
}

async function abortUpload(
  env: DriveEnv,
  driveId: string,
  uploadId: string,
): Promise<Response> {
  const upload = await getUpload(env, driveId, uploadId);
  if (!upload) {
    return json({ error: "drive_upload_not_found" }, 404);
  }
  for (let index = 0; index < upload.chunk_count; index += 1) {
    await env.DRIVE_FILES!.delete(`drive:${upload.object_id}:${index}`);
  }
  await env.DB.batch([
    env.DB.prepare(
      `UPDATE shared_drives
       SET reserved_bytes = MAX(0, reserved_bytes - ?)
       WHERE id = ?`,
    ).bind(upload.size_bytes, driveId),
    env.DB.prepare(`DELETE FROM drive_upload_chunks WHERE upload_id = ?`).bind(uploadId),
    env.DB.prepare(`DELETE FROM drive_uploads WHERE id = ?`).bind(uploadId),
  ]);
  return json({ status: "aborted" });
}

async function completeUpload(
  env: DriveEnv,
  drive: DriveRow,
  uploadId: string,
): Promise<Response> {
  const upload = await getUpload(env, drive.id, uploadId);
  if (!upload) {
    return json({ error: "drive_upload_not_found" }, 404);
  }
  if (upload.expires_at <= Math.floor(Date.now() / 1000)) {
    await expireUploads(env, drive.id);
    return json({ error: "drive_upload_expired" }, 410);
  }
  const chunks = await env.DB.prepare(
    `SELECT chunk_index, size_bytes, sha256
     FROM drive_upload_chunks
     WHERE upload_id = ?
     ORDER BY chunk_index`,
  )
    .bind(uploadId)
    .all<UploadChunkRow>();
  if (
    chunks.results.length !== upload.chunk_count ||
    chunks.results.some(
      (chunk, index) =>
        chunk.chunk_index !== index ||
        chunk.size_bytes !== expectedChunkSize(upload, index),
    ) ||
    chunks.results.reduce((total, chunk) => total + chunk.size_bytes, 0) !== upload.size_bytes
  ) {
    return json({ error: "drive_upload_incomplete" }, 409);
  }

  const currentFile = await env.DB.prepare(
    `SELECT object_id, chunk_count, size_bytes
     FROM drive_files WHERE drive_id = ? AND path = ? AND deleting = 0`,
  )
    .bind(drive.id, upload.path)
    .first<Pick<FileRow, "object_id" | "chunk_count" | "size_bytes">>();
  const timestamp = new Date().toISOString();
  const statements: D1PreparedStatement[] = [];
  if (currentFile) {
    statements.push(
      env.DB.prepare(
        `INSERT INTO drive_garbage (drive_id, object_id, chunk_count, created_at)
         VALUES (?, ?, ?, ?)`,
      ).bind(drive.id, currentFile.object_id, currentFile.chunk_count, timestamp),
    );
  }
  statements.push(
    env.DB.prepare(
      `INSERT INTO drive_files
         (drive_id, path, size_bytes, sha256, object_id, chunk_count, deleting, updated_at)
       VALUES (?, ?, ?, ?, ?, ?, 0, ?)
       ON CONFLICT (drive_id, path) DO UPDATE SET
         size_bytes = excluded.size_bytes,
         sha256 = excluded.sha256,
         object_id = excluded.object_id,
         chunk_count = excluded.chunk_count,
         deleting = 0,
         updated_at = excluded.updated_at`,
    ).bind(
      drive.id,
      upload.path,
      upload.size_bytes,
      upload.sha256,
      upload.object_id,
      upload.chunk_count,
      timestamp,
    ),
  );
  statements.push(
    env.DB.prepare(
      `UPDATE shared_drives
       SET used_bytes = used_bytes - ? + ?,
           reserved_bytes = MAX(0, reserved_bytes - ?)
       WHERE id = ? AND used_bytes >= ? AND reserved_bytes >= ?`,
    ).bind(
      currentFile?.size_bytes ?? 0,
      upload.size_bytes,
      upload.size_bytes,
      drive.id,
      currentFile?.size_bytes ?? 0,
      upload.size_bytes,
    ),
  );
  statements.push(
    env.DB.prepare(`DELETE FROM drive_upload_chunks WHERE upload_id = ?`).bind(uploadId),
    env.DB.prepare(`DELETE FROM drive_uploads WHERE id = ?`).bind(uploadId),
  );
  const results = await env.DB.batch(statements);
  const usageResult = results[results.length - 3];
  if (usageResult.meta.changes !== 1) {
    throw new Error("Published shared-drive file without updating quota accounting");
  }
  const cleanupPending = await cleanupGarbage(env, drive.id);
  return json({ status: "complete", cleanup_pending: cleanupPending });
}

async function downloadFile(
  request: Request,
  env: DriveEnv,
  drive: DriveRow,
): Promise<Response> {
  const path = normalizePath(new URL(request.url).searchParams.get("path"));
  if (path === null || path === "") {
    return json({ error: "invalid_drive_path" }, 400);
  }
  const file = await env.DB.prepare(
    `SELECT path, size_bytes, sha256, object_id, chunk_count, deleting, updated_at
     FROM drive_files WHERE drive_id = ? AND path = ?`,
  )
    .bind(drive.id, path)
    .first<FileRow>();
  if (!file || file.deleting) {
    return json({ error: "drive_file_not_found" }, 404);
  }

  let index = 0;
  const body = new ReadableStream<Uint8Array>({
    pull: async (controller) => {
      if (index >= file.chunk_count) {
        controller.close();
        return;
      }
      const value = await env.DRIVE_FILES!.get(`drive:${file.object_id}:${index}`, "arrayBuffer");
      if (!value) {
        controller.error(new Error("Shared-drive file chunk is missing"));
        return;
      }
      controller.enqueue(new Uint8Array(value));
      index += 1;
    },
  });
  return new Response(body, {
    headers: {
      "content-type": "application/octet-stream",
      "content-length": String(file.size_bytes),
      "cache-control": "no-store",
      "x-file-size-bytes": String(file.size_bytes),
      "x-file-sha256": file.sha256,
      "x-file-updated-at": file.updated_at,
    },
  });
}

async function deleteEntry(
  request: Request,
  env: DriveEnv,
  drive: DriveRow,
): Promise<Response> {
  const path = normalizePath(new URL(request.url).searchParams.get("path"));
  if (path === null || path === "") {
    return json({ error: "invalid_drive_path" }, 400);
  }
  const directory = await env.DB.prepare(
    `SELECT path FROM drive_directories WHERE drive_id = ? AND path = ?`,
  )
    .bind(drive.id, path)
    .first<{ path: string }>();
  if (directory) {
    const prefix = `${path}/`;
    const [childDirectories, childFiles] = await Promise.all([
      env.DB.prepare(
        `SELECT 1 AS present FROM drive_directories
         WHERE drive_id = ? AND substr(path, 1, ?) = ? LIMIT 1`,
      )
        .bind(drive.id, prefix.length, prefix)
        .first(),
      env.DB.prepare(
        `SELECT 1 AS present FROM drive_files
         WHERE drive_id = ? AND substr(path, 1, ?) = ? LIMIT 1`,
      )
        .bind(drive.id, prefix.length, prefix)
        .first(),
    ]);
    if (childDirectories || childFiles) {
      return json({ error: "drive_directory_not_empty" }, 409);
    }
    const removed = await env.DB.prepare(
      `DELETE FROM drive_directories
       WHERE drive_id = ? AND path = ?
         AND NOT EXISTS (
           SELECT 1 FROM drive_directories
           WHERE drive_id = ? AND substr(path, 1, ?) = ?
         )
         AND NOT EXISTS (
           SELECT 1 FROM drive_files
           WHERE drive_id = ? AND substr(path, 1, ?) = ?
         )
         AND NOT EXISTS (
           SELECT 1 FROM drive_uploads
           WHERE drive_id = ? AND substr(path, 1, ?) = ?
         )`,
    )
      .bind(
        drive.id,
        path,
        drive.id,
        prefix.length,
        prefix,
        drive.id,
        prefix.length,
        prefix,
        drive.id,
        prefix.length,
        prefix,
      )
      .run();
    if (removed.meta.changes !== 1) {
      return json({ error: "drive_directory_not_empty" }, 409);
    }
    return json({ status: "deleted" });
  }

  const file = await env.DB.prepare(
    `SELECT path, size_bytes, sha256, object_id, chunk_count, deleting, updated_at
     FROM drive_files WHERE drive_id = ? AND path = ?`,
  )
    .bind(drive.id, path)
    .first<FileRow>();
  if (!file) {
    return json({ error: "drive_entry_not_found" }, 404);
  }
  const pendingUpload = await env.DB.prepare(
    `SELECT 1 AS present FROM drive_uploads WHERE drive_id = ? AND path = ?`,
  )
    .bind(drive.id, path)
    .first();
  if (pendingUpload) {
    return json({ error: "drive_path_busy" }, 409);
  }
  const results = await env.DB.batch([
    env.DB.prepare(
      `INSERT INTO drive_garbage (drive_id, object_id, chunk_count, created_at)
       SELECT ?, object_id, chunk_count, ?
       FROM drive_files
       WHERE drive_id = ? AND path = ?
         AND NOT EXISTS (
           SELECT 1 FROM drive_uploads WHERE drive_id = ? AND path = ?
         )
       ON CONFLICT (object_id) DO NOTHING`,
    ).bind(drive.id, new Date().toISOString(), drive.id, path, drive.id, path),
    env.DB.prepare(
      `DELETE FROM drive_files
       WHERE drive_id = ? AND path = ?
         AND NOT EXISTS (
           SELECT 1 FROM drive_uploads WHERE drive_id = ? AND path = ?
         )`,
    ).bind(drive.id, path, drive.id, path),
    env.DB.prepare(
      `UPDATE shared_drives
       SET used_bytes = used_bytes - ?
       WHERE id = ? AND used_bytes >= ? AND changes() = 1`,
    ).bind(file.size_bytes, drive.id, file.size_bytes),
  ]);
  if (results[1].meta.changes !== 1) {
    return json({ error: "drive_path_busy" }, 409);
  }
  if (results[2].meta.changes !== 1) {
    throw new Error("Deleted shared-drive file without updating quota accounting");
  }
  const cleanupPending = await cleanupGarbage(env, drive.id);
  return json(
    { status: "deleted", cleanup_pending: cleanupPending },
    cleanupPending ? 202 : 200,
  );
}

async function deleteEmptyDrive(
  env: DriveEnv,
  drive: DriveRow,
): Promise<Response> {
  const results = await env.DB.batch([
    env.DB.prepare(
      `DELETE FROM shared_drives
       WHERE id = ?
         AND used_bytes = 0
         AND reserved_bytes = 0
         AND NOT EXISTS (SELECT 1 FROM drive_directories WHERE drive_id = ?)
         AND NOT EXISTS (SELECT 1 FROM drive_files WHERE drive_id = ?)
         AND NOT EXISTS (SELECT 1 FROM drive_garbage WHERE drive_id = ?)
         AND NOT EXISTS (SELECT 1 FROM drive_uploads WHERE drive_id = ?)`,
    ).bind(drive.id, drive.id, drive.id, drive.id, drive.id),
    env.DB.prepare(
      `UPDATE drive_capacity
       SET allocated_bytes = allocated_bytes - ?
       WHERE id = 1 AND changes() = 1 AND allocated_bytes >= ?`,
    ).bind(drive.quota_bytes, drive.quota_bytes),
  ]);
  if (results[0].meta.changes !== 1) {
    const existing = await getDrive(env, drive.id);
    return existing
      ? json({ error: "drive_not_empty" }, 409)
      : json({ error: "drive_not_found" }, 404);
  }
  if (results[1].meta.changes !== 1) {
    throw new Error("Drive was removed without releasing its quota allocation");
  }
  return json({ status: "deleted" });
}

export async function handleDriveRequest(
  request: Request,
  env: DriveEnv,
): Promise<Response> {
  if (!env.DRIVE_FILES || configuredBudget(env) === null) {
    return json({ error: "drive_storage_unavailable" }, 503);
  }

  const url = new URL(request.url);
  const parts = url.pathname.split("/").filter(Boolean);
  if (parts[0] !== "v1" || parts[1] !== "drives") {
    return json({ error: "not_found" }, 404);
  }
  if (request.method === "POST" && parts.length === 2) {
    return createDrive(request, env);
  }
  if (parts.length < 3 || !validDriveId(parts[2])) {
    return json({ error: "drive_not_found" }, 404);
  }
  const drive = await getDrive(env, parts[2]);
  if (!drive) {
    return json({ error: "drive_not_found" }, 404);
  }
  await expireUploads(env, drive.id);
  await cleanupGarbage(env, drive.id);
  if (request.method === "GET" && parts.length === 3) {
    return json(driveSummary(drive));
  }
  if (request.method === "DELETE" && parts.length === 3) {
    return deleteEmptyDrive(env, drive);
  }
  if (request.method === "POST" && parts.length === 4 && parts[3] === "uploads") {
    return createUpload(request, env, drive);
  }
  if (
    request.method === "PUT" &&
    parts.length === 6 &&
    parts[3] === "uploads" &&
    validDriveId(parts[4]) &&
    /^(0|[1-9]\d*)$/.test(parts[5])
  ) {
    const index = Number(parts[5]);
    return Number.isSafeInteger(index)
      ? putUploadChunk(request, env, drive.id, parts[4], index)
      : json({ error: "invalid_chunk_index" }, 400);
  }
  if (
    request.method === "POST" &&
    parts.length === 6 &&
    parts[3] === "uploads" &&
    validDriveId(parts[4]) &&
    parts[5] === "complete"
  ) {
    return completeUpload(env, drive, parts[4]);
  }
  if (
    request.method === "DELETE" &&
    parts.length === 5 &&
    parts[3] === "uploads" &&
    validDriveId(parts[4])
  ) {
    return abortUpload(env, drive.id, parts[4]);
  }
  if (request.method === "GET" && parts.length === 4 && parts[3] === "files") {
    return downloadFile(request, env, drive);
  }
  if (request.method === "GET" && parts.length === 4 && parts[3] === "entries") {
    const path = normalizePath(url.searchParams.get("path") ?? "", true);
    if (path === null) {
      return json({ error: "invalid_drive_path" }, 400);
    }
    if (path) {
      const directory = await env.DB.prepare(
        `SELECT 1 AS present FROM drive_directories WHERE drive_id = ? AND path = ?`,
      )
        .bind(drive.id, path)
        .first();
      if (!directory) {
        return json({ error: "drive_directory_not_found" }, 404);
      }
    }
    return listEntries(env, drive, path);
  }
  if (request.method === "POST" && parts.length === 4 && parts[3] === "directories") {
    return createDirectory(request, env, drive);
  }
  if (request.method === "DELETE" && parts.length === 4 && parts[3] === "entries") {
    return deleteEntry(request, env, drive);
  }
  return json({ error: "not_found" }, 404);
}
