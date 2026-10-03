import base64
import hashlib
import json
import random
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path

import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app_catalog.publish import build_release
from app_catalog_client import (
    CatalogClient,
    CatalogCompatibilityError,
    CatalogNetworkError,
    CatalogResponseError,
    CatalogVerificationError,
)

CHUNK_SIZE = 1_048_576


def canonical_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def make_release(key, *, files=None, app_id="welcome", version="1.2.0", min_version="1.0.0"):
    file_contents = files or {
        "__init__.py": b"from .greeting import greeting\n",
        "greeting.py": b"greeting = 'hello'\n",
        "data/welcome.txt": b"Welcome!\n",
    }
    archive_stream = BytesIO()
    with zipfile.ZipFile(archive_stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for filename, content in file_contents.items():
            archive.writestr(filename, content)
    archive_bytes = archive_stream.getvalue()
    chunks = [
        archive_bytes[offset : offset + CHUNK_SIZE]
        for offset in range(0, len(archive_bytes), CHUNK_SIZE)
    ]
    manifest = {
        "app_id": app_id,
        "version": version,
        "name": "Welcome",
        "description": "A test app.",
        "min_pyos_version": min_version,
        "entry_point": "__init__.py",
        "files": [
            {
                "path": filename,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for filename, content in sorted(file_contents.items())
        ],
        "archive_size": len(archive_bytes),
        "expanded_size": sum(map(len, file_contents.values())),
        "archive_sha256": hashlib.sha256(archive_bytes).hexdigest(),
        "chunk_size": CHUNK_SIZE,
        "chunk_hashes": [hashlib.sha256(chunk).hexdigest() for chunk in chunks],
    }
    manifest_bytes = canonical_json(manifest)
    signature = base64.b64encode(key.sign(manifest_bytes)).decode("ascii")
    record = {
        "id": app_id,
        "name": "Welcome",
        "description": "A test app.",
        "version": version,
        "min_pyos_version": min_version,
        "package_size": len(archive_bytes),
        "chunk_size": CHUNK_SIZE,
        "chunk_count": len(chunks),
    }
    return record, manifest, signature, chunks


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self.body = body

    def iter_content(self, chunk_size):
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset : offset + chunk_size]


class FakeSession:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url not in self.responses:
            raise AssertionError(f"Unexpected URL: {url}")
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response


class CatalogClientTests(unittest.TestCase):
    def setUp(self):
        self.key = Ed25519PrivateKey.generate()
        public_key = self.key.public_key().public_bytes_raw()
        self.encoded_key = base64.b64encode(public_key).decode("ascii")

    def make_client(self, record, manifest, signature, chunks, pyos_version="1.0.0"):
        base = "https://catalog.example"
        responses = {
            f"{base}/v1/apps": FakeResponse(
                200,
                json.dumps({"apps": [record]}).encode("utf-8"),
            ),
            f"{base}/v1/apps/{record['id']}": FakeResponse(
                200,
                json.dumps(record).encode("utf-8"),
            ),
            f"{base}/v1/apps/{record['id']}/{record['version']}/manifest": FakeResponse(
                200,
                json.dumps({"manifest": manifest, "signature": signature}).encode("utf-8"),
            ),
        }
        for index, chunk in enumerate(chunks):
            responses[
                f"{base}/v1/apps/{record['id']}/{record['version']}/chunks/{index}"
            ] = FakeResponse(200, chunk)
        session = FakeSession(responses)
        return CatalogClient(
            base,
            self.encoded_key,
            session=session,
            pyos_version=pyos_version,
        ), session

    def test_lists_valid_catalog_records(self):
        record, manifest, signature, chunks = make_release(self.key)
        client, _ = self.make_client(record, manifest, signature, chunks)

        self.assertEqual(client.list_apps(), [record])

    def test_verifies_publisher_manifest_and_installs_its_archive(self):
        with tempfile.TemporaryDirectory() as source:
            package = Path(source) / "welcome"
            package.mkdir()
            (package / "__init__.py").write_text("from .helper import value\n", encoding="utf-8")
            (package / "helper.py").write_text("value = 42\n", encoding="utf-8")
            release = build_release(
                package,
                app_id="welcome",
                version="1.2.0",
                name="Welcome",
                description="A test app.",
                min_pyos_version="1.0.0",
                signing_key=self.key,
            )
        record = {
            "id": "welcome",
            "name": "Welcome",
            "description": "A test app.",
            "version": "1.2.0",
            "min_pyos_version": "1.0.0",
            "package_size": release.manifest["archive_size"],
            "chunk_size": CHUNK_SIZE,
            "chunk_count": len(release.chunks),
        }
        client, _ = self.make_client(
            record,
            release.manifest,
            release.signature,
            release.chunks,
        )

        with tempfile.TemporaryDirectory() as directory:
            installed = client.download_verified(record, directory)

            self.assertEqual((installed / "helper.py").read_text(encoding="utf-8"), "value = 42\n")

    def test_downloads_and_installs_verified_multifile_package(self):
        record, manifest, signature, chunks = make_release(self.key)
        client, session = self.make_client(record, manifest, signature, chunks)
        with tempfile.TemporaryDirectory() as directory:
            installed = client.download_verified(record, directory)

            self.assertEqual(installed, Path(directory) / "welcome")
            self.assertEqual(
                {path.relative_to(installed).as_posix() for path in installed.rglob("*") if path.is_file()},
                {item["path"] for item in manifest["files"]} | {".pyos-catalog.json"},
            )
            self.assertEqual(
                (installed / "greeting.py").read_bytes(),
                b"greeting = 'hello'\n",
            )
            self.assertTrue(all(call[1]["allow_redirects"] is False for call in session.calls))

    def test_rejects_untrusted_base_urls(self):
        with self.assertRaises(CatalogResponseError):
            CatalogClient("http://catalog.example", self.encoded_key)

    def test_rejects_incompatible_release_without_installing_files(self):
        record, manifest, signature, chunks = make_release(
            self.key, min_version="99.0.0"
        )
        client, _ = self.make_client(record, manifest, signature, chunks)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(CatalogCompatibilityError):
                client.download_verified(record, directory)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_compares_prerelease_versions_using_semver_order(self):
        record, manifest, signature, chunks = make_release(
            self.key, min_version="1.0.0-alpha"
        )
        client, _ = self.make_client(
            record,
            manifest,
            signature,
            chunks,
            pyos_version="1.0.0-1",
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(CatalogCompatibilityError):
                client.download_verified(record, directory)

    def test_rejects_invalid_signature_without_installing_files(self):
        record, manifest, _, chunks = make_release(self.key)
        client, _ = self.make_client(record, manifest, "AAAA", chunks)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(CatalogVerificationError):
                client.download_verified(record, directory)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_rejects_modified_archive_chunk(self):
        record, manifest, signature, chunks = make_release(self.key)
        altered = [bytes([chunks[0][0] ^ 1]) + chunks[0][1:]]
        client, _ = self.make_client(record, manifest, signature, altered)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(CatalogVerificationError):
                client.download_verified(record, directory)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_rejects_manifest_with_zip_slip_path(self):
        record, manifest, signature, chunks = make_release(self.key)
        manifest["files"][1]["path"] = "../outside.py"
        signature = base64.b64encode(self.key.sign(canonical_json(manifest))).decode("ascii")
        client, _ = self.make_client(record, manifest, signature, chunks)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(CatalogResponseError):
                client.download_verified(record, directory)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_rejects_non_success_catalog_response(self):
        client = CatalogClient(
            "https://catalog.example",
            self.encoded_key,
            session=FakeSession(
                {"https://catalog.example/v1/apps": FakeResponse(503, b'{"error":"unavailable"}')}
            ),
        )
        with self.assertRaises(CatalogResponseError):
            client.list_apps()

    def test_rejects_catalog_record_over_100_mib(self):
        record, manifest, signature, chunks = make_release(self.key)
        record["package_size"] = 100 * 1024 * 1024 + 1
        client, _ = self.make_client(record, manifest, signature, chunks)

        with self.assertRaises(CatalogResponseError):
            client.list_apps()

    def test_rejects_missing_or_oversized_chunk_response(self):
        record, manifest, signature, chunks = make_release(self.key)
        client, session = self.make_client(record, manifest, signature, chunks)
        chunk_url = (
            f"https://catalog.example/v1/apps/{record['id']}/{record['version']}/chunks/0"
        )
        with tempfile.TemporaryDirectory() as directory:
            session.responses[chunk_url] = FakeResponse(404, b'{"error":"not_found"}')
            with self.assertRaises(CatalogResponseError):
                client.download_verified(record, directory)
            self.assertEqual(list(Path(directory).iterdir()), [])

            session.responses[chunk_url] = FakeResponse(200, b"x" * (CHUNK_SIZE + 1))
            with self.assertRaises(CatalogResponseError):
                client.download_verified(record, directory)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_downloads_large_package_in_bounded_chunks(self):
        contents = random.Random(24).randbytes(1_100_000)
        record, manifest, signature, chunks = make_release(
            self.key,
            files={"__init__.py": b"pass\n", "payload.bin": contents},
        )
        client, session = self.make_client(record, manifest, signature, chunks)
        with tempfile.TemporaryDirectory() as directory:
            installed = client.download_verified(record, directory)

            self.assertEqual(len(chunks), 2)
            self.assertEqual((installed / "payload.bin").read_bytes(), contents)
            chunk_calls = [call for call in session.calls if "/chunks/" in call[0]]
            self.assertEqual(len(chunk_calls), 2)

    def test_network_failure_is_explicit(self):
        record, manifest, signature, chunks = make_release(self.key)
        client, session = self.make_client(record, manifest, signature, chunks)
        session.responses["https://catalog.example/v1/apps"] = requests.Timeout("offline")

        with self.assertRaises(CatalogNetworkError):
            client.list_apps()

    def test_failed_update_keeps_the_previous_install(self):
        record, manifest, signature, chunks = make_release(self.key)
        client, session = self.make_client(record, manifest, signature, chunks)
        with tempfile.TemporaryDirectory() as directory:
            installed = client.download_verified(record, directory)
            old_content = (installed / "greeting.py").read_bytes()
            session.responses[
                f"https://catalog.example/v1/apps/{record['id']}/{record['version']}/manifest"
            ] = FakeResponse(
                200,
                json.dumps({"manifest": manifest, "signature": "AAAA"}).encode("utf-8"),
            )

            with self.assertRaises(CatalogVerificationError):
                client.download_verified(record, directory)

            self.assertEqual((installed / "greeting.py").read_bytes(), old_content)


if __name__ == "__main__":
    unittest.main()
