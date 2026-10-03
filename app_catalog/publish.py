"""Build and publish signed, multi-file PyOS app releases."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import stat
import sys
import unicodedata
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

MAX_PACKAGE_SIZE = 100 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024
MAX_MANIFEST_SIZE = 1024 * 1024
MAX_FILES = 5_000
APP_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
VERSION_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


class PackageValidationError(ValueError):
    """Raised when registry metadata or a package cannot be safely published."""


@dataclass(frozen=True)
class ReleaseBuild:
    archive: bytes
    manifest: dict[str, Any]
    manifest_bytes: bytes
    signature: str
    signature_bytes: bytes
    chunks: tuple[bytes, ...]


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _validate_relative_path(value: str) -> None:
    parts = value.split("/")
    if (
        not value
        or value.casefold() == ".pyos-catalog.json"
        or len(value.encode("utf-8")) > 512
        or "\\" in value
        or value.startswith("/")
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or unicodedata.normalize("NFC", value) != value
        or any(
            not part
            or part in {".", ".."}
            or any(character in part for character in '<>:"|?*')
            or part.endswith((" ", "."))
            or part.split(".", 1)[0].rstrip(" .").upper() in WINDOWS_RESERVED_NAMES
            for part in parts
        )
    ):
        raise PackageValidationError(f"unsafe path: {value!r}")


def validate_registry(
    registry: Any, repository_root: str | Path
) -> list[dict[str, Any]]:
    if not isinstance(registry, dict) or not isinstance(registry.get("apps"), list):
        raise PackageValidationError("registry must contain an apps list")

    root = Path(repository_root).resolve()
    seen_ids: set[str] = set()
    entries: list[dict[str, Any]] = []
    for entry in registry["apps"]:
        if not isinstance(entry, dict):
            raise PackageValidationError("each registry app must be an object")
        app_id = entry.get("id")
        if not isinstance(app_id, str) or not APP_ID_PATTERN.fullmatch(app_id):
            raise PackageValidationError(f"invalid app ID: {app_id!r}")
        if app_id in seen_ids:
            raise PackageValidationError(f"duplicate app ID: {app_id}")
        seen_ids.add(app_id)

        for field, limit in (("name", 128), ("description", 2_048)):
            value = entry.get(field)
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise PackageValidationError(f"missing or invalid {field} for {app_id}")
        release_notes = entry.get("release_notes")
        if not isinstance(release_notes, str) or len(release_notes) > 2_048:
            raise PackageValidationError(f"missing or invalid release_notes for {app_id}")
        if entry.get("entry_point") != "__init__.py":
            raise PackageValidationError(f"invalid entry point for {app_id}")
        for field in ("version", "min_pyos_version"):
            value = entry.get(field)
            if not isinstance(value, str) or not VERSION_PATTERN.fullmatch(value):
                raise PackageValidationError(f"missing or invalid {field} for {app_id}")

        package = entry.get("package")
        if not isinstance(package, str) or "\\" in package or package.startswith("/"):
            raise PackageValidationError(f"package path must be inside the repository: {package!r}")
        package_parts = package.split("/")
        if any(part in {"", ".", ".."} for part in package_parts):
            raise PackageValidationError(f"package path must be inside the repository: {package!r}")
        _validate_relative_path(package)
        package_path = root.joinpath(*package_parts)
        if any((root.joinpath(*package_parts[:index])).is_symlink() for index in range(1, len(package_parts) + 1)):
            raise PackageValidationError(f"package path cannot contain symbolic links: {package!r}")
        resolved = package_path.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as error:
            raise PackageValidationError(
                f"package path must be inside the repository: {package!r}"
            ) from error
        if not resolved.is_dir() or not (resolved / "__init__.py").is_file():
            raise PackageValidationError(f"package must contain __init__.py: {package!r}")

        normalized = dict(entry)
        normalized["package_path"] = resolved
        entries.append(normalized)
    return entries


def _package_files(package_dir: Path) -> list[tuple[str, bytes]]:
    if package_dir.is_symlink() or not package_dir.is_dir():
        raise PackageValidationError("package must be a real directory")
    files: list[tuple[str, bytes]] = []
    seen_paths: set[str] = set()
    expanded_size = 0
    for path in package_dir.rglob("*"):
        if path.is_symlink():
            raise PackageValidationError(f"symbolic links are not allowed: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise PackageValidationError(f"unsupported file type: {path}")

        relative = path.relative_to(package_dir).as_posix()
        if "__pycache__" in relative.split("/") or path.suffix.lower() in {".pyc", ".pyo"}:
            continue
        _validate_relative_path(relative)
        path_key = relative.casefold()
        if path_key in seen_paths:
            raise PackageValidationError(f"duplicate normalized path: {relative}")
        seen_paths.add(path_key)
        remaining = MAX_PACKAGE_SIZE - expanded_size
        with path.open("rb") as source:
            content = source.read(remaining + 1)
        if len(content) > remaining:
            raise PackageValidationError("package exceeds the 100 MiB expanded size limit")
        expanded_size += len(content)
        files.append((relative, content))
        if len(files) > MAX_FILES:
            raise PackageValidationError(f"package exceeds the {MAX_FILES} file limit")

    files.sort(key=lambda item: item[0].encode("utf-16-be"))
    if "__init__.py" not in {path for path, _ in files}:
        raise PackageValidationError("package is missing its __init__.py entry point")
    return files


def build_release(
    package_dir: str | Path,
    *,
    app_id: str,
    version: str,
    name: str,
    description: str,
    min_pyos_version: str,
    signing_key: Ed25519PrivateKey,
) -> ReleaseBuild:
    if not isinstance(app_id, str) or not APP_ID_PATTERN.fullmatch(app_id):
        raise PackageValidationError(f"invalid app ID: {app_id!r}")
    for field, value in (("version", version), ("min_pyos_version", min_pyos_version)):
        if not isinstance(value, str) or not VERSION_PATTERN.fullmatch(value):
            raise PackageValidationError(f"invalid {field}: {value!r}")
    if not isinstance(name, str) or not name.strip() or len(name) > 128:
        raise PackageValidationError("missing or invalid name")
    if not isinstance(description, str) or not description.strip() or len(description) > 2_048:
        raise PackageValidationError("missing or invalid description")

    files = _package_files(Path(package_dir))
    expanded_size = sum(len(content) for _, content in files)
    if expanded_size > MAX_PACKAGE_SIZE:
        raise PackageValidationError("package exceeds the 100 MiB expanded size limit")

    file_records = [
        {
            "path": path,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        for path, content in files
    ]
    archive_stream = BytesIO()
    with zipfile.ZipFile(archive_stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for (path, content), record in zip(files, file_records):
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, content, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)

    archive_bytes = archive_stream.getvalue()
    if len(archive_bytes) > MAX_PACKAGE_SIZE:
        raise PackageValidationError("package exceeds the 100 MiB compressed size limit")
    chunks = tuple(
        archive_bytes[offset : offset + CHUNK_SIZE]
        for offset in range(0, len(archive_bytes), CHUNK_SIZE)
    )
    manifest = {
        "app_id": app_id,
        "version": version,
        "name": name,
        "description": description,
        "min_pyos_version": min_pyos_version,
        "entry_point": "__init__.py",
        "files": file_records,
        "archive_size": len(archive_bytes),
        "expanded_size": expanded_size,
        "archive_sha256": hashlib.sha256(archive_bytes).hexdigest(),
        "chunk_size": CHUNK_SIZE,
        "chunk_hashes": [hashlib.sha256(chunk).hexdigest() for chunk in chunks],
    }
    manifest_bytes = canonical_json_bytes(manifest)
    if len(manifest_bytes) > MAX_MANIFEST_SIZE:
        raise PackageValidationError("manifest exceeds the 1 MiB size limit")
    signature_bytes = signing_key.sign(manifest_bytes)
    return ReleaseBuild(
        archive=archive_bytes,
        manifest=manifest,
        manifest_bytes=manifest_bytes,
        signature=base64.b64encode(signature_bytes).decode("ascii"),
        signature_bytes=signature_bytes,
        chunks=chunks,
    )


def _read_signing_key(value: str) -> Ed25519PrivateKey:
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, base64.binascii.Error) as error:
        raise PackageValidationError("APP_CATALOG_SIGNING_KEY must be base64 raw Ed25519 key bytes") from error
    if len(raw) != 32:
        raise PackageValidationError("APP_CATALOG_SIGNING_KEY must encode exactly 32 bytes")
    return Ed25519PrivateKey.from_private_bytes(raw)


def publish_release(
    release: ReleaseBuild,
    *,
    app_id: str,
    version: str,
    base_url: str,
    token: str,
    session: Any = None,
) -> None:
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
        raise PackageValidationError("catalog API URL must be an HTTPS origin")
    if (
        not isinstance(app_id, str)
        or not APP_ID_PATTERN.fullmatch(app_id)
        or not isinstance(version, str)
        or not VERSION_PATTERN.fullmatch(version)
    ):
        raise PackageValidationError("invalid app ID or version")
    if not token or "\r" in token or "\n" in token:
        raise PackageValidationError("APP_CATALOG_PUBLISH_TOKEN is required")

    import requests

    if session is None:
        session = requests.Session()
    base = f"https://{parsed.netloc}"
    headers = {"Authorization": f"Bearer {token}"}
    response = _request(
        session,
        "post",
        f"{base}/v1/admin/releases",
        headers={**headers, "Content-Type": "application/json"},
        json={"manifest": release.manifest, "signature": release.signature},
        timeout=(10, 60),
        allow_redirects=False,
    )
    _require_status(response, {200, 201}, "start release")
    try:
        result = response.json()
    except (ValueError, AttributeError) as error:
        raise PackageValidationError("catalog API returned invalid release JSON") from error
    if not isinstance(result, dict):
        raise PackageValidationError("catalog API returned an invalid release response")
    if result.get("chunk_count") != len(release.chunks):
        raise PackageValidationError("catalog API returned an unexpected chunk count")

    for index, chunk in enumerate(release.chunks):
        response = _request(
            session,
            "put",
            f"{base}/v1/admin/releases/{app_id}/{version}/chunks/{index}",
            headers={**headers, "Content-Type": "application/octet-stream"},
            data=chunk,
            timeout=(10, 60),
            allow_redirects=False,
        )
        _require_status(response, {204}, f"upload chunk {index}")

    response = _request(
        session,
        "post",
        f"{base}/v1/admin/releases/{app_id}/{version}/publish",
        headers=headers,
        timeout=(10, 60),
        allow_redirects=False,
    )
    _require_status(response, {200}, "publish release")


def _require_status(response: Any, expected: set[int], operation: str) -> None:
    status = getattr(response, "status_code", None)
    if status not in expected:
        raise PackageValidationError(
            f"catalog API failed to {operation}: unexpected HTTP status {status}"
        )


def _request(session: Any, method: str, url: str, **kwargs: Any) -> Any:
    import requests

    try:
        return getattr(session, method)(url, **kwargs)
    except requests.RequestException as error:
        raise PackageValidationError("catalog API request failed") from error


def _registry_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise PackageValidationError(f"duplicate registry field: {key}")
        result[key] = value
    return result


def _run_publish(args: argparse.Namespace) -> int:
    repository_root = Path(__file__).resolve().parent.parent
    registry_path = Path(args.registry).resolve() if args.registry else Path(__file__).with_name("registry.json")
    try:
        registry = json.loads(
            registry_path.read_text(encoding="utf-8"),
            object_pairs_hook=_registry_object,
        )
        entries = validate_registry(registry, repository_root)
        entry = next((item for item in entries if item["id"] == args.app_id), None)
        if entry is None:
            raise PackageValidationError(f"app ID is not in the reviewed registry: {args.app_id}")
        if args.version != entry["version"]:
            raise PackageValidationError(
                f"requested version {args.version} does not match reviewed registry version {entry['version']}"
            )
        base_url = os.environ.get("APP_CATALOG_API_URL", "")
        token = os.environ.get("APP_CATALOG_PUBLISH_TOKEN", "")
        signing_key = _read_signing_key(os.environ.get("APP_CATALOG_SIGNING_KEY", ""))
        release = build_release(
            entry["package_path"],
            app_id=entry["id"],
            version=entry["version"],
            name=entry["name"],
            description=entry["description"],
            min_pyos_version=entry["min_pyos_version"],
            signing_key=signing_key,
        )
        publish_release(
            release,
            app_id=entry["id"],
            version=entry["version"],
            base_url=base_url,
            token=token,
        )
    except (OSError, json.JSONDecodeError, PackageValidationError) as error:
        print(f"Publish failed: {error}", file=sys.stderr)
        return 1

    print(f"Published {args.app_id} {args.version}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    publish_parser = subparsers.add_parser("publish", help="publish an approved registry app")
    publish_parser.add_argument("--app-id", required=True)
    publish_parser.add_argument("--version", required=True)
    publish_parser.add_argument("--registry", help="registry JSON path (defaults to app_catalog/registry.json)")
    publish_parser.set_defaults(handler=_run_publish)
    args = parser.parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
