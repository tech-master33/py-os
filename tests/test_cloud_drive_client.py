import unittest
import hashlib
import json
import tempfile
from pathlib import Path
from urllib.parse import quote

from cloud_drive_client import CloudDriveClient, CloudDriveError


class FakeResponse:
    def __init__(self, status_code, body=b"", headers=None):
        self.status_code = status_code
        self.body = body
        self.headers = headers or {}
        self.closed = False

    def iter_content(self, chunk_size):
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset : offset + chunk_size]

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def _call(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.handler(method, url, kwargs)

    def get(self, url, **kwargs):
        return self._call("get", url, **kwargs)

    def post(self, url, **kwargs):
        return self._call("post", url, **kwargs)

    def put(self, url, **kwargs):
        return self._call("put", url, **kwargs)

    def delete(self, url, **kwargs):
        return self._call("delete", url, **kwargs)


class CloudDriveClientContractTests(unittest.TestCase):
    def test_client_exposes_create_and_connect_operations(self):
        client = CloudDriveClient("https://catalog.example")
        self.assertTrue(callable(client.create_drive))
        self.assertTrue(callable(client.get_drive))
        self.assertTrue(callable(client.list_entries))
        self.assertTrue(callable(client.upload_file))
        self.assertTrue(callable(client.download_file))
        self.assertTrue(callable(client.delete_entry))

    def test_cloud_drive_errors_expose_code_and_http_status(self):
        error = CloudDriveError("drive quota exhausted", code="drive_quota_exhausted", status=409)
        self.assertEqual(error.code, "drive_quota_exhausted")
        self.assertEqual(error.status, 409)

    def test_client_rejects_non_https_origins(self):
        for url in ("http://catalog.example", "https://user@catalog.example", "https://catalog.example/path"):
            with self.subTest(url=url), self.assertRaises(CloudDriveError):
                CloudDriveClient(url)

    def test_create_drive_uses_api_and_validates_generated_drive_id(self):
        drive = {
            "id": "A" * 32,
            "name": "School",
            "quota_bytes": 1_048_576,
            "used_bytes": 0,
        }

        def handle(method, url, kwargs):
            self.assertEqual(method, "post")
            self.assertEqual(url, "https://catalog.example/v1/drives")
            self.assertFalse(kwargs["allow_redirects"])
            self.assertEqual(json.loads(kwargs["data"]), {
                "name": "School",
                "quota_bytes": 1_048_576,
            })
            return FakeResponse(201, json.dumps(drive).encode())

        client = CloudDriveClient("https://catalog.example", session=FakeSession(handle))
        self.assertEqual(client.create_drive(" School ", 1_048_576), drive)
        with self.assertRaises(CloudDriveError):
            client.create_drive("School", 1_048_577)

    def test_quota_errors_are_reported_as_cloud_drive_errors(self):
        def handle(method, url, kwargs):
            return FakeResponse(409, b'{"error":"drive_quota_exhausted"}')

        client = CloudDriveClient("https://catalog.example", session=FakeSession(handle))
        with self.assertRaises(CloudDriveError) as raised:
            client.create_drive("School", 1_048_576)
        self.assertEqual(raised.exception.code, "drive_quota_exhausted")
        self.assertEqual(raised.exception.status, 409)

    def test_oversized_response_is_closed(self):
        response = FakeResponse(200, headers={"content-length": str(1024 * 1024)})
        client = CloudDriveClient("https://catalog.example")

        with self.assertRaises(CloudDriveError):
            client._response_bytes(response, 1024)

        self.assertTrue(response.closed)

    def test_directory_listing_encodes_paths_and_rejects_traversal(self):
        def handle(method, url, kwargs):
            self.assertIn(f"/v1/drives/{'A' * 32}/entries?path=notes%2Fweek+1", url)
            return FakeResponse(200, b'{"entries":[]}')

        client = CloudDriveClient("https://catalog.example", session=FakeSession(handle))
        self.assertEqual(client.list_entries("A" * 32, "notes/week 1"), [])
        with self.assertRaises(CloudDriveError):
            client.list_entries("A" * 32, "../outside")

    def test_uploads_chunks_then_completes_only_after_all_chunks(self):
        data = b"binary\x00payload"
        digest = hashlib.sha256(data).hexdigest()
        upload_id = "B" * 32

        def handle(method, url, kwargs):
            if method == "post" and url.endswith("/uploads"):
                return FakeResponse(201, json.dumps({
                    "upload_id": upload_id,
                    "chunk_size": 20 * 1024 * 1024,
                    "chunk_count": 1,
                }).encode())
            if method == "put":
                self.assertEqual(kwargs["data"], data)
                self.assertEqual(kwargs["headers"]["x-chunk-sha256"], digest)
                return FakeResponse(200, b'{"status":"uploaded"}')
            if method == "post" and url.endswith("/complete"):
                return FakeResponse(200, b'{"status":"complete","cleanup_pending":false}')
            if method == "delete":
                return FakeResponse(200, b'{"status":"aborted"}')
            raise AssertionError((method, url))

        session = FakeSession(handle)
        client = CloudDriveClient("https://catalog.example", session=session)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.bin"
            source.write_bytes(data)
            result = client.upload_file("A" * 32, source, "folder/source.bin")
        self.assertEqual(result["status"], "complete")
        self.assertEqual([method for method, _, _ in session.calls], ["post", "put", "post"])

    def test_failed_chunk_aborts_upload_without_completing(self):
        data = b"content"
        upload_id = "C" * 32
        methods = []

        def handle(method, url, kwargs):
            methods.append(method)
            if method == "post" and url.endswith("/uploads"):
                return FakeResponse(201, json.dumps({
                    "upload_id": upload_id,
                    "chunk_size": 20 * 1024 * 1024,
                    "chunk_count": 1,
                }).encode())
            if method == "put":
                return FakeResponse(503, b'{"error":"drive_storage_unavailable"}')
            if method == "delete":
                return FakeResponse(200, b'{"status":"aborted"}')
            raise AssertionError("Upload must not complete after a failed chunk")

        client = CloudDriveClient("https://catalog.example", session=FakeSession(handle))
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.bin"
            source.write_bytes(data)
            with self.assertRaises(CloudDriveError):
                client.upload_file("A" * 32, source, "source.bin")
        self.assertEqual(methods, ["post", "put", "delete"])

    def test_failed_completion_aborts_upload(self):
        data = b"content"
        upload_id = "D" * 32
        methods = []

        def handle(method, url, kwargs):
            methods.append(method)
            if method == "post" and url.endswith("/uploads"):
                return FakeResponse(201, json.dumps({
                    "upload_id": upload_id,
                    "chunk_size": 20 * 1024 * 1024,
                    "chunk_count": 1,
                }).encode())
            if method == "put":
                return FakeResponse(200, b'{"status":"uploaded"}')
            if method == "post" and url.endswith("/complete"):
                return FakeResponse(503, b'{"error":"drive_storage_unavailable"}')
            if method == "delete":
                return FakeResponse(200, b'{"status":"aborted"}')
            raise AssertionError((method, url))

        client = CloudDriveClient("https://catalog.example", session=FakeSession(handle))
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.bin"
            source.write_bytes(data)
            with self.assertRaises(CloudDriveError):
                client.upload_file("A" * 32, source, "source.bin")
        self.assertEqual(methods, ["post", "put", "post", "delete"])

    def test_download_verifies_digest_before_replacing_destination(self):
        data = b"\x00\xfforiginal"
        digest = hashlib.sha256(data).hexdigest()

        def handle(method, url, kwargs):
            self.assertTrue(kwargs["allow_redirects"] is False)
            return FakeResponse(
                200,
                data,
                {"x-file-size-bytes": str(len(data)), "x-file-sha256": digest},
            )

        client = CloudDriveClient("https://catalog.example", session=FakeSession(handle))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "download.bin"
            target.write_bytes(b"previous")
            result = client.download_file("A" * 32, "download.bin", target)
            self.assertEqual(result, target)
            self.assertEqual(target.read_bytes(), data)

    def test_download_digest_failure_preserves_existing_file(self):
        data = b"tampered"

        def handle(method, url, kwargs):
            return FakeResponse(
                200,
                data,
                {"x-file-size-bytes": str(len(data)), "x-file-sha256": "0" * 64},
            )

        client = CloudDriveClient("https://catalog.example", session=FakeSession(handle))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "download.bin"
            target.write_bytes(b"previous")
            with self.assertRaises(CloudDriveError):
                client.download_file("A" * 32, "download.bin", target)
            self.assertEqual(target.read_bytes(), b"previous")


if __name__ == "__main__":
    unittest.main()
