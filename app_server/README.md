# PyOS App Catalog Server

This folder contains the dynamic Cloudflare Worker API and D1 migration for the PyOS app
catalog. The Worker serves live catalog metadata and signed app-package chunks; it never
executes app code. Packages remain maintainer-published and each release is limited to
100 MiB compressed and 100 MiB extracted.

## Current deployment status

Staging is deployed at `https://pyos-app-catalog-staging.tech-chat.workers.dev`, and
production is deployed at `https://pyos-app-catalog.tech-chat.workers.dev`. Each has its
own D1 database, publish token, and Ed25519 signing key. The reviewed `welcome` 1.0.0
package is published in both catalogs. PyOS is configured to use the production URL and
public verification key; private signing keys and publish tokens are stored outside the
repository under `%APPDATA%\\PyOS` on the maintainer machine. Keep those files backed up
securely; never commit or send them.
Production `welcome` 1.0.0 has been verified through the signed client after migration;
its package chunk is in shard 0 and its duplicate legacy chunk has been removed.

## Production D1 allocation

The production storage layout uses the existing `pyos-app-catalog` D1 for catalog
records, signed manifests, release pointers, and publishing metadata. Package ZIP chunks
are sharded across seven dedicated app-package D1 databases. The eighth additional D1 is
reserved, schema-free and not bound to the catalog Worker, for a future PyOS core
component-update system that may distribute individual `.py` files. Staging continues
to use only `pyos-app-catalog-staging`.

The layout uses all ten Free D1 database slots: one staging database, one production
metadata database, seven app-package databases, and one reserved core-components
database. This does **not** raise the Free account's aggregate 5 GB storage allowance;
each database is also limited to 500 MB. Capacity must be monitored across the account,
not treated as eight extra 500 MB allowances. Do not bind the reserved core database to
the app catalog or create its schema until the core-update feature is designed.

App chunks use stable FNV-1a assignment based on app ID across seven package databases.
The release's assigned shard number is persisted with its signed manifest and reused for
every chunk upload and download. Existing production releases remain readable from the
legacy metadata database until the authenticated migration has copied and verified their
chunks. Run the migration tool without `--prune-legacy` first; only use that flag after
the production client has successfully downloaded and installed each migrated release.
The cleanup route refuses to delete legacy chunks until the version points at its shard
and every expected chunk index, digest, and size is present there.

Before provisioning, confirm the account has room for eight additional databases:

```powershell
npx wrangler d1 list
npx wrangler d1 create pyos-app-catalog-apps-0
npx wrangler d1 create pyos-app-catalog-apps-1
npx wrangler d1 create pyos-app-catalog-apps-2
npx wrangler d1 create pyos-app-catalog-apps-3
npx wrangler d1 create pyos-app-catalog-apps-4
npx wrangler d1 create pyos-app-catalog-apps-5
npx wrangler d1 create pyos-app-catalog-apps-6
npx wrangler d1 create pyos-core-components
```

Use the returned IDs only in the production bindings in `wrangler.jsonc`; do not add
package shards to staging. Apply `migrations/package_storage` to each app-package D1,
but leave `pyos-core-components` empty. The metadata D1 uses `migrations`, including
`0002_storage_shard.sql`. Keep all old chunk rows until the sharded Worker, signed
download, and install flow have passed production checks.

To migrate existing releases, load the production publish token from its local file into
the process environment without printing it:

```powershell
$secretDir = Join-Path $env:APPDATA 'PyOS'
$env:APP_CATALOG_PUBLISH_TOKEN = (
  Get-Content -Raw (Join-Path $secretDir 'app-catalog-production.token')
).Trim()
python -m app_server.scripts.migrate_legacy_chunks `
  --base-url https://pyos-app-catalog.tech-chat.workers.dev
```

The command copies and finalizes all published releases still in legacy storage, but
retains their old copies. After successful client verification, rerun it with
`--prune-legacy` to delete the migrated package BLOB rows from the metadata database.
This cleanup is per release; it does not drop the legacy table.

## Local development

Requirements: Node.js 22 or newer and npm. From the repository root:

```powershell
npm --prefix app_server install --legacy-peer-deps
npm --prefix app_server test -- --run
Set-Location app_server
npx tsc --noEmit
npm run dev
```

Local Worker state uses Wrangler's local D1 emulator. Check `http://localhost:8787/v1/health`,
`/v1/apps`, and a missing app route. Tests create and migrate their own isolated local D1
database and do not need a Cloudflare account or API credentials.
The Cloudflare setup and deployment commands below assume the current directory is
`app_server`; the publisher command switches back to the repository root.

## Cloudflare account and free-tier setup

An authorized owner must create or sign in to a Cloudflare account, enable a `workers.dev`
subdomain, create the staging and production D1 databases, and authorize Wrangler. This
deployment requires the account owner's consent and account access; no account, domain, or
VPS is provisioned by this repository. If Cloudflare requires a payment card or offers a
paid upgrade during signup, stop and do not select or enable a paid product.

Use the free Workers and D1 plans only. Cloudflare's published free D1 limit is 500 MB per
database, shared by the entire catalog; four 100 MiB releases will nearly fill it after
metadata and row overhead. A 100 MiB download is approximately 100 Worker requests. Quota
exhaustion can make the catalog unavailable until the quota resets or the owner explicitly
approves another plan or storage design. Do not enable automatic paid upgrades.
At the time this plan was prepared, Cloudflare also listed 100,000 Worker requests/day,
5 million D1 rows read/day, and 100,000 D1 rows written/day on the free plans. Re-check the
current limits before deployment; each package download adds roughly 100 D1 chunk reads
and Worker requests per 100 MiB, in addition to catalog and manifest reads.

1. Authenticate using `npx wrangler login`, or use a scoped `CLOUDFLARE_API_TOKEN` in the
   current shell. Grant only the account permissions needed
   to edit Workers and D1 and read the account's resource list. Do not paste credentials
   into chat or commit them.
2. Create the two databases:

   ```powershell
   npx wrangler d1 create pyos-app-catalog-staging
   npx wrangler d1 create pyos-app-catalog
   ```

   Copy each returned database ID into the matching `env.staging` or `env.production`
   `d1_databases` entry in `wrangler.jsonc`. Do not use the local placeholder IDs for a
   remote deployment.
3. Generate an Ed25519 key pair on the maintainer's computer. Install the repository's
   Python requirements first. This command prints a raw 32-byte private key and its raw
   public key as base64; keep the private output in a password manager or local secret
   manager, and never share or commit it:

   ```powershell
   python -c "import base64; from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey; from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, PublicFormat, NoEncryption; k=Ed25519PrivateKey.generate(); print('APP_CATALOG_SIGNING_KEY='+base64.b64encode(k.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())).decode()); print('PUBLISH_PUBLIC_KEY='+base64.b64encode(k.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode())"
   ```

   Put the public key value in both `PUBLISH_PUBLIC_KEY` environment variables in
   `wrangler.jsonc`. The public key is not secret. The private key is never sent to
   Cloudflare.
4. Generate a different random publish token, store it in the maintainer's secret manager,
   then set it as a Worker secret in each environment. Wrangler prompts for the secret;
   type it at the prompt rather than adding it to command history. Generate a random
   value locally with `python -c "import secrets; print(secrets.token_urlsafe(32))"`:

   ```powershell
   npx wrangler secret put PUBLISH_TOKEN --env staging
   npx wrangler secret put PUBLISH_TOKEN --env production
   ```

5. Apply D1 migrations and deploy staging first:

   ```powershell
   npx wrangler d1 migrations apply pyos-app-catalog-staging --remote --env staging
   npm run deploy:staging
   ```

   After staging validation, apply the migration to production and deploy only with owner
   approval:

   ```powershell
   npx wrangler d1 migrations apply pyos-app-catalog --remote --env production
   npm run deploy:production
   ```

Wrangler authentication is separate from the app-publishing token. Neither the account API
token nor the publish token belongs in `wrangler.jsonc`. `PUBLISH_PUBLIC_KEY` is a public
verification key, not a secret.

## Configure PyOS and publish an approved app

After staging deploys, Cloudflare reports the Worker URL under its account's
`workers.dev` subdomain. Set `CATALOG_API_URL` and `CATALOG_PUBLIC_KEY` in
`apps/app_catalog.py` to that HTTPS origin and the matching raw public key. The production
URL and key must be configured before a production PyOS release; never ship the staging
endpoint as the production catalog. The client refuses plain HTTP and verifies the pinned
public key before downloading chunks.

On the maintainer computer, load the production publisher credentials from the local
`%APPDATA%\\PyOS` files into the current PowerShell process. The private key is stored as
raw 32-byte Ed25519 data; base64-encode it for the publisher without printing it:

```powershell
$secretDir = Join-Path $env:APPDATA 'PyOS'
$env:APP_CATALOG_API_URL = 'https://pyos-app-catalog.tech-chat.workers.dev'
$env:APP_CATALOG_SIGNING_KEY = [Convert]::ToBase64String(
  [IO.File]::ReadAllBytes((Join-Path $secretDir 'app-catalog-production.key'))
)
$env:APP_CATALOG_PUBLISH_TOKEN = (
  Get-Content -Raw (Join-Path $secretDir 'app-catalog-production.token')
).Trim()
```

Review the app source and its entry in `app_catalog/registry.json`, including its version,
minimum PyOS version, package path, and release notes. Then publish:

```powershell
Set-Location ..
python -m app_catalog.publish publish --app-id welcome --version 1.0.0
```

The version passed on the command line must match the reviewed registry entry. The publisher
validates the package, builds a deterministic ZIP, signs its canonical manifest locally,
and sends the manifest and chunks over HTTPS. It sends neither the private signing key nor
the app-publishing token in a URL. Releases are immutable: publish changed content under a
new version, not an existing version.

Do not publish packages that require dependencies PyOS does not already ship. The publisher
rejects symlinks, unsafe paths, non-regular files, packages over 100 MiB compressed or
expanded, and manifests over 1 MiB. It excludes Python bytecode and `__pycache__`.

## Staging checks

Before production, use a staging-only test app containing `__init__.py`, an imported
sibling module, and a data file. Verify the public health/list/manifest/chunk routes, install
the package into a clean temporary `PY_OS_DATA_DIR`, launch it, update it under a new
version, and uninstall it. Also verify bad signatures, modified chunks, missing chunks,
invalid publish tokens, and incompatible minimum PyOS versions fail without replacing an
installed package.

Keep staging and production databases separate. Remove staging test rows after verification
using the D1 dashboard or a reviewed `wrangler d1 execute` command that targets the staging
database only. Never copy a test signing key or test package into production.

## Storage, rollback, and key rotation

D1 stores one 1 MiB BLOB per row, plus signed manifests and catalog metadata. There is no
scheduled retention policy: cleanup is an explicit, authenticated per-release operation.
The legacy-migration cleanup route verifies the finalized shard copy before removing
duplicate legacy chunks. For removing old releases, export the database first; only delete
non-current versions, and delete their chunk rows before the version row. Never remove the
version referenced by the current `apps` row. Public downloads are read-only.

Worker code rollback and D1 data recovery are separate operations. Use Cloudflare's Worker
version history to roll back the Worker code; a rollback does not undo D1 migrations or
release changes. Export D1 before schema or retention changes and verify a recovery path
before production updates. Do not delete or rewrite a published release to roll back an
app; the current catalog pointer can be set to a previously published version only through
a reviewed database operation.

Rotate the publish token with `wrangler secret put PUBLISH_TOKEN --env <environment>` and
update the maintainer's local `APP_CATALOG_PUBLISH_TOKEN`. For signing-key rotation, deploy
the new public key to the Worker and a PyOS release before signing releases with the new
private key. Existing releases signed with the old key will no longer verify unless the
client and Worker are explicitly extended to support a transition period; plan and test
that change before rotating.

Cloudflare free-tier limits and pricing can change. Re-check them in the Cloudflare
dashboard and official pricing/limits documentation before deploying. Never upgrade to a
paid plan without the owner's explicit approval.
