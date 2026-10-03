import { env } from "cloudflare:workers";
import { beforeEach, describe, expect, it } from "vitest";
import type { AppEnv } from "../src/index";
import worker from "../src/index";

const workerEnv = env as AppEnv;

function toHex(bytes: Uint8Array): string {
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function sha256(bytes: Uint8Array): Promise<string> {
  return toHex(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)));
}

async function request(path: string, init?: RequestInit, overrides: Partial<AppEnv> = {}) {
  return worker.fetch(
    new Request(`https://catalog.test${path}`, init),
    {
      ...workerEnv,
      PUBLISH_TOKEN: "test-token",
      PUBLISH_PUBLIC_KEY: "",
      ...overrides,
    },
  );
}

async function createTestDrive(name = "Test drive") {
  const response = await request("/v1/drives", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ name, quota_bytes: 1_048_576 }),
  });
  if (!response.ok) {
    throw new Error(`Unable to create test drive: HTTP ${response.status}`);
  }
  return response.json() as Promise<{ id: string; quota_bytes: number; used_bytes: number }>;
}

async function uploadFile(driveId: string, path: string, bytes: Uint8Array) {
  const digest = await sha256(bytes);
  const start = await request(`/v1/drives/${driveId}/uploads`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ path, size_bytes: bytes.byteLength, sha256: digest }),
  });
  expect(start.status).toBe(201);
  const session = await start.json() as { upload_id: string; chunk_count: number };
  expect(session.chunk_count).toBe(1);

  const chunk = await request(
    `/v1/drives/${driveId}/uploads/${session.upload_id}/0`,
    { method: "PUT", headers: { "x-chunk-sha256": digest }, body: bytes },
  );
  expect(chunk.status).toBe(200);
  const complete = await request(
    `/v1/drives/${driveId}/uploads/${session.upload_id}/complete`,
    { method: "POST" },
  );
  expect(complete.status).toBe(200);
  return complete;
}

async function clearDriveData() {
  await env.DB.batch([
    env.DB.prepare("DELETE FROM drive_upload_chunks"),
    env.DB.prepare("DELETE FROM drive_uploads"),
    env.DB.prepare("DELETE FROM drive_garbage"),
    env.DB.prepare("DELETE FROM drive_files"),
    env.DB.prepare("DELETE FROM drive_directories"),
    env.DB.prepare("DELETE FROM shared_drives"),
    env.DB.prepare(
      "UPDATE drive_capacity SET max_bytes = 1000000000, allocated_bytes = 0 WHERE id = 1",
    ),
  ]);
  const listing = await workerEnv.DRIVE_FILES!.list({ prefix: "drive:" });
  await Promise.all(listing.keys.map(({ name }) => workerEnv.DRIVE_FILES!.delete(name)));
}

describe("shared-drive schema", () => {
  beforeEach(clearDriveData);

  it("initializes the shared-drive quota ledger at the free storage ceiling", async () => {
    const row = await env.DB.prepare(
      "SELECT max_bytes, allocated_bytes FROM drive_capacity WHERE id = 1",
    ).first<{ max_bytes: number; allocated_bytes: number }>();

    expect(row).toEqual({
      max_bytes: 1_000_000_000,
      allocated_bytes: 0,
    });
  });

  it("creates a high-entropy drive and lists its public metadata", async () => {
    const response = await request("/v1/drives", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ name: "School files", quota_bytes: 1_048_576 }),
    });
    expect(response.status).toBe(201);

    const created = await response.json() as {
      id: string;
      name: string;
      quota_bytes: number;
      used_bytes: number;
    };
    expect(created).toEqual({
      id: expect.stringMatching(/^[A-Za-z0-9_-]{32}$/),
      name: "School files",
      quota_bytes: 1_048_576,
      used_bytes: 0,
    });

    const summary = await request(`/v1/drives/${created.id}`);
    expect(summary.status).toBe(200);
    expect(await summary.json()).toEqual(created);
  });

  it("lists immediate children and rejects paths that escape the drive", async () => {
    const createdResponse = await request("/v1/drives", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ name: "Shared", quota_bytes: 1_048_576 }),
    });
    const created = await createdResponse.json() as { id: string };

    const directory = await request(`/v1/drives/${created.id}/directories`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ path: "notes" }),
    });
    expect(directory.status).toBe(201);

    const listing = await request(`/v1/drives/${created.id}/entries?path=`);
    expect(listing.status).toBe(200);
    expect(await listing.json()).toEqual({
      entries: [{ name: "notes", type: "directory", size_bytes: 0, updated_at: expect.any(String) }],
    });

    const traversal = await request(`/v1/drives/${created.id}/directories`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ path: "../outside" }),
    });
    expect(traversal.status).toBe(400);
  });

  it("does not allocate more drive quota than the configured free budget", async () => {
    const first = await request("/v1/drives", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ name: "First", quota_bytes: 1_048_576 }),
    }, { DRIVE_STORAGE_LIMIT_BYTES: "1048576" });
    expect(first.status).toBe(201);

    const second = await request("/v1/drives", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ name: "Second", quota_bytes: 1_048_576 }),
    }, { DRIVE_STORAGE_LIMIT_BYTES: "1048576" });
    expect(second.status).toBe(409);
    expect(await second.json()).toEqual({ error: "drive_quota_exhausted" });
  });

  it("releases quota when deleting an empty drive", async () => {
    const createdResponse = await request("/v1/drives", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ name: "Temporary", quota_bytes: 1_048_576 }),
    });
    const created = await createdResponse.json() as { id: string };

    const deleted = await request(`/v1/drives/${created.id}`, { method: "DELETE" });
    expect(deleted.status).toBe(200);
    const capacity = await env.DB.prepare(
      "SELECT allocated_bytes FROM drive_capacity WHERE id = 1",
    ).first<{ allocated_bytes: number }>();
    expect(capacity?.allocated_bytes).toBe(0);
  });

  it("requires a configured KV binding for drive requests", async () => {
    const response = await request(
      "/v1/drives",
      { method: "POST", body: JSON.stringify({ name: "Unavailable", quota_bytes: 1_048_576 }) },
      { DRIVE_FILES: undefined },
    );
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({ error: "drive_storage_unavailable" });
  });

  it("round-trips binary file contents and reports drive quota usage", async () => {
    const drive = await createTestDrive();
    const bytes = new Uint8Array([0, 1, 2, 255, 10]);
    await uploadFile(drive.id, "folderless.bin", bytes);

    const response = await request(`/v1/drives/${drive.id}/files?path=folderless.bin`);
    expect(response.status).toBe(200);
    expect(response.headers.get("content-length")).toBe(String(bytes.byteLength));
    expect(response.headers.get("x-file-size-bytes")).toBe(String(bytes.byteLength));
    expect(response.headers.get("x-file-sha256")).toBe(await sha256(bytes));
    expect(new Uint8Array(await response.arrayBuffer())).toEqual(bytes);

    const summary = await request(`/v1/drives/${drive.id}`);
    expect(await summary.json()).toMatchObject({ used_bytes: bytes.byteLength });
  });

  it("keeps an existing file intact when an upload chunk digest is wrong", async () => {
    const drive = await createTestDrive();
    const prior = new Uint8Array([1, 2, 3]);
    await uploadFile(drive.id, "notes.bin", prior);

    const next = new Uint8Array([4, 5, 6]);
    const start = await request(`/v1/drives/${drive.id}/uploads`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        path: "notes.bin",
        size_bytes: next.byteLength,
        sha256: await sha256(next),
      }),
    });
    const upload = await start.json() as { upload_id: string };
    const rejected = await request(`/v1/drives/${drive.id}/uploads/${upload.upload_id}/0`, {
      method: "PUT",
      headers: { "x-chunk-sha256": await sha256(prior) },
      body: next,
    });
    expect(rejected.status).toBe(422);

    const previous = await request(`/v1/drives/${drive.id}/files?path=notes.bin`);
    expect(new Uint8Array(await previous.arrayBuffer())).toEqual(prior);
  });

  it("overwrites files without leaking old chunks or quota", async () => {
    const drive = await createTestDrive();
    const firstBytes = new Uint8Array([1, 2, 3]);
    const secondBytes = new Uint8Array([4, 5, 6, 7]);
    await uploadFile(drive.id, "notes.bin", firstBytes);
    const overwrite = await uploadFile(drive.id, "notes.bin", secondBytes);
    expect(await overwrite.json()).toMatchObject({ cleanup_pending: false });

    const summary = await request(`/v1/drives/${drive.id}`);
    expect(await summary.json()).toMatchObject({ used_bytes: secondBytes.byteLength });
    const response = await request(`/v1/drives/${drive.id}/files?path=notes.bin`);
    expect(new Uint8Array(await response.arrayBuffer())).toEqual(secondBytes);
    const listing = await workerEnv.DRIVE_FILES!.list({ prefix: "drive:" });
    expect(listing.keys).toHaveLength(1);
  });

  it("deletes files and releases their used quota", async () => {
    const drive = await createTestDrive();
    const bytes = new Uint8Array([9, 8, 7]);
    await uploadFile(drive.id, "notes.bin", bytes);

    const deleted = await request(
      `/v1/drives/${drive.id}/entries?path=notes.bin`,
      { method: "DELETE" },
    );
    expect(deleted.status).toBe(200);
    expect(await deleted.json()).toMatchObject({ status: "deleted", cleanup_pending: false });
    const summary = await request(`/v1/drives/${drive.id}`);
    expect(await summary.json()).toMatchObject({ used_bytes: 0 });
  });

  it("rejects deleting a drive that still contains entries", async () => {
    const drive = await createTestDrive();
    await request(`/v1/drives/${drive.id}/directories`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ path: "folder" }),
    });

    const deleted = await request(`/v1/drives/${drive.id}`, { method: "DELETE" });
    expect(deleted.status).toBe(409);
    expect(await deleted.json()).toEqual({ error: "drive_not_empty" });
  });

  it("round-trips an empty file through one zero-byte chunk", async () => {
    const drive = await createTestDrive();
    await uploadFile(drive.id, "empty.bin", new Uint8Array());
    const downloaded = await request(`/v1/drives/${drive.id}/files?path=empty.bin`);
    expect(downloaded.status).toBe(200);
    expect(new Uint8Array(await downloaded.arrayBuffer())).toHaveLength(0);
  });
});
