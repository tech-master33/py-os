"""Move published legacy app chunks from the catalog D1 into package D1 shards."""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Any
from urllib.parse import urlsplit


class MigrationError(RuntimeError):
    """Raised when the legacy package migration cannot safely complete."""


def _request(session: Any, method: str, url: str, **kwargs: Any) -> Any:
    import requests

    for attempt in range(3):
        try:
            response = getattr(session, method)(url, **kwargs)
        except (requests.Timeout, requests.ConnectionError) as error:
            if attempt == 2:
                raise MigrationError("catalog migration request failed after retries") from error
            continue
        except requests.RequestException as error:
            raise MigrationError("catalog migration request failed") from error
        if 300 <= response.status_code < 400:
            raise MigrationError("catalog migration endpoint redirected unexpectedly")
        return response
    raise MigrationError("catalog migration request failed")


def _require_status(response: Any, expected: set[int], operation: str) -> None:
    if getattr(response, "status_code", None) not in expected:
        raise MigrationError(
            f"catalog migration failed to {operation}: HTTP {getattr(response, 'status_code', None)}"
        )


def migrate_legacy_chunks(
    base_url: str,
    token: str,
    *,
    app_id: str | None = None,
    version: str | None = None,
    prune_legacy: bool = False,
    session: Any = None,
) -> int:
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
        raise MigrationError("catalog API URL must be an HTTPS origin")
    if not token or "\r" in token or "\n" in token:
        raise MigrationError("catalog publish token is required")
    if app_id is not None and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?", app_id) is None:
        raise MigrationError("invalid app ID filter")
    if version is not None and re.fullmatch(
        r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
        r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?",
        version,
    ) is None:
        raise MigrationError("invalid version filter")

    import requests

    if session is None:
        session = requests.Session()
    base = f"https://{parsed.netloc}"
    headers = {"Authorization": f"Bearer {token}"}
    response = _request(
        session,
        "get",
        f"{base}/v1/admin/storage-migrations",
        headers=headers,
        timeout=(10, 60),
        allow_redirects=False,
    )
    _require_status(response, {200}, "list legacy releases")
    try:
        payload = response.json()
    except (ValueError, AttributeError) as error:
        raise MigrationError("catalog migration returned invalid inventory JSON") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("versions"), list):
        raise MigrationError("catalog migration returned invalid inventory")

    migrated = 0
    for release in payload["versions"]:
        if not isinstance(release, dict):
            raise MigrationError("catalog migration returned an invalid version record")
        release_id = release.get("app_id")
        release_version = release.get("version")
        chunk_count = release.get("chunk_count")
        if (
            not isinstance(release_id, str)
            or re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?", release_id) is None
            or not isinstance(release_version, str)
            or re.fullmatch(
                r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
                r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?",
                release_version,
            ) is None
            or not isinstance(chunk_count, int)
            or isinstance(chunk_count, bool)
            or not 1 <= chunk_count <= 100
        ):
            raise MigrationError("catalog migration returned an invalid version record")
        if (app_id is not None and release_id != app_id) or (
            version is not None and release_version != version
        ):
            continue

        path = f"/v1/admin/storage-migrations/{release_id}/{release_version}"
        for index in range(chunk_count):
            response = _request(
                session,
                "put",
                f"{base}{path}/chunks/{index}",
                headers=headers,
                timeout=(10, 60),
                allow_redirects=False,
            )
            _require_status(response, {204}, f"copy chunk {index} for {release_id} {release_version}")

        response = _request(
            session,
            "post",
            f"{base}{path}/finalize",
            headers=headers,
            timeout=(10, 60),
            allow_redirects=False,
        )
        _require_status(response, {200}, f"finalize {release_id} {release_version}")
        if prune_legacy:
            response = _request(
                session,
                "post",
                f"{base}{path}/cleanup",
                headers=headers,
                timeout=(10, 60),
                allow_redirects=False,
            )
            _require_status(response, {200}, f"remove legacy chunks for {release_id} {release_version}")
        migrated += 1

    return migrated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="HTTPS Worker origin")
    parser.add_argument(
        "--token-env",
        default="APP_CATALOG_PUBLISH_TOKEN",
        help="environment variable containing the publish token",
    )
    parser.add_argument("--app-id", help="migrate only this app")
    parser.add_argument("--version", help="migrate only this app version")
    parser.add_argument(
        "--prune-legacy",
        action="store_true",
        help="remove legacy copies after successful shard verification and finalize",
    )
    args = parser.parse_args()
    token = os.environ.get(args.token_env, "")
    try:
        count = migrate_legacy_chunks(
            args.base_url,
            token,
            app_id=args.app_id,
            version=args.version,
            prune_legacy=args.prune_legacy,
        )
    except MigrationError as error:
        print(f"Migration failed: {error}", file=sys.stderr)
        return 1
    print(f"Migrated {count} published release(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
