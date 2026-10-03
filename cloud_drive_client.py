"""HTTPS client for shared Py-OS cloud drives."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import requests

DRIVE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32}$")
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
MAX_JSON_BYTES = 64 * 1024
MAX_CHUNK_BYTES = 20 * 1024 * 1024
MAX_FILE_BYTES = 1_000_000_000


class CloudDriveError(Exception):
    """Raised when a shared-drive request or transfer fails."""

    def __init__(self, message: str, *, code: str = "drive_error", status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


def _validate_drive_id(drive_id: str) -> str:
    if not isinstance(drive_id, str) or not DRIVE_ID_PATTERN.fullmatch(drive_id):
        raise CloudDriveError("invalid shared-drive ID", code="invalid_drive_id")
    return drive_id


def _validate_path(path: str, *, allow_root: bool = False) -> str:
    if not isinstance(path, str) or len(path.encode("utf-8")) > 1024 or "\\" in path:
        raise CloudDriveError("invalid shared-drive path", code="invalid_drive_path")
    if allow_root and path == "":
        return path
    parts = path.split("/")
    if (
        path.startswith("/")
        or not path
        or any(
            not part
            or part in {".", ".."}
            or part.endswith((".", " "))
            or any(character in '<>:"|?*' or ord(character) < 32 or ord(character) == 127 for character in part)
            for part in parts
        )
    ):
        raise CloudDriveError("invalid shared-drive path", code="invalid_drive_path")
    return path


def _digest_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_FILE_BYTES:
                raise CloudDriveError("file exceeds shared-drive file limit", code="file_too_large")
            digest.update(chunk)
    return size, digest.hexdigest()


class CloudDriveClient:
    """Create and access drives using the Py-OS shared-drive Worker API."""

    def __init__(self, base_url: str, session: Any = None):
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
            raise CloudDriveError("shared-drive API URL must be an HTTPS origin", code="invalid_api_url")
        self.base_url = f"https://{parsed.netloc}"
        self._session = session if session is not None else requests.Session()

    @staticmethod
    def _close_response(response: Any) -> None:
        close = getattr(response, "close", None)
        if callable(close):
            close()

    def _response_bytes(self, response: Any, limit: int) -> bytes:
        status = getattr(response, "status_code", None)
        body = bytearray()
        try:
            try:
                content_length = response.headers.get("content-length")
            except AttributeError:
                content_length = None
            if content_length is not None:
                try:
                    if int(content_length) > limit:
                        raise CloudDriveError("shared-drive response exceeds its size limit", code="response_too_large")
                except ValueError as error:
                    raise CloudDriveError("invalid content length from shared-drive server", code="invalid_response") from error
            try:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    if not isinstance(chunk, bytes):
                        chunk = bytes(chunk)
                    body.extend(chunk)
                    if len(body) > limit:
                        raise CloudDriveError("shared-drive response exceeds its size limit", code="response_too_large")
            except requests.RequestException as error:
                raise CloudDriveError(f"shared-drive response download failed: {error}", code="network_error") from error
        finally:
            self._close_response(response)
        if status is None:
            raise CloudDriveError("shared-drive server returned no HTTP status", code="invalid_response")
        if status < 200 or status >= 300:
            code = "drive_error"
            message = f"shared-drive request returned HTTP status {status}"
            try:
                payload = json.loads(bytes(body).decode("utf-8"))
                if isinstance(payload, dict) and isinstance(payload.get("error"), str):
                    code = payload["error"][:128]
                    message = code.replace("_", " ")
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
            raise CloudDriveError(message, code=code, status=status)
        return bytes(body)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        limit: int = MAX_JSON_BYTES,
    ) -> bytes:
        request_headers = dict(headers or {})
        if json_body is not None:
            request_headers["content-type"] = "application/json"
            body = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
        try:
            response = getattr(self._session, method.lower())(
                f"{self.base_url}{path}",
                data=body,
                headers=request_headers,
                timeout=(10, 60),
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException as error:
            raise CloudDriveError(f"shared-drive request failed: {error}", code="network_error") from error
        return self._response_bytes(response, limit)

    def _json_request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        body = self._request(method, path, **kwargs)
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CloudDriveError("shared-drive server returned invalid JSON", code="invalid_response") from error
        if not isinstance(value, dict):
            raise CloudDriveError("shared-drive server returned an invalid response", code="invalid_response")
        return value

    @staticmethod
    def _validate_drive(value: Any) -> dict[str, Any]:
        required = {"id", "name", "quota_bytes", "used_bytes"}
        if (
            not isinstance(value, dict)
            or set(value) != required
            or not isinstance(value["id"], str)
            or not DRIVE_ID_PATTERN.fullmatch(value["id"])
            or not isinstance(value["name"], str)
            or not value["name"]
            or not isinstance(value["quota_bytes"], int)
            or isinstance(value["quota_bytes"], bool)
            or value["quota_bytes"] <= 0
            or not isinstance(value["used_bytes"], int)
            or isinstance(value["used_bytes"], bool)
            or not 0 <= value["used_bytes"] <= value["quota_bytes"]
        ):
            raise CloudDriveError("shared-drive server returned invalid drive metadata", code="invalid_response")
        return value

    def create_drive(self, name: str, quota_bytes: int) -> dict[str, Any]:
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name.strip()) > 64
            or any(ord(character) < 32 or ord(character) == 127 for character in name)
            or not isinstance(quota_bytes, int)
            or isinstance(quota_bytes, bool)
            or quota_bytes <= 0
            or quota_bytes % (1024 * 1024)
        ):
            raise CloudDriveError("invalid shared-drive creation request", code="invalid_drive_request")
        return self._validate_drive(
            self._json_request(
                "post",
                "/v1/drives",
                json_body={"name": name.strip(), "quota_bytes": quota_bytes},
            )
        )

    def get_drive(self, drive_id: str) -> dict[str, Any]:
        return self._validate_drive(
            self._json_request("get", f"/v1/drives/{quote(_validate_drive_id(drive_id), safe='')}")
        )

    def list_entries(self, drive_id: str, path: str = "") -> list[dict[str, Any]]:
        drive_id = _validate_drive_id(drive_id)
        path = _validate_path(path, allow_root=True)
        query = urlencode({"path": path})
        value = self._json_request("get", f"/v1/drives/{drive_id}/entries?{query}")
        entries = value.get("entries")
        if set(value) != {"entries"} or not isinstance(entries, list) or len(entries) > 1000:
            raise CloudDriveError("shared-drive server returned an invalid directory listing", code="invalid_response")
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or set(entry) != {"name", "type", "size_bytes", "updated_at"}
                or not isinstance(entry["name"], str)
                or not entry["name"]
                or not isinstance(entry["type"], str)
                or entry["type"] not in {"file", "directory"}
                or not isinstance(entry["size_bytes"], int)
                or isinstance(entry["size_bytes"], bool)
                or entry["size_bytes"] < 0
                or not isinstance(entry["updated_at"], str)
            ):
                raise CloudDriveError("shared-drive server returned an invalid entry", code="invalid_response")
        return entries

    def create_directory(self, drive_id: str, path: str) -> None:
        self._json_request(
            "post",
            f"/v1/drives/{_validate_drive_id(drive_id)}/directories",
            json_body={"path": _validate_path(path)},
        )

    def upload_file(self, drive_id: str, local_path: str | Path, remote_path: str) -> dict[str, Any]:
        drive_id = _validate_drive_id(drive_id)
        remote_path = _validate_path(remote_path)
        source_path = Path(local_path)
        size, full_digest = _digest_file(source_path)
        session = self._json_request(
            "post",
            f"/v1/drives/{drive_id}/uploads",
            json_body={"path": remote_path, "size_bytes": size, "sha256": full_digest},
        )
        upload_id = session.get("upload_id")
        chunk_size = session.get("chunk_size")
        chunk_count = session.get("chunk_count")
        expected_count = max(1, (size + MAX_CHUNK_BYTES - 1) // MAX_CHUNK_BYTES)
        if (
            set(session) != {"upload_id", "chunk_size", "chunk_count"}
            or not isinstance(upload_id, str)
            or not DRIVE_ID_PATTERN.fullmatch(upload_id)
            or chunk_size != MAX_CHUNK_BYTES
            or chunk_count != expected_count
        ):
            raise CloudDriveError("shared-drive server returned invalid upload metadata", code="invalid_response")

        second_digest = hashlib.sha256()
        try:
            with source_path.open("rb") as source:
                for index in range(chunk_count):
                    chunk = source.read(chunk_size)
                    second_digest.update(chunk)
                    chunk_digest = hashlib.sha256(chunk).hexdigest()
                    self._request(
                        "put",
                        f"/v1/drives/{drive_id}/uploads/{upload_id}/{index}",
                        body=chunk,
                        headers={"x-chunk-sha256": chunk_digest},
                        limit=MAX_CHUNK_BYTES,
                    )
            if second_digest.hexdigest() != full_digest:
                raise CloudDriveError("local file changed during upload", code="file_changed")
        except CloudDriveError as error:
            self._abort_upload(drive_id, upload_id, error)
            raise error
        except OSError as error:
            self._abort_upload(drive_id, upload_id, error)
            raise CloudDriveError("local file could not be read during upload", code="file_read_error") from error
        try:
            return self._json_request(
                "post",
                f"/v1/drives/{drive_id}/uploads/{upload_id}/complete",
                json_body={},
            )
        except CloudDriveError as error:
            self._abort_upload(drive_id, upload_id, error)
            raise

    def _abort_upload(self, drive_id: str, upload_id: str, original_error: Exception) -> None:
        try:
            self._request("delete", f"/v1/drives/{drive_id}/uploads/{upload_id}")
        except CloudDriveError as cleanup_error:
            raise CloudDriveError(
                f"{original_error}; upload cleanup failed: {cleanup_error}",
                code=getattr(original_error, "code", "drive_error"),
                status=getattr(original_error, "status", None),
            ) from original_error

    def download_file(self, drive_id: str, remote_path: str, destination: str | Path) -> Path:
        drive_id = _validate_drive_id(drive_id)
        remote_path = _validate_path(remote_path)
        target = Path(destination).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        query = urlencode({"path": remote_path})
        url = f"{self.base_url}/v1/drives/{drive_id}/files?{query}"
        try:
            response = self._session.get(
                url,
                timeout=(10, 60),
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException as error:
            raise CloudDriveError(f"shared-drive download failed: {error}", code="network_error") from error

        if getattr(response, "status_code", None) != 200:
            self._response_bytes(response, MAX_JSON_BYTES)
            raise CloudDriveError("shared-drive download returned an unexpected response", code="invalid_response")
        expected_digest = response.headers.get("x-file-sha256", "")
        if not SHA256_PATTERN.fullmatch(expected_digest):
            self._close_response(response)
            raise CloudDriveError("shared-drive file digest is missing or invalid", code="invalid_response")
        content_length = response.headers.get("x-file-size-bytes")
        if content_length is None or not content_length.isdigit() or int(content_length) > MAX_FILE_BYTES:
            self._close_response(response)
            raise CloudDriveError("shared-drive file size is missing or invalid", code="invalid_response")
        expected_size = int(content_length)

        temporary_path: Path | None = None
        digest = hashlib.sha256()
        size = 0
        try:
            with tempfile.NamedTemporaryFile(
                prefix=".pyos-drive-",
                dir=target.parent,
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    if not isinstance(chunk, bytes):
                        chunk = bytes(chunk)
                    size += len(chunk)
                    if size > expected_size or size > MAX_FILE_BYTES:
                        raise CloudDriveError("download exceeds its declared size", code="invalid_response")
                    digest.update(chunk)
                    temporary.write(chunk)
            if size != expected_size or digest.hexdigest() != expected_digest:
                raise CloudDriveError("downloaded file failed size or digest verification", code="integrity_error")
            os.replace(temporary_path, target)
            temporary_path = None
            return target
        except requests.RequestException as error:
            raise CloudDriveError(f"shared-drive download failed: {error}", code="network_error") from error
        finally:
            self._close_response(response)
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def delete_entry(self, drive_id: str, path: str) -> dict[str, Any]:
        drive_id = _validate_drive_id(drive_id)
        query = urlencode({"path": _validate_path(path)})
        return self._json_request(
            "delete",
            f"/v1/drives/{drive_id}/entries?{query}",
            json_body={},
        )

    def delete_empty_drive(self, drive_id: str) -> dict[str, Any]:
        return self._json_request(
            "delete",
            f"/v1/drives/{_validate_drive_id(drive_id)}",
            json_body={},
        )
