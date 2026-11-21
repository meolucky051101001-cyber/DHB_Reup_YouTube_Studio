import asyncio
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app as app_module
import database
from starlette.requests import Request
from starlette.responses import StreamingResponse


def _request(headers=()):
    return Request({
        "type": "http", "method": "GET", "path": "/acceptance-json",
        "headers": [(b"host", b"127.0.0.1:8686"), *headers], "scheme": "http",
        "server": ("127.0.0.1", 8686), "query_string": b"",
    })


def _call_next(payload):
    async def call_next(_request):
        async def chunks():
            yield json.dumps(payload).encode("utf-8")
        return StreamingResponse(chunks(), media_type="application/json")
    return call_next


class JsonEtagTests(unittest.TestCase):
    def _run(self, request, payload):
        async def run():
            with mock.patch.object(app_module, "collect_secret_values", return_value=[]):
                return await app_module.guard_local_api(request, _call_next(payload))
        return asyncio.run(run())

    def test_get_json_has_etag_and_matching_request_gets_304(self):
        payload = [{"id": 1, "status": "Pending", "api_key": "sample-key"}]
        first = self._run(_request(), payload)
        etag = first.headers["etag"]
        self.assertTrue(etag.startswith('W/"'))
        self.assertEqual(json.loads(first.body)[0]["api_key"], "***")

        second = self._run(_request([(b"if-none-match", etag.encode())]), payload)
        self.assertEqual(second.status_code, 304)
        self.assertEqual(second.body, b"")
        self.assertEqual(second.headers["etag"], etag)

    def test_changed_payload_returns_full_body(self):
        first = self._run(_request(), [{"id": 1, "status": "Pending"}])
        changed = self._run(
            _request([(b"if-none-match", first.headers["etag"].encode())]),
            [{"id": 1, "status": "Completed"}],
        )
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(json.loads(changed.body), [{"id": 1, "status": "Completed"}])
        self.assertNotEqual(changed.headers["etag"], first.headers["etag"])


class RedactPrefilterTests(unittest.TestCase):
    def test_plain_text_is_unchanged_but_known_secret_values_are_masked(self):
        self.assertEqual(database.redact_secrets("Tiêu đề video 1080p", []), "Tiêu đề video 1080p")
        self.assertEqual(database.redact_secrets("value AbC123xyz here", ["AbC123xyz"]), "value *** here")

    def test_patterns_still_apply(self):
        self.assertEqual(database.redact_secrets("Authorization: Bearer abc.def", []), "Authorization: Bearer ***")
        self.assertEqual(database.redact_secrets("http://u:pw@host/x", []), "http://u:***@host/x")
        self.assertEqual(database.redact_secrets("1.2.3.4:8080:user:pw", []), "1.2.3.4:8080:user:***")


class SettingsCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "app.sqlite"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO app_settings VALUES ('max_concurrent_threads', '2')")
        conn.commit()
        conn.close()
        self.original_path = database.APP_DB_PATH
        database.APP_DB_PATH = db_path
        database._invalidate_settings_cache()

    def tearDown(self):
        database.APP_DB_PATH = self.original_path
        database._invalidate_settings_cache()
        self.tmp.cleanup()

    def test_returns_copies_and_save_invalidates(self):
        first = database.get_settings()
        first["max_concurrent_threads"] = 99
        self.assertEqual(database.get_settings()["max_concurrent_threads"], 2)
        database.save_settings({"max_concurrent_threads": 4})
        self.assertEqual(database.get_settings()["max_concurrent_threads"], 4)

    def test_cache_expires_for_writes_from_other_processes(self):
        self.assertEqual(database.get_settings()["max_concurrent_threads"], 2)
        conn = sqlite3.connect(database.APP_DB_PATH)
        conn.execute("UPDATE app_settings SET value = '3'")
        conn.commit()
        conn.close()
        with mock.patch.object(database, "_SETTINGS_CACHE_TTL_SECONDS", 0):
            self.assertEqual(database.get_settings()["max_concurrent_threads"], 3)


if __name__ == "__main__":
    unittest.main()
