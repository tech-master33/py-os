const JSON_HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "no-store",
};

const APP_ID_PATTERN = /^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/;
const VERSION_PATTERN =
  /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/;
const DIGEST_PATTERN = /^[a-f0-9]{64}$/;
const MAX_PACKAGE_SIZE = 100 * 1024 * 1024;
const CHUNK_SIZE = 1024 * 1024;
const MAX_MANIFEST_SIZE = 1024 * 1024;
const MAX_FILES = 5_000;
const WINDOWS_RESERVED_NAME = /^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])[ .]*(?:\.|$)/i;
const utf8 = new TextEncoder();

export interface CatalogEnv {
  DB: D1Database;
  APP_DB_0?: D1Database;
  APP_DB_1?: D1Database;
  APP_DB_2?: D1Database;
  APP_DB_3?: D1Database;
  APP_DB_4?: D1Database;
  APP_DB_5?: D1Database;
  APP_DB_6?: D1Database;
  PUBLISH_TOKEN: string;
  PUBLISH_PUBLIC_KEY: string;
}

interface AppRow {
  id: string;
  name: string;
  description: string;
  version: string;
  min_pyos_version: string;
  package_size: number;
  chunk_size: number;
  chunk_count: number;
}

interface VersionRow {
  manifest: string;
  signature: string;
  storage_shard: number;
}

interface ChunkRow {
  content: number[] | ArrayBuffer | ArrayBufferView;
}

interface ManifestFile {
  path: string;
  size: number;
  sha256: string;
}

interface ReleaseManifest {
  app_id: string;
  version: string;
  name: string;
  description: string;
  min_pyos_version: string;
  entry_point: string;
  files: ManifestFile[];
  archive_size: number;
  expanded_size: number;
  archive_sha256: string;
  chunk_size: number;
  chunk_hashes: string[];
}

interface PendingVersionRow {
  status: "pending" | "published";
  manifest: string;
  signature: string;
  package_size: number;
  expanded_size: number;
  chunk_count: number;
  storage_shard: number;
}

function json(data: unknown, status = 200): Response {
  return Response.json(data, { status, headers: JSON_HEADERS });
}

function validateAppId(value: string): boolean {
  return APP_ID_PATTERN.test(value);
}

function validateVersion(value: string): boolean {
  return VERSION_PATTERN.test(value);
}

function parseChunkIndex(value: string): number | null {
  if (!/^(0|[1-9]\d*)$/.test(value)) {
    return null;
  }

  const index = Number(value);
  return Number.isSafeInteger(index) ? index : null;
}

function shardForAppId(appId: string): number {
  let hash = 0x811c9dc5;
  for (const byte of utf8.encode(appId)) {
    hash = Math.imul(hash ^ byte, 0x01000193);
  }
  return (hash >>> 0) % 7;
}

function getShard(env: CatalogEnv, shardIndex: number): D1Database | null {
  switch (shardIndex) {
    case 0:
      return env.APP_DB_0 ?? null;
    case 1:
      return env.APP_DB_1 ?? null;
    case 2:
      return env.APP_DB_2 ?? null;
    case 3:
      return env.APP_DB_3 ?? null;
    case 4:
      return env.APP_DB_4 ?? null;
    case 5:
      return env.APP_DB_5 ?? null;
    case 6:
      return env.APP_DB_6 ?? null;
    default:
      return null;
  }
}

function releaseShard(env: CatalogEnv, appId: string): number {
  const shards = [
    env.APP_DB_0,
    env.APP_DB_1,
    env.APP_DB_2,
    env.APP_DB_3,
    env.APP_DB_4,
    env.APP_DB_5,
    env.APP_DB_6,
  ];
  if (shards.every((shard) => shard === undefined)) {
    return -1;
  }
  return shards.every((shard) => shard !== undefined) ? shardForAppId(appId) : -2;
}

function releaseDatabase(env: CatalogEnv, shardIndex: number): D1Database | null {
  return shardIndex === -1 ? env.DB : getShard(env, shardIndex);
}

function toBytes(value: ChunkRow["content"]): Uint8Array {
  if (value instanceof ArrayBuffer) {
    return new Uint8Array(value);
  }
  if (ArrayBuffer.isView(value)) {
    return new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
  }
  return Uint8Array.from(value);
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) {
    return `[${value.map(canonicalJson).join(",")}]`;
  }
  if (value !== null && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

function decodeBase64(value: string): Uint8Array | null {
  if (!/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(value)) {
    return null;
  }
  try {
    const decoded = atob(value);
    return Uint8Array.from(decoded, (character) => character.charCodeAt(0));
  } catch {
    return null;
  }
}

async function verifyManifestSignature(
  manifest: ReleaseManifest,
  signature: string,
  publicKey: string,
): Promise<boolean> {
  const keyBytes = decodeBase64(publicKey);
  const signatureBytes = decodeBase64(signature);
  if (keyBytes?.byteLength !== 32 || signatureBytes?.byteLength !== 64) {
    return false;
  }

  try {
    const key = await crypto.subtle.importKey(
      "raw",
      keyBytes,
      { name: "Ed25519" },
      false,
      ["verify"],
    );
    return await crypto.subtle.verify(
      { name: "Ed25519" },
      key,
      signatureBytes,
      utf8.encode(canonicalJson(manifest)),
    );
  } catch (error) {
    console.error("Unable to verify the catalog release signature", error);
    return false;
  }
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isSafePackagePath(value: string): boolean {
  const parts = value.split("/");
  return (
    value.length > 0 &&
    value.toLocaleLowerCase("en-US") !== ".pyos-catalog.json" &&
    utf8.encode(value).byteLength <= 512 &&
    !value.includes("\\") &&
    !value.startsWith("/") &&
    !/[\u0000-\u001f\u007f]/.test(value) &&
    value.normalize("NFC") === value &&
    parts.every(
      (part) =>
        part.length > 0 &&
        part !== "." &&
        part !== ".." &&
        !/[<>:"|?*]/.test(part) &&
        !/[ .]$/.test(part) &&
        !WINDOWS_RESERVED_NAME.test(part),
    )
  );
}

function validateManifest(value: unknown): value is ReleaseManifest {
  if (!isPlainObject(value)) {
    return false;
  }
  const manifest = value as Partial<ReleaseManifest>;
  if (
    typeof manifest.app_id !== "string" ||
    !validateAppId(manifest.app_id) ||
    typeof manifest.version !== "string" ||
    !validateVersion(manifest.version) ||
    typeof manifest.name !== "string" ||
    manifest.name.trim().length === 0 ||
    manifest.name.length > 128 ||
    typeof manifest.description !== "string" ||
    manifest.description.length > 2_048 ||
    typeof manifest.min_pyos_version !== "string" ||
    !validateVersion(manifest.min_pyos_version) ||
    manifest.entry_point !== "__init__.py" ||
    !Array.isArray(manifest.files) ||
    manifest.files.length === 0 ||
    manifest.files.length > MAX_FILES ||
    !Number.isSafeInteger(manifest.archive_size) ||
    manifest.archive_size! <= 0 ||
    manifest.archive_size! > MAX_PACKAGE_SIZE ||
    !Number.isSafeInteger(manifest.expanded_size) ||
    manifest.expanded_size! < 0 ||
    manifest.expanded_size! > MAX_PACKAGE_SIZE ||
    manifest.archive_sha256 === undefined ||
    !DIGEST_PATTERN.test(manifest.archive_sha256) ||
    manifest.chunk_size !== CHUNK_SIZE ||
    !Array.isArray(manifest.chunk_hashes) ||
    manifest.chunk_hashes.length !== Math.ceil(manifest.archive_size! / CHUNK_SIZE) ||
    manifest.chunk_hashes.length > Math.ceil(MAX_PACKAGE_SIZE / CHUNK_SIZE)
  ) {
    return false;
  }

  let expandedSize = 0;
  const seenPaths = new Set<string>();
  let previousPath = "";
  for (const item of manifest.files) {
    if (
      !isPlainObject(item) ||
      typeof item.path !== "string" ||
      !isSafePackagePath(item.path) ||
      !Number.isSafeInteger(item.size) ||
      item.size < 0 ||
      typeof item.sha256 !== "string" ||
      !DIGEST_PATTERN.test(item.sha256) ||
      item.path <= previousPath
    ) {
      return false;
    }

    const pathKey = item.path.toLocaleLowerCase("en-US");
    if (seenPaths.has(pathKey)) {
      return false;
    }
    seenPaths.add(pathKey);
    previousPath = item.path;
    expandedSize += item.size;
    if (!Number.isSafeInteger(expandedSize) || expandedSize > MAX_PACKAGE_SIZE) {
      return false;
    }
  }

  return (
    expandedSize === manifest.expanded_size &&
    seenPaths.has(manifest.entry_point) &&
    manifest.chunk_hashes.every(
      (digest) => typeof digest === "string" && DIGEST_PATTERN.test(digest),
    ) &&
    utf8.encode(canonicalJson(manifest)).byteLength <= MAX_MANIFEST_SIZE
  );
}

async function readBodyLimited(request: Request, maxBytes: number): Promise<Uint8Array | null> {
  const contentLength = request.headers.get("content-length");
  if (contentLength !== null && (!/^\d+$/.test(contentLength) || Number(contentLength) > maxBytes)) {
    return null;
  }
  if (!request.body) {
    return new Uint8Array();
  }

  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }
    total += value.byteLength;
    if (total > maxBytes) {
      await reader.cancel();
      return null;
    }
    chunks.push(value);
  }

  const result = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    result.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return result;
}

async function authenticated(request: Request, env: CatalogEnv): Promise<boolean> {
  const authorization = request.headers.get("authorization") ?? "";
  const providedToken = authorization.startsWith("Bearer ") ? authorization.slice(7) : "";
  if (!providedToken || !env.PUBLISH_TOKEN) {
    return false;
  }

  const [providedHash, expectedHash] = await Promise.all([
    crypto.subtle.digest("SHA-256", utf8.encode(providedToken)),
    crypto.subtle.digest("SHA-256", utf8.encode(env.PUBLISH_TOKEN)),
  ]);
  const providedBytes = new Uint8Array(providedHash);
  const expectedBytes = new Uint8Array(expectedHash);
  let difference = 0;
  for (let index = 0; index < expectedBytes.length; index += 1) {
    difference |= providedBytes[index] ^ expectedBytes[index];
  }
  return difference === 0;
}

async function startRelease(request: Request, env: CatalogEnv): Promise<Response> {
  if (!(await authenticated(request, env))) {
    return json({ error: "unauthorized" }, 401);
  }
  if (!request.headers.get("content-type")?.toLowerCase().startsWith("application/json")) {
    return json({ error: "invalid_content_type" }, 415);
  }

  const bodyBytes = await readBodyLimited(request, MAX_MANIFEST_SIZE + 16_384);
  if (!bodyBytes) {
    return json({ error: "request_too_large" }, 413);
  }

  let body: unknown;
  try {
    body = JSON.parse(
      new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bodyBytes),
    );
  } catch {
    return json({ error: "invalid_json" }, 400);
  }
  if (!isPlainObject(body) || !validateManifest(body.manifest) || typeof body.signature !== "string") {
    return json({ error: "invalid_manifest" }, 400);
  }

  const manifest = body.manifest;
  if (
    !(await verifyManifestSignature(manifest, body.signature, env.PUBLISH_PUBLIC_KEY))
  ) {
    return json({ error: "invalid_signature" }, 400);
  }

  const storageShard = releaseShard(env, manifest.app_id);
  if (storageShard === -2) {
    return json({ error: "storage_shards_incompletely_configured" }, 503);
  }
  const manifestJson = canonicalJson(manifest);
  const existing = await env.DB.prepare(
    `SELECT status, manifest, signature, storage_shard
       FROM app_versions
      WHERE app_id = ? AND version = ?`,
  )
    .bind(manifest.app_id, manifest.version)
    .first<Pick<PendingVersionRow, "status" | "manifest" | "signature" | "storage_shard">>();
  if (existing) {
    if (existing.manifest !== manifestJson || existing.signature !== body.signature) {
      return json({ error: "version_conflict" }, 409);
    }
    return json(
      { status: existing.status, chunk_count: manifest.chunk_hashes.length },
      existing.status === "pending" ? 200 : 200,
    );
  }

  await env.DB.prepare(
    `INSERT INTO app_versions
       (app_id, version, status, manifest, signature, package_size, expanded_size,
        chunk_count, storage_shard, created_at, published_at)
     VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, NULL)
     ON CONFLICT (app_id, version) DO NOTHING`,
  )
    .bind(
      manifest.app_id,
      manifest.version,
      manifestJson,
      body.signature,
      manifest.archive_size,
      manifest.expanded_size,
      manifest.chunk_hashes.length,
      storageShard,
      new Date().toISOString(),
    )
    .run();

  const saved = await env.DB.prepare(
    `SELECT status, manifest, signature, storage_shard
       FROM app_versions
      WHERE app_id = ? AND version = ?`,
  )
    .bind(manifest.app_id, manifest.version)
    .first<Pick<PendingVersionRow, "status" | "manifest" | "signature" | "storage_shard">>();
  if (!saved || saved.manifest !== manifestJson || saved.signature !== body.signature) {
    return json({ error: "version_conflict" }, 409);
  }

  return json({ status: "pending", chunk_count: manifest.chunk_hashes.length }, 201);
}

async function putReleaseChunk(
  request: Request,
  env: CatalogEnv,
  appId: string,
  version: string,
  indexText: string,
): Promise<Response> {
  if (!(await authenticated(request, env))) {
    return json({ error: "unauthorized" }, 401);
  }
  if (!validateAppId(appId)) {
    return json({ error: "invalid_app_id" }, 400);
  }
  if (!validateVersion(version)) {
    return json({ error: "invalid_version" }, 400);
  }
  const chunkIndex = parseChunkIndex(indexText);
  if (chunkIndex === null) {
    return json({ error: "invalid_chunk_index" }, 400);
  }

  const release = await env.DB.prepare(
    `SELECT status, manifest, signature, package_size, expanded_size, chunk_count,
            storage_shard
       FROM app_versions WHERE app_id = ? AND version = ?`,
  )
    .bind(appId, version)
    .first<PendingVersionRow>();
  if (!release) {
    return json({ error: "not_found" }, 404);
  }
  if (release.status !== "pending") {
    return json({ error: "release_immutable" }, 409);
  }
  const shard = releaseDatabase(env, release.storage_shard);
  if (!shard) {
    return json({ error: "invalid_storage_shard" }, 503);
  }

  const manifest = JSON.parse(release.manifest) as ReleaseManifest;
  if (chunkIndex >= manifest.chunk_hashes.length) {
    return json({ error: "invalid_chunk_index" }, 400);
  }

  const chunk = await readBodyLimited(request, CHUNK_SIZE);
  if (!chunk) {
    return json({ error: "chunk_too_large" }, 413);
  }
  const expectedLength =
    chunkIndex === manifest.chunk_hashes.length - 1
      ? manifest.archive_size - chunkIndex * CHUNK_SIZE
      : CHUNK_SIZE;
  if (chunk.byteLength !== expectedLength) {
    return json({ error: "invalid_chunk_size" }, 400);
  }
  const digest = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", chunk)))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
  if (digest !== manifest.chunk_hashes[chunkIndex]) {
    return json({ error: "chunk_digest_mismatch" }, 400);
  }

  const existing = await shard.prepare(
    `SELECT sha256 FROM app_chunks
      WHERE app_id = ? AND version = ? AND chunk_index = ?`,
  )
    .bind(appId, version, chunkIndex)
    .first<{ sha256: string }>();
  if (existing) {
    return existing.sha256 === digest
      ? new Response(null, { status: 204 })
      : json({ error: "chunk_conflict" }, 409);
  }

  await shard.prepare(
    `INSERT INTO app_chunks (app_id, version, chunk_index, sha256, content)
     VALUES (?, ?, ?, ?, ?)`,
  )
    .bind(appId, version, chunkIndex, digest, chunk.buffer)
    .run();
  return new Response(null, { status: 204 });
}

function compareVersions(left: string, right: string): number {
  const parse = (version: string) => {
    const [withoutBuild, build] = version.split("+", 2);
    const [core, prerelease = ""] = withoutBuild.split("-", 2);
    const [major, minor, patch] = core.split(".").map(Number);
    return { major, minor, patch, prerelease: prerelease ? prerelease.split(".") : [], build };
  };
  const a = parse(left);
  const b = parse(right);
  for (const key of ["major", "minor", "patch"] as const) {
    if (a[key] !== b[key]) {
      return a[key] < b[key] ? -1 : 1;
    }
  }
  if (a.prerelease.length === 0 || b.prerelease.length === 0) {
    return a.prerelease.length === b.prerelease.length ? 0 : a.prerelease.length === 0 ? 1 : -1;
  }
  for (let index = 0; index < Math.max(a.prerelease.length, b.prerelease.length); index += 1) {
    const ai = a.prerelease[index];
    const bi = b.prerelease[index];
    if (ai === undefined || bi === undefined) {
      return ai === bi ? 0 : ai === undefined ? -1 : 1;
    }
    if (ai === bi) {
      continue;
    }
    const an = /^\d+$/.test(ai);
    const bn = /^\d+$/.test(bi);
    if (an && bn) {
      return ai.length === bi.length ? (ai < bi ? -1 : 1) : ai.length < bi.length ? -1 : 1;
    }
    if (an !== bn) {
      return an ? -1 : 1;
    }
    return ai < bi ? -1 : 1;
  }
  return 0;
}

async function publishRelease(
  request: Request,
  env: CatalogEnv,
  appId: string,
  version: string,
): Promise<Response> {
  if (!(await authenticated(request, env))) {
    return json({ error: "unauthorized" }, 401);
  }
  if (!validateAppId(appId)) {
    return json({ error: "invalid_app_id" }, 400);
  }
  if (!validateVersion(version)) {
    return json({ error: "invalid_version" }, 400);
  }

  const release = await env.DB.prepare(
    `SELECT status, manifest, signature, package_size, expanded_size, chunk_count,
            storage_shard
       FROM app_versions WHERE app_id = ? AND version = ?`,
  )
    .bind(appId, version)
    .first<PendingVersionRow>();
  if (!release) {
    return json({ error: "not_found" }, 404);
  }
  if (release.status === "published") {
    return json({ status: "published" });
  }

  const manifest = JSON.parse(release.manifest) as ReleaseManifest;
  if (!(await verifyManifestSignature(manifest, release.signature, env.PUBLISH_PUBLIC_KEY))) {
    return json({ error: "catalog_data_unavailable" }, 503);
  }

  const shard = releaseDatabase(env, release.storage_shard);
  if (!shard) {
    return json({ error: "invalid_storage_shard" }, 503);
  }
  const chunks = await shard.prepare(
    `SELECT COUNT(*) AS count, COALESCE(SUM(LENGTH(content)), 0) AS total_size
       FROM app_chunks WHERE app_id = ? AND version = ?`,
  )
    .bind(appId, version)
    .first<{ count: number; total_size: number }>();
  if (
    chunks?.count !== release.chunk_count ||
    chunks.total_size !== release.package_size
  ) {
    return json({ error: "release_incomplete" }, 409);
  }

  const now = new Date().toISOString();
  const current = await env.DB.prepare("SELECT version FROM apps WHERE id = ?")
    .bind(appId)
    .first<{ version: string }>();
  const updateCurrentApp = !current || compareVersions(version, current.version) > 0;
  const statements: D1PreparedStatement[] = [
    env.DB.prepare(
      `UPDATE app_versions
          SET status = 'published', published_at = ?
        WHERE app_id = ? AND version = ? AND status = 'pending'`,
    ).bind(now, appId, version),
  ];
  if (updateCurrentApp) {
    statements.push(
      env.DB.prepare(
        `INSERT INTO apps (id, name, description, version, min_pyos_version, published_at)
         VALUES (?, ?, ?, ?, ?, ?)
         ON CONFLICT (id) DO UPDATE SET
           name = excluded.name,
           description = excluded.description,
           version = excluded.version,
           min_pyos_version = excluded.min_pyos_version,
           published_at = excluded.published_at`,
      ).bind(
        appId,
        manifest.name,
        manifest.description,
        version,
        manifest.min_pyos_version,
        now,
      ),
    );
  }
  await env.DB.batch(statements);
  return json({ status: "published" });
}

async function listApps(env: CatalogEnv): Promise<Response> {
  const result = await env.DB.prepare(
    `SELECT a.id, a.name, a.description, a.version, a.min_pyos_version,
            v.package_size, 1048576 AS chunk_size, v.chunk_count
       FROM apps AS a
       JOIN app_versions AS v
         ON v.app_id = a.id
        AND v.version = a.version
        AND v.status = 'published'
      ORDER BY a.name COLLATE NOCASE, a.id
      LIMIT 100`,
  ).all<AppRow>();

  return json({ apps: result.results });
}

async function getApp(env: CatalogEnv, appId: string): Promise<Response> {
  if (!validateAppId(appId)) {
    return json({ error: "invalid_app_id" }, 400);
  }

  const app = await env.DB.prepare(
    `SELECT a.id, a.name, a.description, a.version, a.min_pyos_version,
            v.package_size, 1048576 AS chunk_size, v.chunk_count
       FROM apps AS a
       JOIN app_versions AS v
         ON v.app_id = a.id
        AND v.version = a.version
        AND v.status = 'published'
      WHERE a.id = ?`,
  )
    .bind(appId)
    .first<AppRow>();

  return app ? json(app) : json({ error: "not_found" }, 404);
}

async function getManifest(
  env: CatalogEnv,
  appId: string,
  version: string,
): Promise<Response> {
  if (!validateAppId(appId)) {
    return json({ error: "invalid_app_id" }, 400);
  }
  if (!validateVersion(version)) {
    return json({ error: "invalid_version" }, 400);
  }

  const row = await env.DB.prepare(
    `SELECT manifest, signature, storage_shard
       FROM app_versions
      WHERE app_id = ? AND version = ? AND status = 'published'`,
  )
    .bind(appId, version)
    .first<VersionRow>();

  if (!row) {
    return json({ error: "not_found" }, 404);
  }

  let manifest: unknown;
  try {
    manifest = JSON.parse(row.manifest);
  } catch (error) {
    console.error("Stored catalog manifest is invalid JSON", error);
    return json({ error: "catalog_data_unavailable" }, 503);
  }

  return json({ manifest, signature: row.signature });
}

async function getChunk(
  env: CatalogEnv,
  appId: string,
  version: string,
  chunkIndexText: string,
): Promise<Response> {
  if (!validateAppId(appId)) {
    return json({ error: "invalid_app_id" }, 400);
  }
  if (!validateVersion(version)) {
    return json({ error: "invalid_version" }, 400);
  }

  const chunkIndex = parseChunkIndex(chunkIndexText);
  if (chunkIndex === null) {
    return json({ error: "invalid_chunk_index" }, 400);
  }

  const versionRow = await env.DB.prepare(
    `SELECT storage_shard
       FROM app_versions
      WHERE app_id = ? AND version = ? AND status = 'published'`,
  )
    .bind(appId, version)
    .first<{ storage_shard: number }>();

  if (!versionRow) {
    return json({ error: "not_found" }, 404);
  }

  const database = releaseDatabase(env, versionRow.storage_shard);
  if (!database) {
    return json({ error: "catalog_data_unavailable" }, 503);
  }
  const row = await database.prepare(
    `SELECT content
       FROM app_chunks
      WHERE app_id = ? AND version = ? AND chunk_index = ?`,
  )
    .bind(appId, version, chunkIndex)
    .first<ChunkRow>();

  if (!row) {
    return json({ error: "not_found" }, 404);
  }

  return new Response(toBytes(row.content), {
    headers: {
      "content-type": "application/octet-stream",
      "cache-control": "public, max-age=31536000, immutable",
      "x-content-type-options": "nosniff",
    },
  });
}

async function migrateLegacyChunk(
  request: Request,
  env: CatalogEnv,
  appId: string,
  version: string,
  indexText: string,
): Promise<Response> {
  if (!(await authenticated(request, env))) {
    return json({ error: "unauthorized" }, 401);
  }
  if (!validateAppId(appId)) {
    return json({ error: "invalid_app_id" }, 400);
  }
  if (!validateVersion(version)) {
    return json({ error: "invalid_version" }, 400);
  }
  const chunkIndex = parseChunkIndex(indexText);
  if (chunkIndex === null) {
    return json({ error: "invalid_chunk_index" }, 400);
  }

  const release = await env.DB.prepare(
    `SELECT status, manifest, signature, storage_shard
       FROM app_versions
      WHERE app_id = ? AND version = ?`,
  )
    .bind(appId, version)
    .first<Pick<VersionRow, "manifest" | "signature" | "storage_shard"> & { status: string }>();
  if (!release || release.status !== "published") {
    return json({ error: "not_found" }, 404);
  }

  const shardIndex = shardForAppId(appId);
  const shard = getShard(env, shardIndex);
  if (!shard) {
    return json({ error: "invalid_storage_shard" }, 503);
  }
  let manifest: ReleaseManifest;
  try {
    manifest = JSON.parse(release.manifest) as ReleaseManifest;
  } catch (error) {
    console.error("Stored catalog manifest is invalid JSON", error);
    return json({ error: "catalog_data_unavailable" }, 503);
  }
  if (
    !(await verifyManifestSignature(manifest, release.signature, env.PUBLISH_PUBLIC_KEY)) ||
    manifest.app_id !== appId ||
    manifest.version !== version
  ) {
    return json({ error: "catalog_data_unavailable" }, 503);
  }
  if (chunkIndex >= manifest.chunk_hashes.length) {
    return json({ error: "invalid_chunk_index" }, 400);
  }

  const source = await env.DB.prepare(
    `SELECT sha256, content FROM app_chunks
      WHERE app_id = ? AND version = ? AND chunk_index = ?`,
  )
    .bind(appId, version, chunkIndex)
    .first<{ sha256: string; content: ChunkRow["content"] }>();
  const expectedLength =
    chunkIndex === manifest.chunk_hashes.length - 1
      ? manifest.archive_size - chunkIndex * CHUNK_SIZE
      : CHUNK_SIZE;
  const existing = await shard.prepare(
    `SELECT sha256, content FROM app_chunks
      WHERE app_id = ? AND version = ? AND chunk_index = ?`,
  )
    .bind(appId, version, chunkIndex)
    .first<{ sha256: string; content: ChunkRow["content"] }>();
  if (existing) {
    const existingBytes = toBytes(existing.content);
    const existingDigest = Array.from(
      new Uint8Array(await crypto.subtle.digest("SHA-256", existingBytes)),
    )
      .map((byte) => byte.toString(16).padStart(2, "0"))
      .join("");
    return existing.sha256 === manifest.chunk_hashes[chunkIndex] &&
      existingDigest === manifest.chunk_hashes[chunkIndex] &&
      existingBytes.byteLength === expectedLength
      ? new Response(null, { status: 204 })
      : json({ error: "chunk_conflict" }, 409);
  }
  if (!source || release.storage_shard !== -1) {
    return json({ error: "not_found" }, 404);
  }

  const bytes = toBytes(source.content);
  const digest = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
  if (
    bytes.byteLength !== expectedLength ||
    source.sha256 !== manifest.chunk_hashes[chunkIndex] ||
    digest !== manifest.chunk_hashes[chunkIndex]
  ) {
    return json({ error: "legacy_chunk_invalid" }, 503);
  }

  await shard.prepare(
    `INSERT INTO app_chunks (app_id, version, chunk_index, sha256, content)
     VALUES (?, ?, ?, ?, ?)`,
  )
    .bind(appId, version, chunkIndex, digest, bytes.buffer)
    .run();
  return new Response(null, { status: 204 });
}

async function finalizeLegacyMigration(
  request: Request,
  env: CatalogEnv,
  appId: string,
  version: string,
): Promise<Response> {
  if (!(await authenticated(request, env))) {
    return json({ error: "unauthorized" }, 401);
  }
  if (!validateAppId(appId)) {
    return json({ error: "invalid_app_id" }, 400);
  }
  if (!validateVersion(version)) {
    return json({ error: "invalid_version" }, 400);
  }

  const release = await env.DB.prepare(
    `SELECT status, manifest, signature, storage_shard
       FROM app_versions
      WHERE app_id = ? AND version = ?`,
  )
    .bind(appId, version)
    .first<Pick<VersionRow, "manifest" | "signature" | "storage_shard"> & { status: string }>();
  if (!release || release.status !== "published") {
    return json({ error: "not_found" }, 404);
  }

  const shardIndex = shardForAppId(appId);
  const shard = getShard(env, shardIndex);
  if (!shard) {
    return json({ error: "invalid_storage_shard" }, 503);
  }
  let manifest: ReleaseManifest;
  try {
    manifest = JSON.parse(release.manifest) as ReleaseManifest;
  } catch (error) {
    console.error("Stored catalog manifest is invalid JSON", error);
    return json({ error: "catalog_data_unavailable" }, 503);
  }
  if (
    !(await verifyManifestSignature(manifest, release.signature, env.PUBLISH_PUBLIC_KEY)) ||
    manifest.app_id !== appId ||
    manifest.version !== version
  ) {
    return json({ error: "catalog_data_unavailable" }, 503);
  }

  const targetChunks = await shard.prepare(
    `SELECT chunk_index, sha256 FROM app_chunks
      WHERE app_id = ? AND version = ?`,
  )
    .bind(appId, version)
    .all<{ chunk_index: number; sha256: string }>();
  const validIndexes = new Set<number>();
  for (const chunk of targetChunks.results) {
    if (
      chunk.chunk_index >= 0 &&
      chunk.chunk_index < manifest.chunk_hashes.length &&
      chunk.sha256 === manifest.chunk_hashes[chunk.chunk_index]
    ) {
      validIndexes.add(chunk.chunk_index);
    }
  }
  if (validIndexes.size !== manifest.chunk_hashes.length) {
    return json(
      {
        error: "migration_incomplete",
        missing_chunks: manifest.chunk_hashes.length - validIndexes.size,
      },
      409,
    );
  }

  if (release.storage_shard === -1) {
    await env.DB.prepare(
      `UPDATE app_versions SET storage_shard = ?
        WHERE app_id = ? AND version = ? AND storage_shard = -1`,
    )
      .bind(shardIndex, appId, version)
      .run();
  } else if (release.storage_shard !== shardIndex) {
    return json({ error: "storage_shard_conflict" }, 409);
  }
  return json({ status: "migrated", storage_shard: shardIndex });
}

async function listLegacyMigrations(request: Request, env: CatalogEnv): Promise<Response> {
  if (!(await authenticated(request, env))) {
    return json({ error: "unauthorized" }, 401);
  }
  const versions = await env.DB.prepare(
    `SELECT v.app_id, v.version, v.chunk_count, v.storage_shard,
            (SELECT COUNT(*) FROM app_chunks AS c
              WHERE c.app_id = v.app_id AND c.version = v.version) AS legacy_chunk_count
       FROM app_versions AS v
      WHERE v.status = 'published'
        AND (v.storage_shard = -1 OR EXISTS (
          SELECT 1 FROM app_chunks AS c
           WHERE c.app_id = v.app_id AND c.version = v.version
        ))
      ORDER BY v.app_id, v.version`,
  ).all<{
    app_id: string;
    version: string;
    chunk_count: number;
    storage_shard: number;
    legacy_chunk_count: number;
  }>();
  return json({ versions: versions.results });
}

async function cleanupLegacyMigration(
  request: Request,
  env: CatalogEnv,
  appId: string,
  version: string,
): Promise<Response> {
  if (!(await authenticated(request, env))) {
    return json({ error: "unauthorized" }, 401);
  }
  if (!validateAppId(appId)) {
    return json({ error: "invalid_app_id" }, 400);
  }
  if (!validateVersion(version)) {
    return json({ error: "invalid_version" }, 400);
  }

  const release = await env.DB.prepare(
    `SELECT status, manifest, signature, storage_shard
       FROM app_versions
      WHERE app_id = ? AND version = ?`,
  )
    .bind(appId, version)
    .first<Pick<VersionRow, "manifest" | "signature" | "storage_shard"> & { status: string }>();
  if (!release || release.status !== "published") {
    return json({ error: "not_found" }, 404);
  }
  if (release.storage_shard === -1) {
    return json({ error: "migration_not_finalized" }, 409);
  }
  const shard = getShard(env, release.storage_shard);
  if (!shard) {
    return json({ error: "invalid_storage_shard" }, 503);
  }

  let manifest: ReleaseManifest;
  try {
    manifest = JSON.parse(release.manifest) as ReleaseManifest;
  } catch (error) {
    console.error("Stored catalog manifest is invalid JSON", error);
    return json({ error: "catalog_data_unavailable" }, 503);
  }
  if (
    !(await verifyManifestSignature(manifest, release.signature, env.PUBLISH_PUBLIC_KEY)) ||
    manifest.app_id !== appId ||
    manifest.version !== version ||
    shardForAppId(appId) !== release.storage_shard
  ) {
    return json({ error: "catalog_data_unavailable" }, 503);
  }

  const result = await shard.prepare(
    `SELECT chunk_index, sha256, LENGTH(content) AS size
       FROM app_chunks WHERE app_id = ? AND version = ?`,
  )
    .bind(appId, version)
    .all<{ chunk_index: number; sha256: string; size: number }>();
  const validIndexes = new Set<number>();
  for (const chunk of result.results) {
    const expectedLength =
      chunk.chunk_index === manifest.chunk_hashes.length - 1
        ? manifest.archive_size - chunk.chunk_index * CHUNK_SIZE
        : CHUNK_SIZE;
    if (
      chunk.chunk_index >= 0 &&
      chunk.chunk_index < manifest.chunk_hashes.length &&
      chunk.sha256 === manifest.chunk_hashes[chunk.chunk_index] &&
      chunk.size === expectedLength
    ) {
      validIndexes.add(chunk.chunk_index);
    }
  }
  if (
    validIndexes.size !== manifest.chunk_hashes.length ||
    result.results.length !== manifest.chunk_hashes.length
  ) {
    return json({ error: "package_shard_incomplete" }, 409);
  }

  const deleted = await env.DB.prepare(
    `DELETE FROM app_chunks WHERE app_id = ? AND version = ?`,
  )
    .bind(appId, version)
    .run();
  return json({ status: "legacy_chunks_removed", deleted: deleted.meta.changes });
}

export async function handleCatalogRequest(
  request: Request,
  env: CatalogEnv,
): Promise<Response> {
  const url = new URL(request.url);
  const parts = url.pathname.split("/").filter(Boolean);

  if (request.method === "GET" && url.pathname === "/v1/health") {
    return json({ status: "ok" });
  }

  if (request.method === "GET" && url.pathname === "/v1/apps") {
    return listApps(env);
  }

  if (request.method === "POST" && url.pathname === "/v1/admin/releases") {
    return startRelease(request, env);
  }

  if (request.method === "GET" && url.pathname === "/v1/admin/storage-migrations") {
    return listLegacyMigrations(request, env);
  }

  if (
    request.method === "PUT" &&
    parts.length === 7 &&
    parts[0] === "v1" &&
    parts[1] === "admin" &&
    parts[2] === "releases" &&
    parts[5] === "chunks"
  ) {
    return putReleaseChunk(request, env, parts[3], parts[4], parts[6]);
  }

  if (
    request.method === "POST" &&
    parts.length === 6 &&
    parts[0] === "v1" &&
    parts[1] === "admin" &&
    parts[2] === "releases" &&
    parts[5] === "publish"
  ) {
    return publishRelease(request, env, parts[3], parts[4]);
  }

  if (
    request.method === "PUT" &&
    parts.length === 7 &&
    parts[0] === "v1" &&
    parts[1] === "admin" &&
    parts[2] === "storage-migrations" &&
    parts[5] === "chunks"
  ) {
    return migrateLegacyChunk(request, env, parts[3], parts[4], parts[6]);
  }

  if (
    request.method === "POST" &&
    parts.length === 6 &&
    parts[0] === "v1" &&
    parts[1] === "admin" &&
    parts[2] === "storage-migrations" &&
    parts[5] === "finalize"
  ) {
    return finalizeLegacyMigration(request, env, parts[3], parts[4]);
  }

  if (
    request.method === "POST" &&
    parts.length === 6 &&
    parts[0] === "v1" &&
    parts[1] === "admin" &&
    parts[2] === "storage-migrations" &&
    parts[5] === "cleanup"
  ) {
    return cleanupLegacyMigration(request, env, parts[3], parts[4]);
  }

  if (request.method === "GET" && parts.length === 3 && parts[0] === "v1" && parts[1] === "apps") {
    return getApp(env, parts[2]);
  }

  if (
    request.method === "GET" &&
    parts.length === 5 &&
    parts[0] === "v1" &&
    parts[1] === "apps" &&
    parts[4] === "manifest"
  ) {
    return getManifest(env, parts[2], parts[3]);
  }

  if (
    request.method === "GET" &&
    parts.length === 6 &&
    parts[0] === "v1" &&
    parts[1] === "apps" &&
    parts[4] === "chunks"
  ) {
    return getChunk(env, parts[2], parts[3], parts[5]);
  }

  return json({ error: "not_found" }, 404);
}
