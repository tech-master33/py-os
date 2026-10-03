# Py-OS Shared Drives Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add public, writable cloud drives to Py-OS File Explorer using Cloudflare Workers KV on the Free plan, without a VPS, R2, or paid fallback.

**Architecture:** The existing catalog Worker exposes a separate shared-drive API. D1 stores drive labels, quota accounting, file and directory metadata, and resumable upload state; KV stores immutable file-content chunks under unguessable object keys. Py-OS File Explorer lets a user create a drive with a chosen quota, share its generated drive ID, add a known ID, browse it, upload and download files, create folders, and delete entries.

**Tech Stack:** Cloudflare Workers, D1, Workers KV, TypeScript, Python `requests`, wxPython, Vitest, and Python `unittest`.

## Global Constraints

- Do not use R2, a VPS, or a paid storage service.
- Keep the implementation on the Cloudflare Workers Free plan; do not enable a paid plan or paid fallback.
- The Free Workers KV account limit is 1 GB (1,000,000,000 bytes) of stored data; the service-wide aggregate drive quota must never exceed the verified remaining allowance.
- A KV value is limited to 25 MiB; file transfers use immutable chunks no larger than 20 MiB.
- Free Workers KV includes 100,000 reads/day and 1,000 writes, deletes, and list operations/day; requests beyond a free operation limit fail.
- Workers KV is eventually consistent; use unique immutable chunk keys and publish file metadata only after all chunks are written. Do not promise atomic cross-region visibility.
- A drive ID is a high-entropy bearer name. Anyone who knows it can read, upload, overwrite, and delete that drive's contents.
- Store no file bodies in D1 and leave the reserved `pyos-core-components` database unbound and schema-free.
- Do not implement the WebDAV protocol; this first version is Py-OS-only.
- Drive quota allocation is selected by the creator in whole MiB, is bounded by the configured service budget, and all allocated drive quotas together must fit within that budget. Deleting an empty drive releases its reserved quota.
- Preserve accessibility: File Explorer drive actions must be keyboard operable and announce concise status and errors through the existing speech API.

---

## File Structure

- `app_server/migrations/0003_shared_drives.sql` — D1 tables for drive quotas, directories, file metadata, resumable upload sessions, and uploaded chunk indexes.
- `app_server/src/drives.ts` — Shared-drive API validation, metadata operations, KV chunk transfer, quota enforcement, and response construction.
- `app_server/src/catalog.ts` — Dispatch `/v1/drives` requests to the new handler without changing catalog routes.
- `app_server/src/index.ts` — Add the optional `DRIVE_FILES` KV binding and configured storage budget to the Worker environment type.
- `app_server/wrangler.jsonc` — Bind separate production and staging KV namespaces after they are provisioned; configure an explicit per-environment storage budget.
- `app_server/wrangler.test.jsonc`, `app_server/vitest.config.ts`, `app_server/tests/setup.ts` — Provide an isolated local KV binding and apply the additional D1 migration in Worker tests.
- `app_server/tests/drives.test.ts` — Exercise the drive API, path rules, capability IDs, transfer integrity, and quota limits against the isolated Worker test environment.
- `cloud_drive_client.py` — HTTPS-only Python client with validated drive records, resumable chunk transfer, strict response limits, and explicit server errors.
- `tests/test_cloud_drive_client.py` — Mocked HTTP tests for client validation, upload/download ordering, retries, malformed responses, and failed transfers.
- `apps/system_apps.py` — Add cloud-drive creation/connection and remote browsing, upload, open/download, folder creation, and delete actions to File Explorer.
- `tests/test_file_explorer_cloud_drives.py` — Test File Explorer integration using fake API/client/dialog controls without contacting Cloudflare.
- `app_server/README.md` — Document KV provisioning, free limits, drive sharing risks, quota operations, deployment, and recovery.

## Task 1: Add drive metadata and local KV test binding

**Interfaces:**
- Consumes: Existing `DB: D1Database` Worker binding and Vitest D1 migration setup.
- Produces: `shared_drives`, `drive_capacity`, `drive_directories`, `drive_files`, `drive_uploads`, and `drive_upload_chunks` tables; a local `DRIVE_FILES: KVNamespace` binding in the Worker test environment.

- [ ] **Step 1: Write the D1 migration and binding test.** Add a Worker test that applies the migration and verifies a capacity singleton is initialized to the test budget.

```ts
it("initializes an empty shared-drive quota ledger", async () => {
  const row = await env.DB.prepare(
    "SELECT max_bytes, allocated_bytes FROM drive_capacity WHERE id = 1",
  ).first<{ max_bytes: number; allocated_bytes: number }>();
  expect(row).toEqual({ max_bytes: 1_000_000_000, allocated_bytes: 0 });
});
```

- [ ] **Step 2: Run the focused Worker test and verify it fails.**

Run from `app_server`: `npm test -- --run tests/drives.test.ts`.
Expected: FAIL because the migration, test KV binding, and drive test file do not exist.

- [ ] **Step 3: Create `0003_shared_drives.sql`.** Create strict tables with these columns and constraints:
  - `drive_capacity(id INTEGER PRIMARY KEY CHECK (id = 1), max_bytes INTEGER NOT NULL, allocated_bytes INTEGER NOT NULL CHECK (allocated_bytes >= 0))`; insert row `(1, 1000000000, 0)` for local migration defaults.
  - `shared_drives(id TEXT PRIMARY KEY, name TEXT NOT NULL, quota_bytes INTEGER NOT NULL CHECK (quota_bytes > 0), used_bytes INTEGER NOT NULL DEFAULT 0 CHECK (used_bytes >= 0), reserved_bytes INTEGER NOT NULL DEFAULT 0 CHECK (reserved_bytes >= 0), created_at TEXT NOT NULL)`.
  - `drive_directories(drive_id TEXT NOT NULL, path TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (drive_id, path), FOREIGN KEY (drive_id) REFERENCES shared_drives(id))`.
  - `drive_files(drive_id TEXT NOT NULL, path TEXT NOT NULL, size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0), sha256 TEXT NOT NULL, object_id TEXT NOT NULL UNIQUE, chunk_count INTEGER NOT NULL CHECK (chunk_count > 0), updated_at TEXT NOT NULL, PRIMARY KEY (drive_id, path), FOREIGN KEY (drive_id) REFERENCES shared_drives(id))`.
  - `drive_uploads(id TEXT PRIMARY KEY, drive_id TEXT NOT NULL, path TEXT NOT NULL, size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0), sha256 TEXT NOT NULL, object_id TEXT NOT NULL UNIQUE, chunk_count INTEGER NOT NULL CHECK (chunk_count > 0), expires_at INTEGER NOT NULL, FOREIGN KEY (drive_id) REFERENCES shared_drives(id))`.
  - `drive_upload_chunks(upload_id TEXT NOT NULL, chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0), size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0), sha256 TEXT NOT NULL, PRIMARY KEY (upload_id, chunk_index), FOREIGN KEY (upload_id) REFERENCES drive_uploads(id))`.
- [ ] **Step 4: Configure isolated test KV and migration loading.** Add one fake `DRIVE_FILES` KV namespace in `wrangler.test.jsonc`; load and apply `0003_shared_drives.sql` in `vitest.config.ts` and `tests/setup.ts`. Clear all drive tables and the test KV namespace in the Worker test `beforeEach`.
- [ ] **Step 5: Run focused and existing Worker tests.**

Run from `app_server`: `npm test -- --run`.
Expected: all current catalog tests and the new quota-ledger test pass.

## Task 2: Implement drive creation, browsing, and directory metadata

**Interfaces:**
- Consumes: Task 1 D1 tables, `DRIVE_FILES`, and the configured byte budget.
- Produces: `handleDriveRequest(request: Request, env: DriveEnv): Promise<Response>`; `POST /v1/drives` creates a drive; `GET /v1/drives/{id}` returns its public summary; `GET /v1/drives/{id}/entries?path=` lists direct children; `POST /v1/drives/{id}/directories` creates an empty directory; `DELETE /v1/drives/{id}/entries?path=` deletes a file or empty directory; `DELETE /v1/drives/{id}` deletes an empty drive and releases its quota.
- JSON response shapes are `{id, name, quota_bytes, used_bytes}` for a drive and `{entries: [{name, type, size_bytes, updated_at}]}` for a listing.

- [ ] **Step 1: Add failing route and validation tests.** Cover creation below/at/over the aggregate quota, names with invalid length/control characters, IDs that are missing or malformed, path traversal, duplicate directories, listing only immediate children, non-empty directory deletion, empty-drive deletion and quota release, deletion rejection while files remain, and the unconfigured-KV response.
- [ ] **Step 2: Run the focused test and verify the expected failures.**

Run from `app_server`: `npm test -- --run tests/drives.test.ts`.
Expected: new routes return 404 or have no implementation.

- [ ] **Step 3: Implement `DriveEnv`, validators, JSON responses, and quota-atomic creation.** Require a 32-character base64url drive ID generated from 24 cryptographically random bytes. Validate display names to 1–64 characters with no control characters. Accept `quota_bytes` only as a positive whole-MiB integer no greater than `DRIVE_STORAGE_LIMIT_BYTES`. Use a conditional update of `drive_capacity` followed by an `INSERT ... SELECT ... WHERE changes() = 1` in one D1 batch so simultaneous anonymous creations cannot reserve more than the configured budget. Return the generated ID as the access-bearing drive name. Keep `drive_capacity.max_bytes` synchronized with the configured environment budget before every quota-changing operation; refuse to lower the budget below currently allocated drive quotas.
- [ ] **Step 4: Implement drive summary, safe path normalization, direct-child listings, and directory creation/deletion.** Use POSIX `/` separators in API paths; reject absolute paths, backslashes, empty components, `.`/`..`, control characters, and paths over 1,024 UTF-8 bytes. Build child directories from explicit directory rows and file path prefixes. Reject file/directory collisions.
- [ ] **Step 5: Dispatch drive routes from the Worker and run tests.** Route `/v1/drives` to `handleDriveRequest` before the catalog's 404 fallback. Return HTTP 503 with `{"error":"drive_storage_unavailable"}` when `DRIVE_FILES` is not bound; do not silently fall back to D1.
- [ ] **Step 6: Run focused and full Worker tests.**

Run from `app_server`: `npm test -- --run`.
Expected: all drive route tests and existing catalog tests pass.

## Task 3: Implement quota-safe chunk uploads, downloads, and deletion

**Interfaces:**
- Consumes: Task 2 drive lookup and validated paths.
- Produces: `POST /v1/drives/{id}/uploads` accepts `{path, size_bytes, sha256}` and returns `{upload_id, chunk_size: 20971520, chunk_count}`; `PUT /v1/drives/{id}/uploads/{upload_id}/{index}` stores one immutable chunk; `POST /v1/drives/{id}/uploads/{upload_id}/complete` publishes the file metadata; `GET /v1/drives/{id}/files?path=` streams a file; file deletion removes the active pointer and every known KV chunk.
- Each content object ID is a new random value, and each chunk key is `drive:{object_id}:{index}`; do not overwrite a key that may have been read in another Cloudflare location.

- [ ] **Step 1: Add failing tests for reservations, chunk validation, completion, readback, and deletion.** Verify empty files round-trip as one zero-byte chunk; wrong chunk sizes and hashes do not publish a file; incomplete uploads cannot complete; simultaneous reservations cannot exceed `quota_bytes`; overwriting a file accounts for its previous size; a failed chunk leaves the prior file intact; deleting a file releases its bytes; and the final binary bytes match the uploaded bytes.
- [ ] **Step 2: Run the focused test and verify it fails.**

Run from `app_server`: `npm test -- --run tests/drives.test.ts`.
Expected: upload and file routes return 404 or fail their asserted contracts.

- [ ] **Step 3: Implement upload reservations.** Enforce `MAX_CHUNK_BYTES = 20 * 1024 * 1024`, `MAX_FILE_BYTES <= DRIVE_STORAGE_LIMIT_BYTES`, and `chunk_count = max(1, ceil(size_bytes / MAX_CHUNK_BYTES))`. In one D1 batch, reserve the incoming bytes only when `used_bytes + reserved_bytes - replaced_file_size + incoming_size <= quota_bytes`; expire abandoned sessions after 24 hours and release their reserved bytes.
- [ ] **Step 4: Implement immutable KV chunk writes.** Verify each request's declared index, expected byte count, and SHA-256 before writing. Insert the chunk index/digest/size into `drive_upload_chunks` only after `DRIVE_FILES.put()` succeeds. Identical retries return success without rewriting the KV key; different bytes at an already accepted index return HTTP 409.
- [ ] **Step 5: Implement atomic publish, bounded reads, and deletion.** Completion checks every expected chunk index and the total declared size, then atomically upserts the `drive_files` pointer and adjusts `used_bytes`/`reserved_bytes`. Downloads read the pointer from D1 and emit chunks in order with `application/octet-stream`, `content-length`, `x-file-size-bytes`, `x-file-sha256`, and `cache-control: no-store`; clients use the explicit size header because streaming responses may not retain `content-length` through the Worker edge. File deletion marks the entry as deleting, deletes each corresponding KV chunk key, and only then removes metadata and releases used bytes; report KV deletion errors rather than returning a success-shaped response. Delete a drive only when it has no files, directories, or upload reservations, then decrement `drive_capacity.allocated_bytes`.
- [ ] **Step 6: Run all Worker tests and TypeScript validation.**

Run from `app_server`: `npx tsc --noEmit; npm test -- --run`.
Expected: type-check succeeds and all Worker tests pass.

## Task 4: Add the Py-OS HTTPS drive client

**Interfaces:**
- Consumes: Task 2–3 HTTP routes and the production catalog HTTPS origin.
- Produces: `cloud_drive_client.py` with `CloudDriveClient(base_url, session=None)`, `create_drive(name, quota_bytes)`, `get_drive(drive_id)`, `list_entries(drive_id, path)`, `create_directory(drive_id, path)`, `upload_file(drive_id, local_path, remote_path)`, `download_file(drive_id, remote_path, destination)`, `delete_entry(drive_id, path)`, and `delete_empty_drive(drive_id)`.

- [ ] **Step 1: Write mocked HTTP tests for the client contracts.** Include HTTPS-origin rejection, redirect refusal, invalid drive ID/path rejection, malformed JSON rejection, correct sequential chunk requests, retrying the same chunk safely, cleanup after incomplete upload, download integrity, empty-drive deletion, and propagation of HTTP/quota errors.
- [ ] **Step 2: Run the new Python test module and verify the expected failures.**

Run from repository root: `python -m unittest tests.test_cloud_drive_client -v`.
Expected: import failure because the client module does not exist.

- [ ] **Step 3: Implement the HTTPS request layer.** Follow `app_catalog_client.py`: accept HTTPS origins only; use `requests.Session.get/post/put/delete` with timeouts and `allow_redirects=False`; cap JSON response bodies at 1 MiB and chunk response/request bodies at 20 MiB; raise a typed `CloudDriveError` with safe server messages on non-2xx responses.
- [ ] **Step 4: Implement resumable file transfer.** Read local files in 20 MiB pieces, hash each piece, create an upload session with total size and whole-file SHA-256, PUT missing chunks in ascending order, and call complete only after all chunks succeed. Download into a sibling temporary file, verify its final size and SHA-256, then atomically replace the destination so failed transfers do not damage an existing file.
- [ ] **Step 5: Run the new and related Python tests.**

Run from repository root: `python -m unittest tests.test_cloud_drive_client tests.test_app_catalog_client -v`.
Expected: all drive-client and catalog-client tests pass.

## Task 5: Integrate shared drives into File Explorer

**Interfaces:**
- Consumes: `CloudDriveClient` from Task 4 and `SystemAPI.get_data_path`, `open_file`, and `speak`.
- Produces: File Explorer actions for **Create Shared Drive**, **Add Shared Drive**, **Upload File**, **New Folder**, **Download/Open**, **Delete Entry**, **Delete Empty Shared Drive**, and **Remove Connection**; locally saved connection records contain only `{id, name}` and are written atomically to `cloud_drives.json`.

- [ ] **Step 1: Add UI behavior tests with fake dialogs and client methods.** Verify cancelled dialogs do nothing, created drives display their generated ID once, adding an existing ID persists it, refresh lists the saved drives, upload/download/delete call only the selected drive, and API errors are spoken without clearing the current location or local list.
- [ ] **Step 2: Run the focused Python tests and verify the expected failures.**

Run from repository root: `python -m unittest tests.test_file_explorer_cloud_drives -v`.
Expected: import or behavior failures because the drive integration is not implemented.

- [ ] **Step 3: Add persistent connection loading/saving and drive actions to This PC.** Use `wx.TextEntryDialog` for the drive label/ID and quota MiB, `wx.FileDialog` for upload/download destinations, and `wx.MessageBox` for delete confirmation. Persist records through a temporary file followed by `os.replace`; reject duplicate IDs and malformed stored JSON with an explicit spoken error. Show a warning that the generated drive ID grants public read/write/delete access before creating a drive.
- [ ] **Step 4: Add a virtual remote location to File Explorer navigation.** Represent remote folders as `(drive_id, POSIX path)` rather than host paths; list entries through the client; route Back, Up, address, activation, and keyboard actions without passing remote paths to `os.path` or the host filesystem. Open supported text/audio files only after verified download into Py-OS user data. Removing a local connection must not delete the shared drive; remote deletion requires an explicit confirmation and an empty drive.
- [ ] **Step 5: Add upload, new-folder, and delete controls.** Show byte usage and quota when entering a shared drive. Require confirmation before deleting a remote entry or deleting an empty shared drive; keep **Remove Connection** separate and local-only. Announce success only after the API confirms it. Preserve the user's current selection and announce network/quota failures.
- [ ] **Step 6: Run focused UI tests and adjacent accessibility tests.**

Run from repository root: `python -m unittest tests.test_file_explorer_cloud_drives tests.test_app_catalog_ui -v`.
Expected: all tests pass with no external network calls.

## Task 6: Document, provision free-tier bindings, deploy, and verify

**Interfaces:**
- Consumes: completed Worker routes, D1 schema, KV binding, client, and File Explorer integration from Tasks 1–5.
- Produces: updated operations documentation and a deployed, verified Py-OS shared-drive service with no paid resources.

- [ ] **Step 1: Update `app_server/README.md`.** Document KV's 1 GB account cap, 25 MiB value cap, 100,000 read/day and 1,000/day write/delete/list limits; state that operation limits fail rather than trigger paid fallback. Explain that every drive ID grants public read/write/delete access, that it must be shared like a password, and that free-tier availability/limits can change. Correct the catalog status text to note that the Welcome sample was removed from both catalogs.
- [ ] **Step 2: Run the complete local validation.**

Run from repository root: `python -m unittest tests.test_cloud_drive_client tests.test_file_explorer_cloud_drives -v`.
Run from `app_server`: `npx tsc --noEmit; npm test -- --run; npx wrangler deploy --env production --dry-run`.
Expected: all tests, type-check, and production configuration dry run pass.

- [ ] **Step 3: Confirm Cloudflare account configuration before provisioning.** Use `npx wrangler kv namespace list` and the Cloudflare dashboard to confirm available KV allowance and that no paid Workers plan or automatic paid fallback is enabled. Stop if the account cannot guarantee the configured drive budget within its free allowance.
- [ ] **Step 4: Provision only free KV namespaces and apply D1 migrations.** Create separate staging and production namespaces with `npx wrangler kv namespace create pyos-shared-drives-staging` and `npx wrangler kv namespace create pyos-shared-drives-production`; bind them only to their matching Wrangler environments. Set each `DRIVE_STORAGE_LIMIT_BYTES` so the sum of staging and production service budgets is no more than the verified remaining 1,000,000,000-byte account allowance, leaving room for any pre-existing KV data. Apply `0003_shared_drives.sql` to staging and production D1 metadata databases; do not bind or modify `pyos-core-components`.
- [ ] **Step 5: Deploy staging, then production after a clean test drive.** Deploy staging first and verify create, add-by-ID, browse, upload, download, folder creation, quota rejection, file deletion, and empty-drive deletion using a tiny test drive. Remove the staging test drive and confirm its objects are deleted. Deploy production only after those checks pass; never publish or print drive IDs containing test data.
- [ ] **Step 6: Verify production and record the deployed state.** Create one small private-to-the-link test drive with an explicitly bounded quota; verify an independent Py-OS client can add its generated ID and round-trip binary data; delete the test drive and confirm its D1 rows and KV chunks are removed. Verify `/v1/health`, the public app catalog, and app-package downloads remain unchanged. Do not create a paid plan or use R2.
- [ ] **Step 7: Commit the completed implementation.**

```powershell
git add app_server apps cloud_drive_client.py tests docs/superpowers/plans/2026-10-03-pyos-shared-drives.md
git commit -m "feat: add public Py-OS shared drives" -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
```

## Self-Review

- **Coverage:** The plan implements a no-VPS, no-R2, free-tier server; configurable drive size; File Explorer add/browse/create/upload/download/delete; name-based public read/write access; storage and daily operation caps; and local, staging, and production verification.
- **Known limitation:** KV's eventual consistency means cross-region reads can briefly lag a write. Immutable chunk keys and D1-published pointers reduce stale-overwrite risk but do not make KV strongly consistent.
- **Abuse limitation:** Anyone with a drive ID can destroy that drive's contents. The generated ID must be high-entropy; the Py-OS UI must display a clear public-sharing warning before creating or adding a drive.
- **No paid overage:** Configure an explicit storage budget below the verified free allowance and return a clear quota-exhausted error before writing beyond it. If no safe free allowance remains, stop before provisioning or deployment.
