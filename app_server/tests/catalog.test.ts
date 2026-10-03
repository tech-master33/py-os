import { env } from "cloudflare:workers";
import { beforeAll, beforeEach, describe, expect, it } from "vitest";
import type { AppEnv } from "../src/index";
import worker from "../src/index";

const manifest = {
  app_id: "sample-app",
  version: "1.0.0",
  min_pyos_version: "1.0.0",
  entry_point: "__init__.py",
  files: [
    {
      path: "__init__.py",
      size: 8,
      sha256: "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
    },
  ],
  archive_size: 5,
  expanded_size: 8,
  archive_sha256: "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
  chunk_size: 1_048_576,
  chunk_hashes: ["2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"],
};

async function seedVersion(status: "pending" | "published") {
  await env.DB.prepare(
    `INSERT INTO apps (id, name, description, version, min_pyos_version, published_at)
     VALUES (?, ?, ?, ?, ?, ?)`,
  )
    .bind("sample-app", "Sample App", "A test app", "1.0.0", "1.0.0", "2026-10-03T00:00:00Z")
    .run();

  await env.DB.prepare(
    `INSERT INTO app_versions
       (app_id, version, status, manifest, signature, package_size, expanded_size, chunk_count, created_at, published_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
  )
    .bind(
      "sample-app",
      "1.0.0",
      status,
      JSON.stringify(manifest),
      "test-signature",
      5,
      8,
      1,
      "2026-10-03T00:00:00Z",
      status === "published" ? "2026-10-03T00:00:00Z" : null,
    )
    .run();

  await env.DB.prepare(
    "INSERT INTO app_chunks (app_id, version, chunk_index, sha256, content) VALUES (?, ?, ?, ?, ?)",
  )
    .bind(
      "sample-app",
      "1.0.0",
      0,
      manifest.chunk_hashes[0],
      new TextEncoder().encode("hello"),
    )
    .run();
}

let signingKeyPair: CryptoKeyPair;
let encodedPublicKey = "";

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

function toBase64(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary);
}

function toArrayBuffer(bytes: Uint8Array): ArrayBuffer {
  const result = new ArrayBuffer(bytes.byteLength);
  new Uint8Array(result).set(bytes);
  return result;
}

async function createSignedRelease(
  additionalFilePath?: string,
  version = "2.0.0",
  name = "Sample App",
) {
  const archive = new TextEncoder().encode("hello");
  const digest = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", archive)))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
  const files = [{ path: "__init__.py", size: archive.length, sha256: digest }];
  if (additionalFilePath) {
    files.push({ path: additionalFilePath, size: 0, sha256: digest });
    files.sort((left, right) => (left.path < right.path ? -1 : left.path > right.path ? 1 : 0));
  }
  const releaseManifest = {
    app_id: "sample-app",
    version,
    name,
    description: "A test app",
    min_pyos_version: "1.0.0",
    entry_point: "__init__.py",
    files,
    archive_size: archive.length,
    expanded_size: archive.length,
    archive_sha256: digest,
    chunk_size: 1_048_576,
    chunk_hashes: [digest],
  };
  const manifestBytes = new TextEncoder().encode(canonicalJson(releaseManifest));
  const signature = await crypto.subtle.sign(
    { name: "Ed25519" },
    signingKeyPair.privateKey,
    manifestBytes,
  );

  return {
    archive,
    manifest: releaseManifest,
    signature: toBase64(new Uint8Array(signature)),
  };
}

async function request(
  path: string,
  init?: RequestInit,
  envOverrides: Partial<AppEnv> = {},
) {
  const workerEnv: AppEnv = {
    DB: env.DB,
    PUBLISH_TOKEN: "test-token",
    PUBLISH_PUBLIC_KEY: encodedPublicKey,
    ...envOverrides,
  };
  return worker.fetch(new Request(`https://catalog.test${path}`, init), workerEnv);
}

describe("catalog Worker", () => {
  beforeAll(async () => {
    signingKeyPair = (await crypto.subtle.generateKey(
      { name: "Ed25519" },
      true,
      ["sign", "verify"],
    )) as CryptoKeyPair;
    const exported = await crypto.subtle.exportKey("raw", signingKeyPair.publicKey);
    if (!(exported instanceof ArrayBuffer)) {
      throw new Error("Unexpected Ed25519 public key format");
    }
    encodedPublicKey = toBase64(new Uint8Array(exported));
  });

  beforeEach(async () => {
    await env.DB.exec(
      "DELETE FROM app_chunks; DELETE FROM app_versions; DELETE FROM apps;",
    );
  });

  it("returns the health response as JSON", async () => {
    const response = await request("/v1/health");

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ status: "ok" });
    expect(response.headers.get("content-type")).toContain("application/json");
  });

  it("lists only published apps with the stable catalog shape", async () => {
    await seedVersion("published");

    const response = await request("/v1/apps");

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      apps: [
        {
          id: "sample-app",
          name: "Sample App",
          description: "A test app",
          version: "1.0.0",
          min_pyos_version: "1.0.0",
          package_size: 5,
          chunk_size: 1_048_576,
          chunk_count: 1,
        },
      ],
    });
  });

  it("does not expose apps or chunks from pending releases", async () => {
    await seedVersion("pending");

    const listResponse = await request("/v1/apps");
    const chunkResponse = await request("/v1/apps/sample-app/1.0.0/chunks/0");

    expect(await listResponse.json()).toEqual({ apps: [] });
    expect(chunkResponse.status).toBe(404);
  });

  it("returns a published manifest and its exact binary chunk", async () => {
    await seedVersion("published");

    const manifestResponse = await request("/v1/apps/sample-app/1.0.0/manifest");
    const chunkResponse = await request("/v1/apps/sample-app/1.0.0/chunks/0");

    expect(manifestResponse.status).toBe(200);
    expect(await manifestResponse.json()).toEqual({ manifest, signature: "test-signature" });
    expect(chunkResponse.status).toBe(200);
    expect(new Uint8Array(await chunkResponse.arrayBuffer())).toEqual(
      new TextEncoder().encode("hello"),
    );
    expect(chunkResponse.headers.get("content-type")).toBe("application/octet-stream");
  });

  it("rejects malformed app IDs and unknown routes", async () => {
    const malformed = await request("/v1/apps/Bad_ID");
    const unknown = await request("/v1/not-a-route");

    expect(malformed.status).toBe(400);
    expect(await malformed.json()).toEqual({ error: "invalid_app_id" });
    expect(unknown.status).toBe(404);
    expect(await unknown.json()).toEqual({ error: "not_found" });
  });

  it("returns a JSON 404 for unknown apps and versions", async () => {
    const unknownApp = await request("/v1/apps/missing-app");
    const unknownVersion = await request("/v1/apps/sample-app/9.0.0/manifest");

    expect(unknownApp.status).toBe(404);
    expect(await unknownApp.json()).toEqual({ error: "not_found" });
    expect(unknownVersion.status).toBe(404);
    expect(await unknownVersion.json()).toEqual({ error: "not_found" });
  });

  it("rejects release publishing without the maintainer token", async () => {
    const release = await createSignedRelease();
    const response = await request(
      "/v1/admin/releases",
      {
        method: "POST",
        headers: { "content-type": "application/json", authorization: "Bearer wrong-token" },
        body: JSON.stringify({ manifest: release.manifest, signature: release.signature }),
      },
    );

    expect(response.status).toBe(401);
    expect(await response.json()).toEqual({ error: "unauthorized" });
    const row = await env.DB.prepare("SELECT COUNT(*) AS count FROM app_versions").first<{
      count: number;
    }>();
    expect(row?.count).toBe(0);
  });

  it("rejects unsafe manifest file paths before creating a release", async () => {
    const release = await createSignedRelease("bad:name.py");
    const response = await request(
      "/v1/admin/releases",
      {
        method: "POST",
        headers: {
          "content-type": "application/json",
          authorization: "Bearer test-token",
        },
        body: JSON.stringify({ manifest: release.manifest, signature: release.signature }),
      },
    );

    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({ error: "invalid_manifest" });
    const row = await env.DB.prepare("SELECT COUNT(*) AS count FROM app_versions").first<{
      count: number;
    }>();
    expect(row?.count).toBe(0);
  });

  it("stores a signed release only after every verified chunk is uploaded", async () => {
    const release = await createSignedRelease();
    const headers = {
      authorization: "Bearer test-token",
      "content-type": "application/json",
    };
    const startResponse = await request("/v1/admin/releases", {
      method: "POST",
      headers,
      body: JSON.stringify({ manifest: release.manifest, signature: release.signature }),
    });

    expect(startResponse.status).toBe(201);
    expect(await startResponse.json()).toEqual({ status: "pending", chunk_count: 1 });
    const idempotentStart = await request("/v1/admin/releases", {
      method: "POST",
      headers,
      body: JSON.stringify({ manifest: release.manifest, signature: release.signature }),
    });
    expect(idempotentStart.status).toBe(200);

    const conflictingRelease = await createSignedRelease(undefined, "2.0.0", "Different name");
    const conflictResponse = await request("/v1/admin/releases", {
      method: "POST",
      headers,
      body: JSON.stringify({
        manifest: conflictingRelease.manifest,
        signature: conflictingRelease.signature,
      }),
    });
    expect(conflictResponse.status).toBe(409);

    const pendingList = await request("/v1/apps");
    expect(await pendingList.json()).toEqual({ apps: [] });

    const badChunk = await request(
      "/v1/admin/releases/sample-app/2.0.0/chunks/0",
      {
        method: "PUT",
        headers: { authorization: "Bearer test-token" },
        body: toArrayBuffer(new TextEncoder().encode("hallo")),
      },
    );
    expect(badChunk.status).toBe(400);
    expect(await badChunk.json()).toEqual({ error: "chunk_digest_mismatch" });

    const missingChunkPublish = await request(
      "/v1/admin/releases/sample-app/2.0.0/publish",
      { method: "POST", headers: { authorization: "Bearer test-token" } },
    );
    expect(missingChunkPublish.status).toBe(409);
    expect(await missingChunkPublish.json()).toEqual({ error: "release_incomplete" });

    const oversizedChunk = await request(
      "/v1/admin/releases/sample-app/2.0.0/chunks/0",
      {
        method: "PUT",
        headers: { authorization: "Bearer test-token" },
        body: new Uint8Array(1_048_577),
      },
    );
    expect(oversizedChunk.status).toBe(413);

    const chunkResponse = await request(
      "/v1/admin/releases/sample-app/2.0.0/chunks/0",
      {
        method: "PUT",
        headers: { authorization: "Bearer test-token" },
        body: toArrayBuffer(release.archive),
      },
    );
    expect(chunkResponse.status).toBe(204);
    const duplicateChunk = await request(
      "/v1/admin/releases/sample-app/2.0.0/chunks/0",
      {
        method: "PUT",
        headers: { authorization: "Bearer test-token" },
        body: toArrayBuffer(release.archive),
      },
    );
    expect(duplicateChunk.status).toBe(204);

    const publishResponse = await request(
      "/v1/admin/releases/sample-app/2.0.0/publish",
      { method: "POST", headers: { authorization: "Bearer test-token" } },
    );
    expect(publishResponse.status).toBe(200);
    expect(await publishResponse.json()).toEqual({ status: "published" });
    const immutableChunk = await request(
      "/v1/admin/releases/sample-app/2.0.0/chunks/0",
      {
        method: "PUT",
        headers: { authorization: "Bearer test-token" },
        body: toArrayBuffer(release.archive),
      },
    );
    expect(immutableChunk.status).toBe(409);

    const catalogResponse = await request("/v1/apps");
    expect(await catalogResponse.json()).toEqual({
      apps: [
        {
          id: "sample-app",
          name: "Sample App",
          description: "A test app",
          version: "2.0.0",
          min_pyos_version: "1.0.0",
          package_size: release.archive.length,
          chunk_size: 1_048_576,
          chunk_count: 1,
        },
      ],
    });
  });

  it("rejects a modified manifest signature before creating a release", async () => {
    const release = await createSignedRelease();
    const response = await request("/v1/admin/releases", {
      method: "POST",
      headers: {
        authorization: "Bearer test-token",
        "content-type": "application/json",
      },
      body: JSON.stringify({
        manifest: { ...release.manifest, description: "tampered" },
        signature: release.signature,
      }),
    });

    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({ error: "invalid_signature" });
  });
});
