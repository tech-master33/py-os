"""HTTPS client for downloading and verifying PyOS app catalog releases."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import tempfile
import unicodedata
import zipfile
import zlib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

MAX_PACKAGE_SIZE = 100 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024
MAX_MANIFEST_SIZE = 1024 * 1024
MAX_FILES = 5_000
PYOS_VERSION = "1.0.0"
APP_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
VERSION_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
WINDOWS_RESERVED_NAME = re.compile(
    r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])[ .]*(?:\.|$)",
    re.IGNORECASE,
)
MANIFEST_FIELDS = {
    "app_id",
    "version",
    "name",
    "description",
    "min_pyos_version",
    "entry_point",
    "files",
    "archive_size",
    "expanded_size",
    "archive_sha256",
    "chunk_size",
    "chunk_hashes",
}


class CatalogError(Exception):
    """Base class for catalog access and verification failures."""


class CatalogNetworkError(CatalogError):
    """Raised when a catalog request cannot be completed."""


class CatalogResponseError(CatalogError):
    """Raised when the catalog response is invalid or unsafe."""


class CatalogVerificationError(CatalogError):
    """Raised when downloaded content does not match its signed manifest."""


class CatalogCompatibilityError(CatalogError):
    """Raised when a release requires a newer PyOS version."""


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise CatalogResponseError("catalog manifest cannot be canonicalized") from error


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise CatalogResponseError("catalog JSON contains duplicate keys")
        result[key] = value
    return result


def _is_digest(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _validate_app_id(value: Any) -> bool:
    return isinstance(value, str) and APP_ID_PATTERN.fullmatch(value) is not None


def _validate_version(value: Any) -> bool:
    return isinstance(value, str) and VERSION_PATTERN.fullmatch(value) is not None


def _parse_version(value: str) -> tuple[int, int, int, tuple[str, ...]]:
    main, _, build = value.partition("+")
    core, separator, prerelease = main.partition("-")
    major, minor, patch = (int(part) for part in core.split("."))
    identifiers = tuple(prerelease.split(".")) if separator else ()
    return major, minor, patch, identifiers


def _version_is_compatible(current: str, required: str) -> bool:
    current_parts = _parse_version(current)
    required_parts = _parse_version(required)
    current_core = current_parts[:3]
    required_core = required_parts[:3]
    if current_core != required_core:
        return current_core > required_core
    current_pre = current_parts[3]
    required_pre = required_parts[3]
    if not required_pre:
        return not current_pre
    if not current_pre:
        return True
    for left, right in zip(current_pre, required_pre):
        if left == right:
            continue
        left_numeric = left.isdigit()
        right_numeric = right.isdigit()
        if left_numeric and right_numeric:
            return int(left) > int(right)
        if left_numeric != right_numeric:
            return not left_numeric
        return left > right
    return len(current_pre) >= len(required_pre)


def _validate_package_path(value: Any) -> list[str]:
    if (
        not isinstance(value, str)
        or not value
        or value.casefold() == ".pyos-catalog.json"
        or len(value.encode("utf-8")) > 512
        or "\\" in value
        or value.startswith("/")
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or unicodedata.normalize("NFC", value) != value
    ):
        raise CatalogResponseError(f"unsafe package path: {value!r}")
    parts = value.split("/")
    if any(
        not part
        or part in {".", ".."}
        or any(character in part for character in '<>:"|?*')
        or part.endswith((" ", "."))
        or WINDOWS_RESERVED_NAME.match(part)
        for part in parts
    ):
        raise CatalogResponseError(f"unsafe package path: {value!r}")
    return parts


class CatalogClient:
    def __init__(
        self,
        base_url: str,
        public_key: str,
        session: Any = None,
        pyos_version: str = PYOS_VERSION,
    ):
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise CatalogResponseError("catalog API URL must be an HTTPS origin")
        if not _validate_version(pyos_version):
            raise CatalogResponseError("invalid local PyOS version")
        try:
            key_bytes = base64.b64decode(public_key, validate=True)
            if len(key_bytes) != 32:
                raise ValueError("incorrect key length")
            self._public_key = Ed25519PublicKey.from_public_bytes(key_bytes)
        except (ValueError, base64.binascii.Error) as error:
            raise CatalogResponseError("invalid catalog Ed25519 public key") from error
        self.base_url = f"https://{parsed.netloc}"
        self._session = session if session is not None else requests.Session()
        self.pyos_version = pyos_version

    def _request(self, path: str, max_bytes: int) -> bytes:
        url = f"{self.base_url}{path}"
        try:
            response = self._session.get(
                url,
                timeout=(10, 60),
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException as error:
            raise CatalogNetworkError(f"catalog request failed: {error}") from error
        status = getattr(response, "status_code", None)
        if status != 200:
            raise CatalogResponseError(f"catalog request returned HTTP status {status}")

        content_length = getattr(response, "headers", {}).get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > max_bytes:
                    raise CatalogResponseError("catalog response exceeds its size limit")
            except ValueError as error:
                raise CatalogResponseError("catalog returned an invalid content length") from error

        body = bytearray()
        try:
            chunks = response.iter_content(chunk_size=64 * 1024)
            for chunk in chunks:
                if not chunk:
                    continue
                if not isinstance(chunk, bytes):
                    chunk = bytes(chunk)
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise CatalogResponseError("catalog response exceeds its size limit")
        except requests.RequestException as error:
            raise CatalogNetworkError(f"catalog response download failed: {error}") from error
        return bytes(body)

    def _get_json(self, path: str) -> Any:
        body = self._request(path, MAX_MANIFEST_SIZE)
        try:
            return json.loads(body.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CatalogResponseError("catalog returned invalid JSON") from error

    @staticmethod
    def _validate_app_record(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise CatalogResponseError("catalog app record must be an object")
        required = {
            "id",
            "name",
            "description",
            "version",
            "min_pyos_version",
            "package_size",
            "chunk_size",
            "chunk_count",
        }
        if set(value) != required:
            raise CatalogResponseError("catalog app record has an invalid shape")
        if (
            not _validate_app_id(value["id"])
            or not isinstance(value["name"], str)
            or not value["name"].strip()
            or len(value["name"]) > 128
            or not isinstance(value["description"], str)
            or len(value["description"]) > 2_048
            or not _validate_version(value["version"])
            or not _validate_version(value["min_pyos_version"])
            or not isinstance(value["package_size"], int)
            or isinstance(value["package_size"], bool)
            or not 0 < value["package_size"] <= MAX_PACKAGE_SIZE
            or value["chunk_size"] != CHUNK_SIZE
            or not isinstance(value["chunk_count"], int)
            or isinstance(value["chunk_count"], bool)
            or value["chunk_count"] != (value["package_size"] + CHUNK_SIZE - 1) // CHUNK_SIZE
        ):
            raise CatalogResponseError("catalog app record contains invalid values")
        return value

    def list_apps(self) -> list[dict[str, Any]]:
        response = self._get_json("/v1/apps")
        if not isinstance(response, dict) or set(response) != {"apps"}:
            raise CatalogResponseError("catalog app list has an invalid shape")
        apps = response["apps"]
        if not isinstance(apps, list) or len(apps) > 100:
            raise CatalogResponseError("catalog app list exceeds its limit")
        return [self._validate_app_record(item) for item in apps]

    def get_app(self, app_id: str) -> dict[str, Any]:
        if not _validate_app_id(app_id):
            raise CatalogResponseError("invalid app ID")
        return self._validate_app_record(self._get_json(f"/v1/apps/{app_id}"))

    def _validate_manifest(
        self, value: Any, signature: Any, record: dict[str, Any]
    ) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != MANIFEST_FIELDS:
            raise CatalogResponseError("catalog manifest has an invalid shape")
        archive_size = value["archive_size"]
        chunk_hashes = value["chunk_hashes"]
        if (
            value["app_id"] != record["id"]
            or value["version"] != record["version"]
            or value["name"] != record["name"]
            or value["description"] != record["description"]
            or value["min_pyos_version"] != record["min_pyos_version"]
            or value["entry_point"] != "__init__.py"
            or not isinstance(value["files"], list)
            or not value["files"]
            or len(value["files"]) > MAX_FILES
            or not isinstance(archive_size, int)
            or isinstance(archive_size, bool)
            or not 0 < archive_size <= MAX_PACKAGE_SIZE
            or archive_size != record["package_size"]
            or value["chunk_size"] != CHUNK_SIZE
            or record["chunk_count"] != (archive_size + CHUNK_SIZE - 1) // CHUNK_SIZE
            or not isinstance(value["expanded_size"], int)
            or isinstance(value["expanded_size"], bool)
            or not 0 <= value["expanded_size"] <= MAX_PACKAGE_SIZE
            or not _is_digest(value["archive_sha256"])
            or not isinstance(chunk_hashes, list)
            or len(chunk_hashes) != record["chunk_count"]
            or not isinstance(signature, str)
        ):
            raise CatalogResponseError("catalog manifest does not match the app record")

        file_paths: set[str] = set()
        total_size = 0
        previous_path = ""
        for item in value["files"]:
            if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
                raise CatalogResponseError("catalog manifest contains an invalid file record")
            parts = _validate_package_path(item["path"])
            path = item["path"]
            if (
                path.casefold() in file_paths
                or path.encode("utf-16-be") <= previous_path.encode("utf-16-be")
                or not isinstance(item["size"], int)
                or isinstance(item["size"], bool)
                or item["size"] < 0
                or not _is_digest(item["sha256"])
            ):
                raise CatalogResponseError("catalog manifest contains an invalid file record")
            file_paths.add(path.casefold())
            previous_path = path
            total_size += item["size"]
            if total_size > MAX_PACKAGE_SIZE:
                raise CatalogResponseError("expanded package exceeds the 100 MiB limit")
            del parts
        if (
            total_size != value["expanded_size"]
            or "__init__.py" not in {item["path"] for item in value["files"]}
            or any(not _is_digest(digest) for digest in chunk_hashes)
        ):
            raise CatalogResponseError("catalog manifest file list is inconsistent")

        try:
            signature_bytes = base64.b64decode(signature, validate=True)
            if len(signature_bytes) != 64:
                raise ValueError("incorrect signature length")
            self._public_key.verify(signature_bytes, _canonical_json(value))
        except (ValueError, base64.binascii.Error, InvalidSignature) as error:
            raise CatalogVerificationError("catalog manifest signature is invalid") from error
        if not _version_is_compatible(self.pyos_version, value["min_pyos_version"]):
            raise CatalogCompatibilityError(
                f"app requires PyOS {value['min_pyos_version']} or newer"
            )
        return value

    def _extract_verified_archive(
        self, archive_path: Path, manifest: dict[str, Any], staging_dir: Path
    ) -> None:
        expected_files = {item["path"]: item for item in manifest["files"]}
        expected_casefold = {path.casefold() for path in expected_files}
        seen_files: set[str] = set()
        seen_entries: set[str] = set()
        try:
            with zipfile.ZipFile(archive_path, "r") as archive:
                for info in archive.infolist():
                    raw_path = info.filename[:-1] if info.is_dir() else info.filename
                    parts = _validate_package_path(raw_path)
                    normalized = raw_path.casefold()
                    if normalized in seen_entries:
                        raise CatalogVerificationError("archive contains duplicate paths")
                    seen_entries.add(normalized)
                    mode = info.external_attr >> 16
                    if stat.S_ISLNK(mode) or info.flag_bits & 0x1:
                        raise CatalogVerificationError("archive contains an unsafe file type")
                    if info.is_dir():
                        if not any(path.startswith(raw_path + "/") for path in expected_files):
                            raise CatalogVerificationError("archive contains an undeclared directory")
                        continue
                    item = expected_files.get(raw_path)
                    if item is None or normalized not in expected_casefold:
                        raise CatalogVerificationError("archive contains an undeclared file")
                    if info.file_size != item["size"]:
                        raise CatalogVerificationError("archive file size does not match its manifest")
                    destination = staging_dir.joinpath(*parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    digest = hashlib.sha256()
                    written = 0
                    with archive.open(info, "r") as source, destination.open("xb") as output:
                        while True:
                            chunk = source.read(64 * 1024)
                            if not chunk:
                                break
                            written += len(chunk)
                            if written > item["size"] or written > MAX_PACKAGE_SIZE:
                                raise CatalogVerificationError("archive file exceeds its declared size")
                            digest.update(chunk)
                            output.write(chunk)
                    if written != item["size"] or digest.hexdigest() != item["sha256"]:
                        raise CatalogVerificationError("archive file digest does not match its manifest")
                    seen_files.add(raw_path)
        except (
            EOFError,
            NotImplementedError,
            OSError,
            RuntimeError,
            ValueError,
            zlib.error,
            zipfile.BadZipFile,
            zipfile.LargeZipFile,
        ) as error:
            raise CatalogVerificationError("catalog package is not a valid safe ZIP archive") from error
        if seen_files != set(expected_files):
            raise CatalogVerificationError("archive is missing manifest-listed files")

    def download_verified(
        self, app_record: dict[str, Any], destination_dir: str | Path
    ) -> Path:
        record = self._validate_app_record(app_record)
        envelope = self._get_json(
            f"/v1/apps/{record['id']}/{record['version']}/manifest"
        )
        if not isinstance(envelope, dict) or set(envelope) != {"manifest", "signature"}:
            raise CatalogResponseError("catalog returned an invalid signed manifest")
        manifest = self._validate_manifest(
            envelope["manifest"], envelope["signature"], record
        )

        destination_root = Path(destination_dir).expanduser()
        destination_root.mkdir(parents=True, exist_ok=True)
        destination_root = destination_root.resolve()
        target = destination_root / record["id"]
        if target.is_symlink():
            raise CatalogResponseError("installed app path cannot be a symbolic link")

        with tempfile.TemporaryDirectory(prefix=".pyos-app-", dir=destination_root) as temporary:
            temporary_dir = Path(temporary)
            archive_path = temporary_dir / "release.zip"
            archive_hash = hashlib.sha256()
            with archive_path.open("xb") as archive_file:
                for index, expected_digest in enumerate(manifest["chunk_hashes"]):
                    path = (
                        f"/v1/apps/{record['id']}/{record['version']}/chunks/{index}"
                    )
                    chunk = self._request(path, CHUNK_SIZE)
                    expected_size = (
                        manifest["archive_size"] - index * CHUNK_SIZE
                        if index == len(manifest["chunk_hashes"]) - 1
                        else CHUNK_SIZE
                    )
                    if len(chunk) != expected_size:
                        raise CatalogVerificationError("catalog chunk has an invalid size")
                    if hashlib.sha256(chunk).hexdigest() != expected_digest:
                        raise CatalogVerificationError("catalog chunk digest is invalid")
                    archive_hash.update(chunk)
                    archive_file.write(chunk)
            if archive_hash.hexdigest() != manifest["archive_sha256"]:
                raise CatalogVerificationError("catalog archive digest is invalid")

            staged = temporary_dir / "installed"
            staged.mkdir()
            self._extract_verified_archive(archive_path, manifest, staged)
            (staged / ".pyos-catalog.json").write_text(
                json.dumps(
                    {
                        "id": manifest["app_id"],
                        "version": manifest["version"],
                        "name": manifest["name"],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            backup = temporary_dir / "previous"
            if target.exists():
                if not target.is_dir():
                    raise CatalogResponseError("installed app target is not a directory")
                os.replace(target, backup)
            try:
                os.replace(staged, target)
            except OSError:
                if backup.exists():
                    os.replace(backup, target)
                raise
        return target
