var __defProp = Object.defineProperty;
var __name = (target, value) => __defProp(target, "name", { value, configurable: true });

// src/catalog.ts
var JSON_HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "no-store"
};
var APP_ID_PATTERN = /^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/;
var VERSION_PATTERN = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/;
var DIGEST_PATTERN = /^[a-f0-9]{64}$/;
var MAX_PACKAGE_SIZE = 100 * 1024 * 1024;
var CHUNK_SIZE = 1024 * 1024;
var MAX_MANIFEST_SIZE = 1024 * 1024;
var MAX_FILES = 5e3;
var WINDOWS_RESERVED_NAME = /^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])[ .]*(?:\.|$)/i;
var utf8 = new TextEncoder();
function json(data, status = 200) {
  return Response.json(data, { status, headers: JSON_HEADERS });
}
__name(json, "json");
function validateAppId(value) {
  return APP_ID_PATTERN.test(value);
}
__name(validateAppId, "validateAppId");
function validateVersion(value) {
  return VERSION_PATTERN.test(value);
}
__name(validateVersion, "validateVersion");
function parseChunkIndex(value) {
  if (!/^(0|[1-9]\d*)$/.test(value)) {
    return null;
  }
  const index = Number(value);
  return Number.isSafeInteger(index) ? index : null;
}
__name(parseChunkIndex, "parseChunkIndex");
function toBytes(value) {
  if (value instanceof ArrayBuffer) {
    return new Uint8Array(value);
  }
  if (ArrayBuffer.isView(value)) {
    return new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
  }
  return Uint8Array.from(value);
}
__name(toBytes, "toBytes");
function canonicalJson(value) {
  if (Array.isArray(value)) {
    return `[${value.map(canonicalJson).join(",")}]`;
  }
  if (value !== null && typeof value === "object") {
    const record = value;
    return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}
__name(canonicalJson, "canonicalJson");
function decodeBase64(value) {
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
__name(decodeBase64, "decodeBase64");
async function verifyManifestSignature(manifest, signature, publicKey) {
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
      ["verify"]
    );
    return await crypto.subtle.verify(
      { name: "Ed25519" },
      key,
      signatureBytes,
      utf8.encode(canonicalJson(manifest))
    );
  } catch (error) {
    console.error("Unable to verify the catalog release signature", error);
    return false;
  }
}
__name(verifyManifestSignature, "verifyManifestSignature");
function isPlainObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
__name(isPlainObject, "isPlainObject");
function isSafePackagePath(value) {
  const parts = value.split("/");
  return value.length > 0 && value.toLocaleLowerCase("en-US") !== ".pyos-catalog.json" && utf8.encode(value).byteLength <= 512 && !value.includes("\\") && !value.startsWith("/") && !/[\u0000-\u001f\u007f]/.test(value) && value.normalize("NFC") === value && parts.every(
    (part) => part.length > 0 && part !== "." && part !== ".." && !/[<>:"|?*]/.test(part) && !/[ .]$/.test(part) && !WINDOWS_RESERVED_NAME.test(part)
  );
}
__name(isSafePackagePath, "isSafePackagePath");
function validateManifest(value) {
  if (!isPlainObject(value)) {
    return false;
  }
  const manifest = value;
  if (typeof manifest.app_id !== "string" || !validateAppId(manifest.app_id) || typeof manifest.version !== "string" || !validateVersion(manifest.version) || typeof manifest.name !== "string" || manifest.name.trim().length === 0 || manifest.name.length > 128 || typeof manifest.description !== "string" || manifest.description.length > 2048 || typeof manifest.min_pyos_version !== "string" || !validateVersion(manifest.min_pyos_version) || manifest.entry_point !== "__init__.py" || !Array.isArray(manifest.files) || manifest.files.length === 0 || manifest.files.length > MAX_FILES || !Number.isSafeInteger(manifest.archive_size) || manifest.archive_size <= 0 || manifest.archive_size > MAX_PACKAGE_SIZE || !Number.isSafeInteger(manifest.expanded_size) || manifest.expanded_size < 0 || manifest.expanded_size > MAX_PACKAGE_SIZE || manifest.archive_sha256 === void 0 || !DIGEST_PATTERN.test(manifest.archive_sha256) || manifest.chunk_size !== CHUNK_SIZE || !Array.isArray(manifest.chunk_hashes) || manifest.chunk_hashes.length !== Math.ceil(manifest.archive_size / CHUNK_SIZE) || manifest.chunk_hashes.length > Math.ceil(MAX_PACKAGE_SIZE / CHUNK_SIZE)) {
    return false;
  }
  let expandedSize = 0;
  const seenPaths = /* @__PURE__ */ new Set();
  let previousPath = "";
  for (const item of manifest.files) {
    if (!isPlainObject(item) || typeof item.path !== "string" || !isSafePackagePath(item.path) || !Number.isSafeInteger(item.size) || item.size < 0 || typeof item.sha256 !== "string" || !DIGEST_PATTERN.test(item.sha256) || item.path <= previousPath) {
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
  return expandedSize === manifest.expanded_size && seenPaths.has(manifest.entry_point) && manifest.chunk_hashes.every(
    (digest) => typeof digest === "string" && DIGEST_PATTERN.test(digest)
  ) && utf8.encode(canonicalJson(manifest)).byteLength <= MAX_MANIFEST_SIZE;
}
__name(validateManifest, "validateManifest");
async function readBodyLimited(request, maxBytes) {
  const contentLength = request.headers.get("content-length");
  if (contentLength !== null && (!/^\d+$/.test(contentLength) || Number(contentLength) > maxBytes)) {
    return null;
  }
  if (!request.body) {
    return new Uint8Array();
  }
  const reader = request.body.getReader();
  const chunks = [];
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
__name(readBodyLimited, "readBodyLimited");
async function authenticated(request, env) {
  const authorization = request.headers.get("authorization") ?? "";
  const providedToken = authorization.startsWith("Bearer ") ? authorization.slice(7) : "";
  if (!providedToken || !env.PUBLISH_TOKEN) {
    return false;
  }
  const [providedHash, expectedHash] = await Promise.all([
    crypto.subtle.digest("SHA-256", utf8.encode(providedToken)),
    crypto.subtle.digest("SHA-256", utf8.encode(env.PUBLISH_TOKEN))
  ]);
  const providedBytes = new Uint8Array(providedHash);
  const expectedBytes = new Uint8Array(expectedHash);
  let difference = 0;
  for (let index = 0; index < expectedBytes.length; index += 1) {
    difference |= providedBytes[index] ^ expectedBytes[index];
  }
  return difference === 0;
}
__name(authenticated, "authenticated");
async function startRelease(request, env) {
  if (!await authenticated(request, env)) {
    return json({ error: "unauthorized" }, 401);
  }
  if (!request.headers.get("content-type")?.toLowerCase().startsWith("application/json")) {
    return json({ error: "invalid_content_type" }, 415);
  }
  const bodyBytes = await readBodyLimited(request, MAX_MANIFEST_SIZE + 16384);
  if (!bodyBytes) {
    return json({ error: "request_too_large" }, 413);
  }
  let body;
  try {
    body = JSON.parse(
      new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bodyBytes)
    );
  } catch {
    return json({ error: "invalid_json" }, 400);
  }
  if (!isPlainObject(body) || !validateManifest(body.manifest) || typeof body.signature !== "string") {
    return json({ error: "invalid_manifest" }, 400);
  }
  const manifest = body.manifest;
  if (!await verifyManifestSignature(manifest, body.signature, env.PUBLISH_PUBLIC_KEY)) {
    return json({ error: "invalid_signature" }, 400);
  }
  const manifestJson = canonicalJson(manifest);
  const existing = await env.DB.prepare(
    `SELECT status, manifest, signature
       FROM app_versions
      WHERE app_id = ? AND version = ?`
  ).bind(manifest.app_id, manifest.version).first();
  if (existing) {
    if (existing.manifest !== manifestJson || existing.signature !== body.signature) {
      return json({ error: "version_conflict" }, 409);
    }
    return json(
      { status: existing.status, chunk_count: manifest.chunk_hashes.length },
      existing.status === "pending" ? 200 : 200
    );
  }
  await env.DB.prepare(
    `INSERT INTO app_versions
       (app_id, version, status, manifest, signature, package_size, expanded_size,
        chunk_count, created_at, published_at)
     VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, NULL)
     ON CONFLICT (app_id, version) DO NOTHING`
  ).bind(
    manifest.app_id,
    manifest.version,
    manifestJson,
    body.signature,
    manifest.archive_size,
    manifest.expanded_size,
    manifest.chunk_hashes.length,
    (/* @__PURE__ */ new Date()).toISOString()
  ).run();
  const saved = await env.DB.prepare(
    `SELECT status, manifest, signature
       FROM app_versions
      WHERE app_id = ? AND version = ?`
  ).bind(manifest.app_id, manifest.version).first();
  if (!saved || saved.manifest !== manifestJson || saved.signature !== body.signature) {
    return json({ error: "version_conflict" }, 409);
  }
  return json({ status: "pending", chunk_count: manifest.chunk_hashes.length }, 201);
}
__name(startRelease, "startRelease");
async function putReleaseChunk(request, env, appId, version, indexText) {
  if (!await authenticated(request, env)) {
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
    `SELECT status, manifest, signature, package_size, expanded_size, chunk_count
       FROM app_versions WHERE app_id = ? AND version = ?`
  ).bind(appId, version).first();
  if (!release) {
    return json({ error: "not_found" }, 404);
  }
  if (release.status !== "pending") {
    return json({ error: "release_immutable" }, 409);
  }
  const manifest = JSON.parse(release.manifest);
  if (chunkIndex >= manifest.chunk_hashes.length) {
    return json({ error: "invalid_chunk_index" }, 400);
  }
  const chunk = await readBodyLimited(request, CHUNK_SIZE);
  if (!chunk) {
    return json({ error: "chunk_too_large" }, 413);
  }
  const expectedLength = chunkIndex === manifest.chunk_hashes.length - 1 ? manifest.archive_size - chunkIndex * CHUNK_SIZE : CHUNK_SIZE;
  if (chunk.byteLength !== expectedLength) {
    return json({ error: "invalid_chunk_size" }, 400);
  }
  const digest = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", chunk))).map((byte) => byte.toString(16).padStart(2, "0")).join("");
  if (digest !== manifest.chunk_hashes[chunkIndex]) {
    return json({ error: "chunk_digest_mismatch" }, 400);
  }
  const existing = await env.DB.prepare(
    `SELECT sha256 FROM app_chunks
      WHERE app_id = ? AND version = ? AND chunk_index = ?`
  ).bind(appId, version, chunkIndex).first();
  if (existing) {
    return existing.sha256 === digest ? new Response(null, { status: 204 }) : json({ error: "chunk_conflict" }, 409);
  }
  await env.DB.prepare(
    `INSERT INTO app_chunks (app_id, version, chunk_index, sha256, content)
     VALUES (?, ?, ?, ?, ?)`
  ).bind(appId, version, chunkIndex, digest, chunk.buffer).run();
  return new Response(null, { status: 204 });
}
__name(putReleaseChunk, "putReleaseChunk");
function compareVersions(left, right) {
  const parse = /* @__PURE__ */ __name((version) => {
    const [withoutBuild, build] = version.split("+", 2);
    const [core, prerelease = ""] = withoutBuild.split("-", 2);
    const [major, minor, patch] = core.split(".").map(Number);
    return { major, minor, patch, prerelease: prerelease ? prerelease.split(".") : [], build };
  }, "parse");
  const a = parse(left);
  const b = parse(right);
  for (const key of ["major", "minor", "patch"]) {
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
    if (ai === void 0 || bi === void 0) {
      return ai === bi ? 0 : ai === void 0 ? -1 : 1;
    }
    if (ai === bi) {
      continue;
    }
    const an = /^\d+$/.test(ai);
    const bn = /^\d+$/.test(bi);
    if (an && bn) {
      return ai.length === bi.length ? ai < bi ? -1 : 1 : ai.length < bi.length ? -1 : 1;
    }
    if (an !== bn) {
      return an ? -1 : 1;
    }
    return ai < bi ? -1 : 1;
  }
  return 0;
}
__name(compareVersions, "compareVersions");
async function publishRelease(request, env, appId, version) {
  if (!await authenticated(request, env)) {
    return json({ error: "unauthorized" }, 401);
  }
  if (!validateAppId(appId)) {
    return json({ error: "invalid_app_id" }, 400);
  }
  if (!validateVersion(version)) {
    return json({ error: "invalid_version" }, 400);
  }
  const release = await env.DB.prepare(
    `SELECT status, manifest, signature, package_size, expanded_size, chunk_count
       FROM app_versions WHERE app_id = ? AND version = ?`
  ).bind(appId, version).first();
  if (!release) {
    return json({ error: "not_found" }, 404);
  }
  if (release.status === "published") {
    return json({ status: "published" });
  }
  const manifest = JSON.parse(release.manifest);
  if (!await verifyManifestSignature(manifest, release.signature, env.PUBLISH_PUBLIC_KEY)) {
    return json({ error: "catalog_data_unavailable" }, 503);
  }
  const chunks = await env.DB.prepare(
    `SELECT COUNT(*) AS count, COALESCE(SUM(LENGTH(content)), 0) AS total_size
       FROM app_chunks WHERE app_id = ? AND version = ?`
  ).bind(appId, version).first();
  if (chunks?.count !== release.chunk_count || chunks.total_size !== release.package_size) {
    return json({ error: "release_incomplete" }, 409);
  }
  const now = (/* @__PURE__ */ new Date()).toISOString();
  const current = await env.DB.prepare("SELECT version FROM apps WHERE id = ?").bind(appId).first();
  const updateCurrentApp = !current || compareVersions(version, current.version) > 0;
  const statements = [
    env.DB.prepare(
      `UPDATE app_versions
          SET status = 'published', published_at = ?
        WHERE app_id = ? AND version = ? AND status = 'pending'`
    ).bind(now, appId, version)
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
           published_at = excluded.published_at`
      ).bind(
        appId,
        manifest.name,
        manifest.description,
        version,
        manifest.min_pyos_version,
        now
      )
    );
  }
  await env.DB.batch(statements);
  return json({ status: "published" });
}
__name(publishRelease, "publishRelease");
async function listApps(env) {
  const result = await env.DB.prepare(
    `SELECT a.id, a.name, a.description, a.version, a.min_pyos_version,
            v.package_size, 1048576 AS chunk_size, v.chunk_count
       FROM apps AS a
       JOIN app_versions AS v
         ON v.app_id = a.id
        AND v.version = a.version
        AND v.status = 'published'
      ORDER BY a.name COLLATE NOCASE, a.id
      LIMIT 100`
  ).all();
  return json({ apps: result.results });
}
__name(listApps, "listApps");
async function getApp(env, appId) {
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
      WHERE a.id = ?`
  ).bind(appId).first();
  return app ? json(app) : json({ error: "not_found" }, 404);
}
__name(getApp, "getApp");
async function getManifest(env, appId, version) {
  if (!validateAppId(appId)) {
    return json({ error: "invalid_app_id" }, 400);
  }
  if (!validateVersion(version)) {
    return json({ error: "invalid_version" }, 400);
  }
  const row = await env.DB.prepare(
    `SELECT manifest, signature
       FROM app_versions
      WHERE app_id = ? AND version = ? AND status = 'published'`
  ).bind(appId, version).first();
  if (!row) {
    return json({ error: "not_found" }, 404);
  }
  let manifest;
  try {
    manifest = JSON.parse(row.manifest);
  } catch (error) {
    console.error("Stored catalog manifest is invalid JSON", error);
    return json({ error: "catalog_data_unavailable" }, 503);
  }
  return json({ manifest, signature: row.signature });
}
__name(getManifest, "getManifest");
async function getChunk(env, appId, version, chunkIndexText) {
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
  const row = await env.DB.prepare(
    `SELECT c.content
       FROM app_chunks AS c
       JOIN app_versions AS v
         ON v.app_id = c.app_id AND v.version = c.version
      WHERE c.app_id = ?
        AND c.version = ?
        AND c.chunk_index = ?
        AND v.status = 'published'`
  ).bind(appId, version, chunkIndex).first();
  if (!row) {
    return json({ error: "not_found" }, 404);
  }
  return new Response(toBytes(row.content), {
    headers: {
      "content-type": "application/octet-stream",
      "cache-control": "public, max-age=31536000, immutable",
      "x-content-type-options": "nosniff"
    }
  });
}
__name(getChunk, "getChunk");
async function handleCatalogRequest(request, env) {
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
  if (request.method === "PUT" && parts.length === 7 && parts[0] === "v1" && parts[1] === "admin" && parts[2] === "releases" && parts[5] === "chunks") {
    return putReleaseChunk(request, env, parts[3], parts[4], parts[6]);
  }
  if (request.method === "POST" && parts.length === 6 && parts[0] === "v1" && parts[1] === "admin" && parts[2] === "releases" && parts[5] === "publish") {
    return publishRelease(request, env, parts[3], parts[4]);
  }
  if (request.method === "GET" && parts.length === 3 && parts[0] === "v1" && parts[1] === "apps") {
    return getApp(env, parts[2]);
  }
  if (request.method === "GET" && parts.length === 5 && parts[0] === "v1" && parts[1] === "apps" && parts[4] === "manifest") {
    return getManifest(env, parts[2], parts[3]);
  }
  if (request.method === "GET" && parts.length === 6 && parts[0] === "v1" && parts[1] === "apps" && parts[4] === "chunks") {
    return getChunk(env, parts[2], parts[3], parts[5]);
  }
  return json({ error: "not_found" }, 404);
}
__name(handleCatalogRequest, "handleCatalogRequest");

// src/index.ts
var src_default = {
  fetch(request, env) {
    return handleCatalogRequest(request, env);
  }
};

// node_modules/wrangler/templates/middleware/middleware-ensure-req-body-drained.ts
var drainBody = /* @__PURE__ */ __name(async (request, env, _ctx, middlewareCtx) => {
  try {
    return await middlewareCtx.next(request, env);
  } finally {
    try {
      if (request.body !== null && !request.bodyUsed) {
        const reader = request.body.getReader();
        while (!(await reader.read()).done) {
        }
      }
    } catch (e) {
      console.error("Failed to drain the unused request body.", e);
    }
  }
}, "drainBody");
var middleware_ensure_req_body_drained_default = drainBody;

// node_modules/wrangler/templates/middleware/middleware-miniflare3-json-error.ts
function reduceError(e) {
  return {
    name: e?.name,
    message: e?.message ?? String(e),
    stack: e?.stack,
    cause: e?.cause === void 0 ? void 0 : reduceError(e.cause)
  };
}
__name(reduceError, "reduceError");
var jsonError = /* @__PURE__ */ __name(async (request, env, _ctx, middlewareCtx) => {
  try {
    return await middlewareCtx.next(request, env);
  } catch (e) {
    const error = reduceError(e);
    const body = JSON.stringify(error);
    const headers = {
      "Content-Type": "application/json",
      "MF-Experimental-Error-Stack": "true"
    };
    const encoded = encodeURIComponent(body);
    if (encoded.length <= 8192) {
      headers["MF-Experimental-Error-Stack-Payload"] = encoded;
    }
    return new Response(body, { status: 500, headers });
  }
}, "jsonError");
var middleware_miniflare3_json_error_default = jsonError;

// .wrangler/tmp/bundle-E3jzs7/middleware-insertion-facade.js
var __INTERNAL_WRANGLER_MIDDLEWARE__ = [
  middleware_ensure_req_body_drained_default,
  middleware_miniflare3_json_error_default
];
var middleware_insertion_facade_default = src_default;

// node_modules/wrangler/templates/middleware/common.ts
var __facade_middleware__ = [];
function __facade_register__(...args) {
  __facade_middleware__.push(...args.flat());
}
__name(__facade_register__, "__facade_register__");
function __facade_invokeChain__(request, env, ctx, dispatch, middlewareChain) {
  const [head, ...tail] = middlewareChain;
  const middlewareCtx = {
    dispatch,
    next(newRequest, newEnv) {
      return __facade_invokeChain__(newRequest, newEnv, ctx, dispatch, tail);
    }
  };
  return head(request, env, ctx, middlewareCtx);
}
__name(__facade_invokeChain__, "__facade_invokeChain__");
function __facade_invoke__(request, env, ctx, dispatch, finalMiddleware) {
  return __facade_invokeChain__(request, env, ctx, dispatch, [
    ...__facade_middleware__,
    finalMiddleware
  ]);
}
__name(__facade_invoke__, "__facade_invoke__");

// .wrangler/tmp/bundle-E3jzs7/middleware-loader.entry.ts
var __Facade_ScheduledController__ = class ___Facade_ScheduledController__ {
  constructor(scheduledTime, cron, noRetry) {
    this.scheduledTime = scheduledTime;
    this.cron = cron;
    this.#noRetry = noRetry;
  }
  scheduledTime;
  cron;
  static {
    __name(this, "__Facade_ScheduledController__");
  }
  #noRetry;
  noRetry() {
    if (!(this instanceof ___Facade_ScheduledController__)) {
      throw new TypeError("Illegal invocation");
    }
    this.#noRetry();
  }
};
function wrapExportedHandler(worker) {
  if (__INTERNAL_WRANGLER_MIDDLEWARE__ === void 0 || __INTERNAL_WRANGLER_MIDDLEWARE__.length === 0) {
    return worker;
  }
  for (const middleware of __INTERNAL_WRANGLER_MIDDLEWARE__) {
    __facade_register__(middleware);
  }
  const fetchDispatcher = /* @__PURE__ */ __name(function(request, env, ctx) {
    if (worker.fetch === void 0) {
      throw new Error("Handler does not export a fetch() function.");
    }
    return worker.fetch(request, env, ctx);
  }, "fetchDispatcher");
  return {
    ...worker,
    fetch(request, env, ctx) {
      const dispatcher = /* @__PURE__ */ __name(function(type, init) {
        if (type === "scheduled" && worker.scheduled !== void 0) {
          const controller = new __Facade_ScheduledController__(
            Date.now(),
            init.cron ?? "",
            () => {
            }
          );
          return worker.scheduled(controller, env, ctx);
        }
      }, "dispatcher");
      return __facade_invoke__(request, env, ctx, dispatcher, fetchDispatcher);
    }
  };
}
__name(wrapExportedHandler, "wrapExportedHandler");
function wrapWorkerEntrypoint(klass) {
  if (__INTERNAL_WRANGLER_MIDDLEWARE__ === void 0 || __INTERNAL_WRANGLER_MIDDLEWARE__.length === 0) {
    return klass;
  }
  for (const middleware of __INTERNAL_WRANGLER_MIDDLEWARE__) {
    __facade_register__(middleware);
  }
  return class extends klass {
    #fetchDispatcher = /* @__PURE__ */ __name((request, env, ctx) => {
      this.env = env;
      this.ctx = ctx;
      if (super.fetch === void 0) {
        throw new Error("Entrypoint class does not define a fetch() function.");
      }
      return super.fetch(request);
    }, "#fetchDispatcher");
    #dispatcher = /* @__PURE__ */ __name((type, init) => {
      if (type === "scheduled" && super.scheduled !== void 0) {
        const controller = new __Facade_ScheduledController__(
          Date.now(),
          init.cron ?? "",
          () => {
          }
        );
        return super.scheduled(controller);
      }
    }, "#dispatcher");
    fetch(request) {
      return __facade_invoke__(
        request,
        this.env,
        this.ctx,
        this.#dispatcher,
        this.#fetchDispatcher
      );
    }
  };
}
__name(wrapWorkerEntrypoint, "wrapWorkerEntrypoint");
var WRAPPED_ENTRY;
if (typeof middleware_insertion_facade_default === "object") {
  WRAPPED_ENTRY = wrapExportedHandler(middleware_insertion_facade_default);
} else if (typeof middleware_insertion_facade_default === "function") {
  WRAPPED_ENTRY = wrapWorkerEntrypoint(middleware_insertion_facade_default);
}
var middleware_loader_entry_default = WRAPPED_ENTRY;
export {
  __INTERNAL_WRANGLER_MIDDLEWARE__,
  middleware_loader_entry_default as default
};
//# sourceMappingURL=index.js.map
