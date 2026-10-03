import unittest
import requests

from app_server.scripts.migrate_legacy_chunks import (
    MigrationError,
    migrate_legacy_chunks,
)


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self.payload = payload

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        return self._respond("get", url, kwargs)

    def put(self, url, **kwargs):
        return self._respond("put", url, kwargs)

    def post(self, url, **kwargs):
        return self._respond("post", url, kwargs)

    def _respond(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError(f"unexpected request: {method} {url}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class MigrationTests(unittest.TestCase):
    base_url = "https://catalog.example"
    inventory = {
        "versions": [{"app_id": "welcome", "version": "1.0.0", "chunk_count": 2}]
    }

    def test_migrates_chunks_in_order_and_finalizes_after_all_succeed(self):
        session = FakeSession(
            [
                FakeResponse(200, self.inventory),
                FakeResponse(204),
                FakeResponse(204),
                FakeResponse(200),
            ]
        )

        count = migrate_legacy_chunks(self.base_url, "secret", session=session)

        self.assertEqual(count, 1)
        self.assertEqual(
            [(method, url.rsplit("/", 1)[-1]) for method, url, _ in session.calls[1:]],
            [
                ("put", "0"),
                ("put", "1"),
                ("post", "finalize"),
            ],
        )
        for _, _, kwargs in session.calls:
            self.assertFalse(kwargs["allow_redirects"])
            self.assertEqual(kwargs["headers"], {"Authorization": "Bearer secret"})

    def test_retries_a_timed_out_idempotent_chunk_copy(self):
        session = FakeSession(
            [
                FakeResponse(200, self.inventory),
                requests.Timeout("upload response lost"),
                FakeResponse(204),
                FakeResponse(204),
                FakeResponse(200),
            ]
        )

        self.assertEqual(migrate_legacy_chunks(self.base_url, "secret", session=session), 1)
        self.assertEqual(
            [call[0] for call in session.calls],
            ["get", "put", "put", "put", "post"],
        )

    def test_does_not_finalize_after_a_chunk_failure(self):
        session = FakeSession(
            [FakeResponse(200, self.inventory), FakeResponse(500)]
        )

        with self.assertRaisesRegex(MigrationError, "HTTP 500"):
            migrate_legacy_chunks(self.base_url, "secret", session=session)
        self.assertEqual([call[0] for call in session.calls], ["get", "put"])

    def test_rejects_redirects_and_invalid_origins(self):
        redirected = FakeSession([FakeResponse(307)])
        with self.assertRaisesRegex(MigrationError, "redirected"):
            migrate_legacy_chunks(self.base_url, "secret", session=redirected)
        with self.assertRaisesRegex(MigrationError, "HTTPS origin"):
            migrate_legacy_chunks("http://catalog.example", "secret", session=redirected)

    def test_only_prunes_legacy_chunks_when_explicitly_requested(self):
        session = FakeSession(
            [
                FakeResponse(200, self.inventory),
                FakeResponse(204),
                FakeResponse(204),
                FakeResponse(200),
                FakeResponse(200),
            ]
        )

        self.assertEqual(
            migrate_legacy_chunks(
                self.base_url,
                "secret",
                prune_legacy=True,
                session=session,
            ),
            1,
        )
        self.assertTrue(session.calls[-1][1].endswith("/cleanup"))

    def test_filters_app_and_version(self):
        session = FakeSession([FakeResponse(200, self.inventory)])

        self.assertEqual(
            migrate_legacy_chunks(
                self.base_url,
                "secret",
                app_id="different-app",
                session=session,
            ),
            0,
        )
        self.assertEqual(len(session.calls), 1)

    def test_rejects_invalid_inventory(self):
        session = FakeSession(
            [FakeResponse(200, {"versions": [{"app_id": "../bad", "version": "1.0.0", "chunk_count": 1}]})]
        )

        with self.assertRaisesRegex(MigrationError, "invalid version record"):
            migrate_legacy_chunks(self.base_url, "secret", session=session)


if __name__ == "__main__":
    unittest.main()
