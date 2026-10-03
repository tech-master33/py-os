# PyOS D1 Package Shards Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep production catalog metadata and signed manifests in the existing production D1 database, distribute app-package chunks across seven new D1 databases, and reserve one new D1 database for future PyOS core `.py` component updates.

**Architecture:** The existing production database remains the catalog authority and stores apps, versions, signed manifests, and each version's stable shard index. Seven additional D1 databases store app-package chunks only; an eighth is provisioned and reserved without a schema for future PyOS core `.py` updates. The Worker selects an app shard deterministically when publishing and uses the version's stored shard index when downloading; a chunk-by-chunk authenticated migration moves current production releases before the legacy chunk table is emptied.

**Tech Stack:** TypeScript Cloudflare Workers, Cloudflare D1, Wrangler, Vitest Workers Pool.

## Global Constraints

- Keep app packages on D1; do not use R2 or a VPS.
- Use only the existing Cloudflare Free plan; do not enable paid products or upgrades.
- Preserve the existing staging database and staging Worker.
- Use the existing production D1 database for catalog metadata and signed manifests only after migration.
- Create and bind seven production package databases, and reserve the final free D1 database for later core-component storage.
- Keep production app publishing maintainer-only and require signature and chunk-hash verification.
- Keep each release capped at 100 MiB compressed and 100 MiB extracted.
- A free D1 account has 10 databases and 5 GB total storage; each free database is limited to 500 MB.
- Never log, commit, or send private signing keys or publishing tokens.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `app_server/src/catalog.ts` | Stable app-to-shard selection, version shard metadata, shard-aware reads/writes, and authenticated legacy migration routes. |
| `app_server/src/index.ts` | Export the expanded typed Worker environment bindings. |
| `app_server/migrations/0002_storage_shard.sql` | Add the shard index to release metadata while preserving the existing legacy chunk table during cutover. |
| `app_server/migrations/package_storage/0001_app_chunks.sql` | Create the chunk-only schema for newly provisioned package databases. |
| `app_server/wrangler.jsonc` | Bind the catalog database for local use and the seven package databases only in production; leave staging unchanged. |
| `app_server/wrangler.test.jsonc` | Define isolated local test metadata/package D1 bindings without declaring production resources. |
| `app_server/tests/catalog.test.ts` | Test deterministic shard selection, package writes/reads, metadata isolation, and safe legacy migration. |
| `app_server/tests/setup.ts` | Apply the package-storage migration to all Worker Pool test D1 bindings. |
| `app_server/README.md` | Document free-tier allocation, provisioning, migration, validation, rollback, and storage monitoring. |
| `app_server/scripts/migrate_legacy_chunks.py` | Move legacy production chunks through authenticated bounded Worker requests and verify migration results. |
| `app_server/tests/test_migrate_legacy_chunks.py` | Test the migration client's ordering, retries, errors, and refusal to finalize incomplete versions. |

## Interfaces

- `CatalogEnv` includes `DB: D1Database` and optional `APP_DB_0` through `APP_DB_6` bindings. Production config supplies all seven; staging continues using only its legacy DB.
- `app_versions.storage_shard` is `-1` for legacy chunks still in the metadata DB or `0` through `6` for a package shard. New releases store their shard before receiving chunks.
- `GET /v1/admin/storage-migrations` requires the publish token and lists published versions that still have legacy chunk rows, including `storage_shard` and `legacy_chunk_count`.
- `PUT /v1/admin/storage-migrations/{app_id}/{version}/chunks/{index}` copies one existing chunk from the legacy `DB.app_chunks` table to that version's deterministic package shard after checking its signed manifest digest. It is idempotent for identical bytes.
- `POST /v1/admin/storage-migrations/{app_id}/{version}/finalize` updates the version's `storage_shard` only after the target shard contains all manifest-declared chunks with expected digests. Until finalize succeeds, `storage_shard = -1` and public downloads continue using legacy `DB.app_chunks`.
- `POST /v1/admin/storage-migrations/{app_id}/{version}/cleanup` removes legacy chunks only after migration is finalized and the package shard contains exactly the expected indexes, digests, and chunk sizes; the CLI exposes this only through the explicit `--prune-legacy` option after install verification.
- `PUT /v1/admin/releases/{app_id}/{version}/chunks/{index}` writes new chunks to the package shard; release metadata, signatures, and publication pointers remain in `DB`.
- Public `GET /v1/apps/{app_id}/{version}/chunks/{index}` reads from the stored shard after migration. It reads the legacy table only for versions not yet migrated.
- App shard selection uses stable FNV-1a over UTF-8 app ID bytes modulo seven. Store the chosen integer in `app_versions` on release creation; do not recompute it for existing versions after provisioning.

## Task 1: Add shard-aware Worker storage behavior

**Files:**
- Modify: `app_server/src/catalog.ts`
- Modify: `app_server/src/index.ts`
- Create: `app_server/migrations/0002_storage_shard.sql`
- Create: `app_server/migrations/package_storage/0001_app_chunks.sql`
- Modify: `app_server/wrangler.jsonc`
- Modify: `app_server/tests/catalog.test.ts`
- Modify: `app_server/tests/setup.ts`

**Interfaces:**
- Consumes: existing `CatalogEnv.DB`, release manifest/signature validation, and catalog routes.
- Produces: seven typed optional package D1 bindings, `getShard(env, shardIndex)`, `shardForAppId(appId)`, and database version records containing `storage_shard`.

- [x] **Step 1: Add failing sharding contract tests.** Seed seven isolated D1 test bindings. Test that two app IDs with known FNV-1a results route to expected indexes; a signed release's chunks are written to the chosen package D1 and never to metadata D1; public chunk fetch returns the byte-identical package chunk; and invalid shard indexes fail explicitly.
- [x] **Step 2: Run Worker tests after implementation.** Run `Set-Location app_server; npm test -- --run`. All existing tests and the new shard tests pass.
- [x] **Step 3: Add shard metadata and package-only schema.** Add `storage_shard INTEGER NOT NULL DEFAULT -1 CHECK (storage_shard BETWEEN -1 AND 6)` to `app_versions`. Create the package-only `app_chunks` table with `(app_id, version, chunk_index)` primary key, digest, and BLOB columns; do not create `apps` or `app_versions` in package databases. Configure `APP_DB_0` through `APP_DB_6` for local Worker tests and production only, not staging.
- [x] **Step 4: Route publish and download chunks through their D1 shard.** Implement unsigned 32-bit FNV-1a over UTF-8 app ID bytes, selecting `hash % 7`. Save the shard index in `app_versions` when starting a release. Use that saved index for chunk upload and fetch. Verify chunk size and SHA-256 before inserting, and do not add package BLOB writes to `DB`.
- [x] **Step 5: Run Worker type-check and tests.** Run `Set-Location app_server; npx tsc --noEmit; npm test -- --run`. Expected: all existing tests and new shard-routing tests pass.
- [x] **Step 6: Commit Worker storage routing.** Commit the shared Worker source and migrations together with the migration routes in Task 2.

## Task 2: Add safe migration for existing production chunks

**Files:**
- Modify: `app_server/src/catalog.ts`
- Create: `app_server/scripts/migrate_legacy_chunks.py`
- Create: `app_server/tests/test_migrate_legacy_chunks.py`
- Modify: `app_server/tests/catalog.test.ts`
- Modify: `app_server/README.md`

**Interfaces:**
- Consumes: `storage_shard`, `getShard`, immutable published manifests, and legacy `DB.app_chunks`.
- Produces: the two authenticated migration routes defined above and a Python CLI that visits published versions, uploads one chunk per request, and finalizes only complete verified copies.

- [x] **Step 1: Write failing migration route tests.** Test an authorized copy of an expected chunk, rejection of a wrong chunk digest, rejection of an invalid index, idempotent retry of the same chunk, refusal to finalize with a missing chunk, and switching `storage_shard` only after every target chunk matches the signed manifest.
- [x] **Step 2: Run the targeted Worker test and confirm the migration routes fail.** Run `Set-Location app_server; npm test -- --run`. Expected: new migration route tests fail with 404 until the routes exist.
- [x] **Step 3: Implement authenticated inventory, one-chunk migration, guarded finalize, and cleanup.** Require the existing publish token on all admin routes. Inventory lists published legacy versions only. The chunk route reads a source row from `DB`, validates stored and recomputed digests against `manifest.chunk_hashes[index]`, then insert-or-compares the BLOB in the mapped package D1. Finalize verifies target row count, all indexes, stored digests, and expected sizes before changing `storage_shard` from `-1` to the target index. Cleanup removes legacy rows only for a finalized complete version.
- [x] **Step 4: Implement the resumable migration CLI.** Accept `--base-url`, `--token-env`, `--app-id`/`--version` filters, and an opt-in `--prune-legacy` flag. Fetch the authenticated inventory, migrate/finalize only releases still assigned to legacy storage, and prune already-migrated releases only when requested. Only after a separately verified client install should operators pass `--prune-legacy`; do not accept tokens on the command line, print tokens, or finalize after any failed upload. Make reruns safe.
- [x] **Step 5: Test migration client behavior.** Use mocked HTTP to verify chunk-index order, bounded one-chunk requests, retry after a timeout, explicit non-2xx failure, redirect rejection, and no finalize request when a chunk upload fails.
- [x] **Step 6: Run Python and Worker migration tests.** Run `python -m unittest app_server.tests.test_migrate_legacy_chunks -v` from repository root and `Set-Location app_server; npm test -- --run`.
- [x] **Step 7: Commit migration tooling.** Commit the migration routes, CLI, tests and operations README together with Task 1's shared Worker source.

## Task 3: Provision and configure production package and core databases

**Files:**
- Modify: `app_server/wrangler.jsonc`
- Modify: `app_server/README.md`

**Interfaces:**
- Consumes: `APP_DB_0` through `APP_DB_6`, each with the package-only migration directory.
- Produces: seven package-only production D1 resources, one reserved `pyos-core-components` D1 resource with no schema or Worker binding yet, Wrangler bindings to the seven package database IDs, and repeatable migration/deployment instructions. Keep staging's current database and bindings unchanged.

- [x] **Step 1: Confirm current account resources and limits.** Run `Set-Location app_server; npx wrangler d1 list`. Confirm the existing staging and production databases are the only two in this project allocation and that eight more fit under the current free account limit. Do not create any resource if the account is already at its limit.
- [x] **Step 2: Create seven package D1 databases and reserve one core-components database.** Run `npx wrangler d1 create pyos-app-catalog-apps-0` through `npx wrangler d1 create pyos-app-catalog-apps-6`, then `npx wrangler d1 create pyos-core-components`. Record each returned database ID in local notes rather than chat. Do not create the core database with an app catalog migration directory.
- [x] **Step 3: Bind each package database to production.** Add production-only `APP_DB_0` through `APP_DB_6` entries in `wrangler.jsonc`, matching each binding to its exact database name and ID and setting `migrations_dir` to `migrations/package_storage`. Leave `pyos-core-components` unbound and schema-free until the later core-update design.
- [x] **Step 4: Apply package-only schema to all seven app resources.** For each index `0` through `6`, run `npx wrangler d1 migrations apply pyos-app-catalog-apps-<index> --remote --env production`; verify Wrangler reports only `0001_app_chunks.sql`. Do not apply a migration to `pyos-core-components`.
- [x] **Step 5: Validate resolved deployment bindings without publishing.** Run `npx wrangler deploy --env production --dry-run` and inspect the displayed names to confirm all seven package databases plus metadata DB appear, with no staging or reserved core database bound to production.
- [x] **Step 6: Document actual allocation and hard limits.** Record that the account now uses 10 of 10 free database slots: one staging DB, one production metadata DB, seven production app-package DBs, and one reserved core-components DB. State that the total remains capped at 5 GB and each database at 500 MB.
- [x] **Step 7: Commit production resource configuration.** The package D1 bindings and operations instructions are included with the implementation commit.

## Task 4: Cut production over and verify catalog operations

**Files:**
- Modify: `app_server/README.md`

**Interfaces:**
- Consumes: deployed production Worker with seven package bindings and the authenticated migration CLI.
- Produces: migrated existing releases, production smoke-test evidence, and a documented rollback procedure that does not discard the legacy chunks until the new path is proven.

- [x] **Step 1: Run all local validation before remote deployment.** Run `python -m unittest app_server.tests.test_migrate_legacy_chunks -v`, then `Set-Location app_server; npx tsc --noEmit; npm test -- --run; npx wrangler deploy --env production --dry-run`. Stop on any failure.
- [x] **Step 2: Apply the additive metadata migration.** Run `npx wrangler d1 migrations apply pyos-app-catalog --remote --env production` and verify the `storage_shard` migration completes without deleting legacy chunks.
- [ ] **Step 3: Deploy shard-aware Worker and migrate current versions.** Run `npm run deploy:production`; then load the production publish token from its local secret file into `APP_CATALOG_PUBLISH_TOKEN` and run the migration CLI against `https://pyos-app-catalog.tech-chat.workers.dev`. Do not echo the token or put it in command arguments.
- [ ] **Step 4: Verify migration before any cleanup.** Confirm every published version finalizes, production `/v1/apps`, detail, manifest, and chunk routes return the existing signed release, the client downloads and verifies it, each package D1 contains the corresponding chunks, and production metadata D1 contains no package BLOB rows for migrated versions.
- [ ] **Step 5: Publish and verify a new package version.** Publish a reviewed new sample version through the standard publisher. Confirm its manifest is stored in production `DB`, its chunk rows exist only in the selected package D1, and the production PyOS client installs it successfully.
- [ ] **Step 6: Document rollback and retention.** Keep old production chunk rows until all checks pass. If cutover fails, redeploy the previous Worker version and the unmigrated legacy data remains available. After client-install verification, run the explicit cleanup operation to remove migrated legacy chunk rows; do not drop the legacy table as part of this task.
- [ ] **Step 7: Commit final operations documentation.** Run `git add app_server/README.md; git commit -m "docs: document D1 shard operations"`.

## Acceptance Criteria

- Production catalog metadata, app-version records, signed manifests, and signatures remain in the existing production database.
- Seven package-only D1 databases are provisioned and bound to production, and one schema-free D1 database is reserved for future PyOS core `.py` components; the current staging Worker and database remain unchanged.
- New package publication writes chunks only to the deterministic package shard; manifests and catalog metadata never include package BLOB writes.
- Public downloads read from the stored shard index and return byte-identical verified chunks.
- Existing published versions migrate in idempotent per-chunk operations; missing, invalid, or incomplete copies never switch the public shard pointer.
- A failed rollout can return to the previous Worker while legacy chunks remain available.
- The deployment uses no R2, VPS, paid plan, or paid upgrade.
- Documentation clearly states that using 10 D1 databases does not exceed the 5 GB free account-wide storage cap.
- Local tests prove metadata isolation, shard selection, migration correctness, authorization, and chunk-integrity behavior.

## External Limit Reference

- [Cloudflare D1 limits](https://developers.cloudflare.com/d1/platform/limits/) — current published limits list 10 databases and 5 GB total storage on Free, 500 MB maximum size per Free database, and a 2,000,000-byte maximum row/BLOB size. Recheck these limits before provisioning because service terms can change.
