# PyOS App Catalog Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a dynamic, low-cost hosted catalog API and an accessible PyOS client for discovering, verifying, installing, updating, and removing maintainer-published multi-file Python apps.

**Architecture:** Run a TypeScript Cloudflare Worker with D1 holding catalog metadata and app-package chunks as separate rows. Each release is a ZIP package that may contain many files and has a signed manifest listing the entry point, file paths, sizes, and hashes; its archive is chunked into 1 MiB pieces for storage and transfer. A maintainer signs and publishes through token-protected Worker endpoints; PyOS downloads and verifies the complete package before safely installing it under the per-user data directory. No app files are hosted on GitHub or R2, and the server never executes PyOS apps.

**Tech Stack:** TypeScript Cloudflare Workers/Wrangler, Cloudflare D1, Vitest Workers Pool, PyOS Python/wxPython client, Web Crypto Ed25519 signatures.

## Global Constraints

- “The thing can't be static.”
- “I don't have my own VPS.”
- Do not use GitHub or R2 for app hosting; store app metadata and package files in D1.
- Prioritize “$0/month initially” using free-tier services and their limits.
- “Maintainer-published apps only for the first version”; no public uploads or submissions.
- Apps remain locally executed Python `BlindApp` plugins; the server only provides catalog metadata and release information.
- Support multi-file ZIP app packages from the first version; do not impose a single-file-only app format.
- Set the maximum compressed and total extracted package size to 100 MiB for now. The limit applies to each app release; this is not a user upload quota, and publishing remains maintainer-only.
- Package entry points are app-package `__init__.py` modules so sibling Python modules can be imported as a package; retain support for today's bundled single-file apps.
- Do not run `pip` or execute package build/install hooks on a user's machine; catalog apps may contain Python modules and data files, and their dependencies must already be supported by PyOS.
- Do not add user accounts or collect personal data for the first version.
- Do not silently upgrade to paid hosting; free-tier exhaustion may make the API unavailable until usage resets or the owner approves a paid plan.

---

## Research Findings and Recommendation

### Existing project behavior

- `desktop.py:340-363` scans only the repository's `apps/` directory at startup and imports each `.py` file. It currently swallows loader errors.
- `app_paths.py` already provides `get_data_dir()`, defaulting to `~/.py-os` with a `PY_OS_DATA_DIR` override. This gives downloaded plugins a writable, per-user location separate from bundled app files.
- `apps/DEVELOPER_GUIDE.md` defines the `BlindApp` plugin contract and documents host-filesystem access through Python. Downloaded plugins therefore execute as local code with the user's normal process permissions.
- `requirements.txt` has `requests`, but no server framework or signature-verification package. Keep server dependencies isolated from the desktop's requirements.

### Hosting options

| Option | Fit | Main tradeoff |
| --- | --- | --- |
| **Cloudflare Python Worker + D1 (recommended)** | Dynamic HTTPS API, no VPS, free allowances for modest API/catalog traffic, multi-file app packages stored in the database, and a `workers.dev` hostname without buying a domain. | Worker Python runs in Pyodide/WebAssembly. D1 has a 2 MB maximum row and a 500 MB maximum database on the free plan. Store 1 MiB package chunks in separate rows; a 100 MiB package takes about 100 rows and requests per download. The free database holds about four 100 MiB packages after overhead, not an unlimited catalog. |
| Render free web service | Straightforward conventional FastAPI hosting. | Render says free instances are not for production; the service sleeps after 15 minutes idle, takes about a minute to wake, and has ephemeral storage. |
| Railway | Conventional app/container hosting and easy deployment. | Its Hobby plan has a $5 monthly subscription, so it is not the first choice for a $0 start. |
| Cloudflare Pages/static files | Easy static hosting. | Not sufficient alone because the requirement is a dynamic server-side API. |

**Recommendation:** Use a TypeScript Worker with D1 only: do not use GitHub, R2, or a static site to host app files. App releases are ZIP archives containing any number of approved files, capped at 100 MiB compressed and 100 MiB total extracted size per release. The publisher signs a manifest containing the app entry point, per-file paths/sizes/digests, archive digest, and ordered 1 MiB chunk hashes. D1 stores each chunk as its own row, and the public API serves chunks by app/version/index; this stays below the 2 MB per-row limit and supports multi-file plugins without a one-file-only restriction. A publish session creates a pending release, uploads validated chunks, and marks it published only after all declared chunks are present. PyOS downloads chunks to a temporary archive, verifies the signed manifest and archive digest, checks every extracted path/file against the manifest and expansion limit, and then installs atomically. The D1 free database cap is 500 MB total, so retaining 100 MiB packages permits only about four releases after metadata and overhead; pruning old releases or approving a paid/alternate storage plan will be needed as the catalog grows. This limit is total shared catalog storage, not a per-user quota, and the initial publisher remains maintainer-only. At the time researched, Cloudflare documents 100,000 Worker requests/day, 5 million D1 rows read/day, and 100,000 D1 rows written/day on free plans. A 100 MiB app download consumes roughly 100 requests per user. It needs no custom domain or VPS. Cloudflare's paid Workers plan starts at $5/month; usage and product terms can change. Keep the account on the free plan unless the owner explicitly approves a paid upgrade.

Use TypeScript for the Worker so D1's official typed bindings, binary request/response bodies, and the Worker's native Web Crypto Ed25519 verification are available without a Python-to-JavaScript FFI dependency. The publisher and PyOS desktop remain Python. Wrangler and Workers Pool tests run through the isolated server's npm project.

### Trust and safety boundary

An app plugin is executable Python and can access the host through the PyOS process. HTTPS and a download hash alone do not establish publisher authenticity if the service or account is compromised. Each release must therefore include a manifest and Ed25519 signature, signed by a private key held only in the authorized maintainer environment; the PyOS client embeds the corresponding public key and verifies the manifest and exact downloaded bytes before installation. Require explicit user confirmation, install only beneath the PyOS user-data apps directory, and never overwrite bundled plugins. This does not sandbox installed apps: make that limitation clear in the UI and documentation.

## File Structure

| File | Responsibility |
| --- | --- |
| `app_server/package.json` | Pinned Wrangler, TypeScript, Vitest, and Workers Pool dependencies and scripts. |
| `app_server/tsconfig.json` | TypeScript compiler settings and Cloudflare Worker types. |
| `app_server/wrangler.jsonc` | Worker entry point, compatibility date, D1 binding, and `workers.dev` deployment configuration. |
| `app_server/migrations/0001_catalog.sql` | D1 schema and indexes for app metadata, immutable version manifests/signatures, pending/published state, and 1 MiB package chunks. |
| `app_server/src/index.ts` | HTTP Worker entry point and route dispatch. |
| `app_server/src/catalog.ts` | Catalog query, response shaping, input validation, release staging/publishing, chunk storage, and stable JSON schema. |
| `app_server/tests/catalog.test.ts` | Catalog behavior and response-contract tests against local D1. |
| `app_catalog/registry.json` | Maintainer-controlled, reviewed list of app package directories, entry points, and metadata approved for publication. |
| `app_catalog/publish.py` | Validate package trees, build/sign a file manifest and chunk digests, and submit package chunks to the protected D1-backed publish API. |
| `app_catalog/tests/test_publish.py` | Registry validation, manifest canonicalization, and signature tests. |
| `app_catalog_client.py` | wx-independent HTTP client, response validation, download handling, and signature verification. |
| `apps/app_catalog.py` | Accessible catalog browser and install/update/uninstall UI. |
| `app_paths.py` | Add a helper returning the per-user plugin directory beneath `get_data_dir()`. |
| `desktop.py` | Load existing single-file plugins and `__init__.py` entry points from bundled/user-installed multi-file app-package directories while preventing downloaded apps from replacing bundled files. |
| `requirements.txt` | Add a supported Ed25519 verification dependency for the desktop client. |
| `tests/test_app_catalog_client.py` | Offline API client, download, signature, and failure-path tests. |
| `tests/test_desktop_helpers.py` | User plugin directory and plugin discovery regression tests. |
| `apps/DEVELOPER_GUIDE.md` | Document catalog publishing and the trust/permission implications of installing plugins. |
| `app_server/README.md` | Local development, Cloudflare account setup, token scopes, direct deploy/publish commands, rollback, quotas, and operations. |

## Task 1: Define the Catalog Contract and Worker Skeleton

**Files:**
- Create: `app_server/package.json`
- Create: `app_server/tsconfig.json`
- Create: `app_server/wrangler.jsonc`
- Create: `app_server/vitest.config.ts`
- Create: `app_server/migrations/0001_catalog.sql`
- Create: `app_server/src/index.ts`
- Create: `app_server/src/catalog.ts`
- Create: `app_server/tests/catalog.test.ts`

**Interfaces:**
- `GET /v1/health` returns `{"status":"ok"}`.
- `GET /v1/apps` returns `{"apps":[...]}`; each app has `id`, `name`, `description`, `version`, `min_pyos_version`, `package_size`, `chunk_size`, and `chunk_count`.
- `GET /v1/apps/{id}` returns one app record or a JSON 404 response.
- `GET /v1/apps/{id}/{version}/manifest` returns the signed manifest for that published version.
- `GET /v1/apps/{id}/{version}/chunks/{index}` returns one raw binary chunk (at most 1 MiB) from that published version.
- `POST /v1/admin/releases` starts a pending release using a valid publish token and signed manifest; it enforces a 100 MiB archive limit and validates app/version/manifest before creating a pending record.
- `PUT /v1/admin/releases/{id}/{version}/chunks/{index}` stores one chunk only when its index and SHA-256 match the signed manifest. `POST /v1/admin/releases/{id}/{version}/publish` makes the immutable version public only when all declared chunks are present. These routes are not available to ordinary clients.
- Public catalog/download routes are read-only. Unknown routes return JSON 404; malformed IDs return JSON 400; unauthenticated or invalid publishing requests return JSON 401/400 without writing.
- D1 stores app identity, display metadata, compatible PyOS version, immutable version records, signed manifest/signature, compressed and expanded package sizes, chunk count, individual chunk bytes, and publication timestamp. The manifest contains the entry point, every normalized archive path and file digest, archive digest, and ordered chunk digests. Public response URLs use the Worker origin.

- [x] **Step 1: Create isolated tooling and failing contract tests.** Add pinned Wrangler, TypeScript, Vitest, and `@cloudflare/vitest-pool-workers` development dependencies in `app_server/package.json`. Configure local D1 in `app_server/wrangler.jsonc`; write Workers Pool tests for health, list, detail, manifest, chunk delivery, malformed identifiers, exact response fields, and unpublished release invisibility.
- [ ] **Step 2: Run targeted tests and confirm they fail.** Run `npm --prefix app_server test -- --run`. Expected: Vitest reports missing Worker routes/schema because the implementation is not in place yet.
- [ ] **Step 3: Add D1 schema and typed Worker routes.** Define indexed app/version and chunk-index fields, status, signed manifest, chunk digest, and chunk BLOB. Add D1 binding types and route handlers. Only published versions are visible publicly; enforce 100 MiB compressed/expanded package caps, a 1 MiB chunk cap, a 1 MiB manifest cap, and D1 row-size limits before writes. Use Web Crypto Ed25519 verification.
- [ ] **Step 4: Run local Worker integration checks.** Run `npm --prefix app_server test -- --run`, then `npm --prefix app_server run dev` and request `/v1/health`, `/v1/apps`, and a missing app route; verify JSON responses and status codes.
- [ ] **Step 5: Commit the independently deployable API skeleton.** Include only `app_server/` changes in the commit.

## Task 2: Add the Reviewed Registry and Signed Release Publisher

**Files:**
- Create: `app_catalog/registry.json`
- Create: `app_catalog/publish.py`
- Create: `app_catalog/tests/test_publish.py`
- Modify: `app_server/src/catalog.ts`

**Interfaces:**
- Registry entries identify an app by stable ID and a package directory under an approved source root; the required entry point is package-root `__init__.py`; include name, description, semantic version, minimum PyOS version, and release notes.
- Publisher rejects symlinks, path traversal, unsupported file types, duplicate normalized paths, ZIP archives over 100 MiB, and archives that expand beyond 100 MiB. It produces a ZIP that can contain many Python modules and app data files.
- A canonical JSON manifest lists app ID, version, compatibility floor, `__init__.py` entry point, normalized file paths/sizes/SHA-256 digests, compressed/uncompressed archive sizes and digest, and ordered 1 MiB chunk hashes. Ed25519 signs the canonical manifest bytes. The client checks all signed identity/version/size fields before extracting any file.
- Publisher signs locally in an authorized maintainer environment, starts a pending release with the signed manifest, uploads the archive chunks, and asks the API to publish only after all chunks are present. The Worker verifies the manifest signature and each chunk digest before accepting storage.
- The publisher token and private signing key are read from environment variables and are never printed, transmitted in URLs, or persisted in the repository.

- [ ] **Step 1: Write failing publisher tests.** Cover valid and invalid IDs, missing metadata, symlinks/path traversal, ZIP traversal, duplicate paths, unsupported file types, multiple Python files, compressed and expanded sizes at and above 100 MiB, manifests above 1 MiB, chunk boundaries, deterministic manifests, tampered chunks/files, duplicate versions, and Ed25519 signature verification.
- [ ] **Step 2: Run the targeted tests and confirm they fail.** Run `python -m unittest app_catalog.tests.test_publish -v`. Expected: failures because registry validation and publisher functions are absent.
- [ ] **Step 3: Add a reviewed multi-file app package and publisher implementation.** Include an entry point and at least one imported sibling module in the sample package. Build a deterministic ZIP, per-file and per-chunk hashes, canonical manifest, and Ed25519 signature locally. Send one chunk per bounded request; the Worker verifies each chunk against the signed manifest, and the publish-finalize route exposes it only after every chunk is present. Reject a reused app/version with different bytes.
- [ ] **Step 4: Verify publication and repeatability.** Run publisher tests and a local dry run with a generated test key and mocked Worker requests. Confirm identical source/metadata produce identical ZIP/manifest bytes, any modified file/chunk fails verification, a missing/wrong token writes nothing, compressed or expanded packages above 100 MiB and manifests above 1 MiB are rejected, missing chunks prevent publication, and retrying an identical chunk/release is idempotent.
- [ ] **Step 5: Commit registry and publisher independently.** Keep signing secrets out of the commit.

## Task 3: Provision and Deploy Directly to Cloudflare

**Files:**
- Create: `app_server/README.md`
- Modify: `app_server/wrangler.jsonc`

**Interfaces:**
- Cloudflare authentication uses an authorized Wrangler session or scoped `CLOUDFLARE_API_TOKEN`; publishing also requires `APP_CATALOG_SIGNING_KEY` and a secret `APP_CATALOG_PUBLISH_TOKEN`.
- `npm --prefix app_server run deploy` deploys the dynamic API; `python app_catalog/publish.py publish --app-id APP_ID --version VERSION` signs an approved app and sends it to the Worker over HTTPS.
- Public clients can read but cannot create, change, or delete catalog entries.

- [ ] **Step 1: Document account provisioning and least-privilege credentials.** Explain how the owner creates/signs into a Cloudflare account, provisions one D1 database, creates a scoped deployment token, configures the Worker publish-token secret, and sets a separate local signing key and publish token without sharing them in chat or committing them. Use `workers.dev`; do not require a custom domain.
- [ ] **Step 2: Add D1 bindings and deployment configuration.** Configure local/remote D1 bindings, migrations, and separate staging Worker/database names. Do not enable a paid plan.
- [ ] **Step 3: Add the authenticated publish route and CLI.** The Worker accepts bounded HTTPS JSON manifest requests and binary chunk requests with the publish token, validates the signature and each digest, and keeps the release pending until finalization verifies all expected chunks exist. The local CLI validates the registry/package and signs before sending; it never sends the private key.
- [ ] **Step 4: Run local checks before remote provisioning.** Run Workers Pool and publisher unit tests, inspect the Wrangler configuration, and confirm test commands use local resources and never require a Cloudflare token.
- [ ] **Step 5: Deploy a staging instance once the owner authorizes Cloudflare access.** Apply migrations, deploy a non-production Worker, publish a multi-file test app to a staging database, verify catalog/manifest/chunk responses over HTTPS and launch the app from a temporary data directory, then remove the staging rows.
- [ ] **Step 6: Document rollback and cost controls.** Include direct deployment, D1 migration, immutable version records, free-tier limits, package pruning, and the rule that a paid plan requires explicit owner approval. State that the free 500 MB D1 cap means the catalog can retain only about four 100 MiB releases after overhead; do not imply per-user 100 MiB storage or unlimited package hosting.

## Task 4: Implement a Safe, Testable Desktop Catalog Client

**Files:**
- Create: `app_catalog_client.py`
- Create: `tests/test_app_catalog_client.py`
- Modify: `requirements.txt`

**Interfaces:**
- `CatalogClient(base_url, public_key, session=None)` exposes `list_apps()`, `get_app(app_id)`, and `download_verified(app_record, destination_dir)`. It downloads each bounded chunk to a temporary archive, verifies the signed manifest, chunk hashes, total archive digest, every file digest, app ID/version, and compatibility floor, safely extracts only manifest-listed files, and atomically installs the package.
- Each compressed package is at most 100 MiB; chunks are at most 1 MiB and downloaded one at a time. Network failures, non-2xx status, invalid JSON/schema, oversize limits, incompatible app versions, digest mismatch, unsafe archive paths, and invalid signatures raise explicit typed errors; none become empty catalogs or success-shaped results.
- Client permits only HTTPS URLs on the configured Worker origin and downloads chunks through validated app/version/index routes; it does not follow redirects to arbitrary hosts.

- [ ] **Step 1: Write offline failing tests.** Mock HTTP responses for valid multi-chunk catalog downloads, timeout, server error, malformed JSON, invalid IDs, untrusted hosts, missing/duplicate/out-of-order chunks, package/chunk size limits, digest mismatch, invalid signature, zip-slip paths, unlisted archive files, compatibility mismatch, and a valid multi-file install. Ensure tests cannot make real network calls.
- [ ] **Step 2: Run tests and confirm they fail.** Run `python -m unittest tests.test_app_catalog_client -v`. Expected: import errors and missing behavior until the client exists.
- [ ] **Step 3: Add the minimal client and Ed25519 verification.** Use `requests` with explicit connect/read timeouts, bounded per-chunk response sizes, strict schema validation, ordered chunk assembly, full archive/file SHA-256 verification, and signature verification against the embedded public key. Extract only manifest paths after rejecting absolute paths, `..`, duplicate paths, symlinks, and undeclared files. Add the verification package to desktop requirements and keep Worker tooling dependencies out of root `requirements.txt`.
- [ ] **Step 4: Verify failure and success paths.** Run the targeted unittest module. Confirm every invalid response raises the documented error, no package is installed before every chunk/signature/path check succeeds, a valid multi-file archive installs under the requested temporary directory, and mocked tests make no external connections.
- [ ] **Step 5: Commit the API client and tests.**

## Task 5: Integrate Catalog Browse and Per-User Plugin Management

**Files:**
- Create: `apps/app_catalog.py`
- Modify: `app_paths.py`
- Modify: `desktop.py`
- Create: `tests/test_app_catalog_ui.py`
- Modify: `tests/test_desktop_helpers.py`
- Modify: `pyos_knowledge.py`

**Interfaces:**
- `app_paths.get_user_apps_dir()` returns `<get_data_dir()>/apps` and creates it only when installing an app.
- Desktop discovery loads existing bundled `.py` apps and entry-point modules from bundled/installed package directories. An installed package cannot replace a bundled app by ID, module, filename, or path.
- The accessible catalog app lists app names/descriptions, speaks request/install errors through `self.api.notify()`, and requires explicit confirmation before installing or updating executable code.
- Uninstall removes only the selected installed app's files beneath the resolved user apps directory. Updates install a verified new version without touching bundled app files.

- [ ] **Step 1: Add failing path and loader tests.** Test that the user's app directory honors `PY_OS_DATA_DIR`, is independent from the source `apps/` directory, is searched at startup, loads an app entry point that imports a sibling module, and cannot shadow a bundled module or escape the user apps directory.
- [ ] **Step 2: Run targeted tests and confirm failure.** Run `python -m unittest tests.test_desktop_helpers -v`. Expected: user plugin directory cases fail before the helper and discovery change exist.
- [ ] **Step 3: Add user app path and merge discovery sources.** Preserve all existing bundled `.py` discovery and launch behavior; load a package directory by importing its `__init__.py` under a unique PyOS package namespace with that directory as its submodule search path. Enforce path containment and reserved app/module names, and do not add catalog packages to global `sys.path`.
- [ ] **Step 4: Add the accessible catalog UI.** Provide refresh, browse details, install confirmation, update, and uninstall actions; maintain keyboard navigation and concise screen-reader announcements. Show a clear warning that installed plugins run locally with PyOS permissions and are not sandboxed.
- [ ] **Step 5: Add UI and installer boundary tests.** Mock the API client and dialogs; verify cancel never writes a file, a valid confirmed multi-file package installs, failed chunk/signature/path verification leaves no installed files, install size is capped at 100 MiB, uninstall cannot delete outside the user apps directory, and existing installed apps remain when refresh fails.
- [ ] **Step 6: Run relevant tests and update AI/help documentation.** Run desktop helper, API client, and catalog UI tests; update `pyos_knowledge.py` and `apps/DEVELOPER_GUIDE.md` so PyOS's catalog behavior and plugin trust boundary are accurately described.
- [ ] **Step 7: Commit the desktop integration and documentation.**

## Task 6: End-to-End Release and Operations Verification

**Files:**
- Modify: `app_server/README.md`

- [ ] **Step 1: Run the focused full test set.** Run `python -m unittest tests.test_app_catalog_client tests.test_desktop_helpers tests.test_app_catalog_ui app_catalog.tests.test_publish -v` and `npm --prefix app_server test -- --run`.
- [ ] **Step 2: Deploy a staging release and exercise the full client flow.** Publish a test plugin whose entry point imports a sibling Python module and reads an included data file; verify its D1 record and signed archive chunks through the live Worker, install it into a clean temporary `PY_OS_DATA_DIR`, restart PyOS, launch it, update it, and uninstall it.
- [ ] **Step 3: Exercise rejection and outage paths.** Test a bad signature, modified D1 chunk, incompatible app version, Worker error, invalid publish token, missing chunk, and D1 unavailable response. Confirm no corrupt/unverified app is installed and current installed apps remain usable.
- [ ] **Step 4: Confirm free-tier operation and recovery.** Check Worker/D1 usage and deployment logs in Cloudflare. Verify the service stays on the free plan, document the 500 MB aggregate catalog cap and quota-exhaustion behavior, and test redeploying the previous Worker version and restoring prior app metadata.
- [ ] **Step 5: Update final deployment and contributor instructions.** Record the public `workers.dev` API URL, key rotation steps, app version conventions, and owner-controlled credentials in `app_server/README.md`; keep live secrets out of documentation.

## Acceptance Criteria

- A deployed HTTPS API reads a live catalog from D1 and returns the documented JSON contract; it is not a static-site-only deployment.
- A new approved release submits a signed, multi-file app package to the protected Worker API, which stores it as D1 chunks and updates catalog metadata without manual server filesystem changes or a VPS.
- PyOS can browse the catalog, install a multi-file app package of up to 100 MiB only after confirmation, verify its signature and all package/file digests, and load its entry point and sibling modules on restart.
- Bundled app files cannot be overwritten by the catalog client; uninstall is contained to the user plugin directory.
- API outages, invalid responses, and verification failures are explicit and never cause an unverified app to be installed.
- The first deployment uses only free-tier services unless the owner explicitly approves a paid upgrade; documentation clearly states D1's 500 MB total free database cap and resulting catalog retention constraint.
- Tests cover API contract, publication, signature verification, download failures, installation safety, and plugin discovery.

## Research Sources

Official product documentation reviewed on 2026-10-03:

- [Cloudflare Workers pricing](https://developers.cloudflare.com/workers/platform/pricing/) — free and paid plan details; paid plan base starts at $5/month.
- [Cloudflare Workers limits](https://developers.cloudflare.com/workers/platform/limits/) — free request and CPU limits.
- [Cloudflare D1 pricing](https://developers.cloudflare.com/d1/platform/pricing/) — free read/write/storage allowances and scale-to-zero billing.
- [Cloudflare D1 limits](https://developers.cloudflare.com/d1/platform/limits/) — free database size and maximum row size.
- [Cloudflare Workers configuration](https://developers.cloudflare.com/workers/wrangler/configuration/) — Wrangler and local D1 binding setup.
- [Cloudflare D1 Worker API](https://developers.cloudflare.com/d1/worker-api/) — typed D1 bindings and BLOB handling.
- [Render free instances](https://render.com/docs/free) — free service sleep, ephemeral filesystem, and production warning.
- [Railway pricing plans](https://docs.railway.com/reference/pricing/plans) — Hobby subscription and included usage.

Free quotas, availability, prices, and platform terms can change. Re-check them before deployment; do not treat a free tier as an uptime or $0-cost guarantee beyond its published limits.
