import hashlib
import json
from io import BytesIO
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)

from app_catalog.publish import (
    PackageValidationError,
    _validate_relative_path,
    build_release,
    canonical_json_bytes,
    publish_release,
    validate_registry,
)


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.package = self.root / "welcome"
        self.package.mkdir()
        (self.package / "__init__.py").write_text(
            "from .helper import greeting\n", encoding="utf-8"
        )
        (self.package / "helper.py").write_text(
            "def greeting():\n    return 'Welcome to PyOS'\n", encoding="utf-8"
        )
        (self.package / "welcome.txt").write_text("A catalog app.\n", encoding="utf-8")
        self.key = Ed25519PrivateKey.generate()

    def tearDown(self):
        self.temp_dir.cleanup()

    def build(self):
        return build_release(
            self.package,
            app_id="welcome",
            version="1.2.0",
            name="Welcome",
            description="A sample multi-file PyOS app.",
            min_pyos_version="1.0.0",
            signing_key=self.key,
        )

    def test_builds_deterministic_multifile_zip_and_signed_manifest(self):
        release = self.build()
        repeat = self.build()

        self.assertEqual(release.archive, repeat.archive)
        self.assertEqual(release.manifest_bytes, repeat.manifest_bytes)
        self.assertEqual(release.signature, repeat.signature)
        manifest = json.loads(release.manifest_bytes)
        self.assertEqual(manifest["entry_point"], "__init__.py")
        self.assertEqual(
            {item["path"] for item in manifest["files"]},
            {"__init__.py", "helper.py", "welcome.txt"},
        )
        self.assertEqual(manifest["archive_size"], len(release.archive))
        self.assertEqual(manifest["chunk_size"], 1_048_576)
        self.assertEqual(manifest["chunk_hashes"], [manifest["archive_sha256"]])
        self.key.public_key().verify(
            release.signature_bytes,
            release.manifest_bytes,
        )

        with zipfile.ZipFile(BytesIO(release.archive)) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {"__init__.py", "helper.py", "welcome.txt"},
            )

    def test_builds_the_reviewed_registry_sample(self):
        repository_root = Path(__file__).resolve().parents[2]
        registry = json.loads(
            (repository_root / "app_catalog" / "registry.json").read_text(encoding="utf-8")
        )
        entry = validate_registry(registry, repository_root)[0]

        release = build_release(
            entry["package_path"],
            app_id=entry["id"],
            version=entry["version"],
            name=entry["name"],
            description=entry["description"],
            min_pyos_version=entry["min_pyos_version"],
            signing_key=self.key,
        )

        self.assertEqual(release.manifest["app_id"], "welcome")
        self.assertEqual(
            {item["path"] for item in release.manifest["files"]},
            {"__init__.py", "content.txt", "greeting.py"},
        )

    def test_canonical_json_sorts_keys_without_ascii_escaping(self):
        self.assertEqual(
            canonical_json_bytes({"z": "café", "a": 1}),
            '{"a":1,"z":"café"}'.encode("utf-8"),
        )

    def test_chunks_archive_and_uploads_without_sending_private_key(self):
        class FakeResponse:
            def __init__(self, status_code, payload=None):
                self.status_code = status_code
                self.payload = payload

            def json(self):
                return self.payload

        class FakeSession:
            def __init__(self):
                self.calls = []

            def post(self, url, **kwargs):
                self.calls.append(("post", url, kwargs))
                if url.endswith("/v1/admin/releases"):
                    return FakeResponse(201, {"chunk_count": 1})
                return FakeResponse(200)

            def put(self, url, **kwargs):
                self.calls.append(("put", url, kwargs))
                return FakeResponse(204)

        session = FakeSession()
        release = self.build()
        publish_release(
            release,
            app_id="welcome",
            version="1.2.0",
            base_url="https://catalog.example",
            token="secret-token",
            session=session,
        )

        self.assertEqual([call[0] for call in session.calls], ["post", "put", "post"])
        self.assertTrue(all(call[2]["allow_redirects"] is False for call in session.calls))
        private_key_bytes = self.key.private_bytes(
            Encoding.Raw,
            PrivateFormat.Raw,
            NoEncryption(),
        )
        self.assertNotIn(private_key_bytes, repr(session.calls).encode("utf-8"))
        self.assertEqual(
            session.calls[1][2]["data"],
            release.chunks[0],
        )

    def test_rejects_publish_redirect_status(self):
        class RedirectSession:
            def post(self, url, **kwargs):
                return type("Response", (), {"status_code": 302})()

        with self.assertRaisesRegex(PackageValidationError, "HTTP status 302"):
            publish_release(
                self.build(),
                app_id="welcome",
                version="1.2.0",
                base_url="https://catalog.example",
                token="secret-token",
                session=RedirectSession(),
            )

    def test_rejects_package_without_entry_point(self):
        (self.package / "__init__.py").unlink()

        with self.assertRaisesRegex(PackageValidationError, "entry point"):
            self.build()

    def test_rejects_unsafe_package_paths(self):
        with self.assertRaisesRegex(PackageValidationError, "unsafe path"):
            _validate_relative_path("bad:name.py")

    def test_rejects_symlinked_package_files(self):
        target = self.root / "outside.py"
        target.write_text("outside = True\n", encoding="utf-8")
        link = self.package / "linked.py"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation is not available")

        with self.assertRaisesRegex(PackageValidationError, "symbolic links"):
            self.build()

    def test_rejects_packages_over_configured_compressed_limit(self):
        with patch("app_catalog.publish.MAX_PACKAGE_SIZE", 64):
            with self.assertRaisesRegex(PackageValidationError, "100 MiB"):
                self.build()

    def test_rejects_expanded_package_over_configured_limit(self):
        (self.package / "large.bin").write_bytes(b"x" * 65)
        with patch("app_catalog.publish.MAX_PACKAGE_SIZE", 64):
            with self.assertRaisesRegex(PackageValidationError, "100 MiB expanded"):
                self.build()

    def test_chunks_archive_at_configured_boundaries(self):
        with patch("app_catalog.publish.CHUNK_SIZE", 64):
            release = self.build()

        self.assertGreater(len(release.chunks), 1)
        self.assertTrue(all(0 < len(chunk) <= 64 for chunk in release.chunks))
        self.assertEqual(
            release.manifest["chunk_hashes"],
            [hashlib.sha256(chunk).hexdigest() for chunk in release.chunks],
        )

    def test_rejects_manifest_over_configured_limit(self):
        with patch("app_catalog.publish.MAX_MANIFEST_SIZE", 16):
            with self.assertRaisesRegex(PackageValidationError, "manifest exceeds"):
                self.build()

    def test_validates_registry_paths_and_unique_ids(self):
        registry = {
            "apps": [
                {
                    "id": "welcome",
                    "package": "welcome",
                    "name": "Welcome",
                    "description": "A sample app.",
                    "release_notes": "Initial release.",
                    "entry_point": "__init__.py",
                    "version": "1.2.0",
                    "min_pyos_version": "1.0.0",
                }
            ]
        }

        entries = validate_registry(registry, self.root)

        self.assertEqual(entries[0]["id"], "welcome")

        duplicated = {"apps": [registry["apps"][0], registry["apps"][0]]}
        with self.assertRaisesRegex(PackageValidationError, "duplicate app ID"):
            validate_registry(duplicated, self.root)

    def test_rejects_registry_path_traversal(self):
        registry = {
            "apps": [
                {
                    "id": "welcome",
                    "package": "../outside",
                    "name": "Welcome",
                    "description": "A sample app.",
                    "release_notes": "Initial release.",
                    "entry_point": "__init__.py",
                    "version": "1.2.0",
                    "min_pyos_version": "1.0.0",
                }
            ]
        }

        with self.assertRaisesRegex(PackageValidationError, "inside the repository"):
            validate_registry(registry, self.root)

    def test_rejects_invalid_registry_id_and_missing_metadata(self):
        registry = {
            "apps": [
                {
                    "id": "Bad_ID",
                    "package": "welcome",
                    "name": "Welcome",
                    "description": "A sample app.",
                    "release_notes": "Initial release.",
                    "entry_point": "__init__.py",
                    "version": "1.2.0",
                    "min_pyos_version": "1.0.0",
                }
            ]
        }
        with self.assertRaisesRegex(PackageValidationError, "invalid app ID"):
            validate_registry(registry, self.root)

        registry["apps"][0]["id"] = "welcome"
        del registry["apps"][0]["description"]
        with self.assertRaisesRegex(PackageValidationError, "description"):
            validate_registry(registry, self.root)


if __name__ == "__main__":
    unittest.main()
