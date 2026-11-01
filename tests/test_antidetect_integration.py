import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import antidetect_client as ac
import database as db
import youtube_uploader as yu


class _Handler(BaseHTTPRequestHandler):
    profiles = [{
        "id": "p-1",
        "name": "Profile Test",
        "status": "stopped",
        "port": None,
        "proxy": {"enabled": True, "host": "10.0.0.1", "port": 3128, "password": "secret"},
    }]
    expected_auth = "Bearer test-key"
    auth_seen = []
    start_calls = []

    def log_message(self, *args):
        pass

    def _json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"application": "antidetect-python", "instance_id": "test"})
            return
        auth = self.headers.get("Authorization", "")
        type(self).auth_seen.append(auth)
        if auth != self.expected_auth:
            self._json(401, {"detail": "bad key"})
            return
        if self.path == "/api/profiles":
            self._json(200, type(self).profiles)
            return
        self._json(404, {"detail": "not found"})

    def do_POST(self):
        auth = self.headers.get("Authorization", "")
        type(self).auth_seen.append(auth)
        if auth != self.expected_auth:
            self._json(401, {"detail": "bad key"})
            return
        if self.path.startswith("/api/profiles/p-1/start"):
            type(self).start_calls.append(self.path)
            self._json(200, {
                "success": True,
                "message": "Profile đang chạy",
                "port": 9222,
            })
            return
        self._json(404, {"detail": "not found"})


class AntidetectClientHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _Handler.auth_seen.clear()
        _Handler.start_calls.clear()

    def _client(self, key="test-key"):
        cfg = ac.AntidetectConfig(
            home=Path(tempfile.gettempdir()),
            data_dir=Path(tempfile.gettempdir()),
            base_url=self.base_url,
            access_key=key,
        )
        return ac.AntidetectClient(cfg)

    def test_status_and_start_use_bearer_token(self):
        client = self._client()
        status = client.status()
        self.assertTrue(status["connected"])
        self.assertEqual(status["profile_count"], 1)

        res = client.start_profile("p-1")
        self.assertEqual(res["port"], 9222)
        self.assertEqual(_Handler.start_calls[0], "/api/profiles/p-1/start")
        self.assertIn("Bearer test-key", _Handler.auth_seen)

    def test_invalid_key_has_clear_error(self):
        client = self._client(key="wrong-key")
        with self.assertRaises(ac.AntidetectError) as ctx:
            client.start_profile("p-1")
        self.assertIn("khóa truy cập", str(ctx.exception).lower())

    def test_unreachable_base_url_is_reported(self):
        cfg = ac.AntidetectConfig(
            home=Path(tempfile.gettempdir()),
            data_dir=Path(tempfile.gettempdir()),
            base_url="http://127.0.0.1:1",
            access_key="test-key",
        )
        status = ac.AntidetectClient(cfg).status()
        self.assertFalse(status["connected"])
        self.assertIn("không phản hồi", status["error"].lower())

    def test_server_pid_port_is_discovered_when_base_url_empty(self):
        port = self.server.server_address[1]
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "server.pid").write_text(
                json.dumps({"pid": 123, "port": port, "instance_id": "test"}),
                encoding="utf-8",
            )
            cfg = ac.AntidetectConfig(
                home=Path(tmp),
                data_dir=Path(tmp),
                base_url="",
                access_key="test-key",
            )
            client = ac.AntidetectClient(cfg)
            self.assertEqual(client.resolve_base_url(), self.base_url)
            self.assertTrue(client.status()["connected"])


class AntidetectConfigTests(unittest.TestCase):
    def test_env_overrides_are_loaded_without_secrets_in_repr(self):
        env = {
            "ANTIDETECT_HOME": str(Path(tempfile.gettempdir()) / "adh"),
            "ANTIDETECT_DATA_DIR": str(Path(tempfile.gettempdir()) / "add"),
            "ANTIDETECT_BASE_URL": "http://127.0.0.1:8123/",
            "ANTIDETECT_ACCESS_KEY": "super-secret-key",
        }
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch("database.get_settings", return_value={}):
            cfg = ac.AntidetectConfig.load()
        self.assertEqual(cfg.base_url, "http://127.0.0.1:8123")
        self.assertEqual(cfg.access_key, "super-secret-key")
        self.assertNotIn("super-secret-key", repr(cfg))

    def test_invalid_base_url_is_rejected(self):
        with self.assertRaises(ac.AntidetectError):
            ac._normalize_base_url("127.0.0.1:8000")


class SyncDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.APP_DB_PATH
        db.APP_DB_PATH = Path(self.tmp.name) / "test.sqlite"
        conn = db.get_conn()
        conn.execute(
            """
            CREATE TABLE chrome_profiles (
                name TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                proxy TEXT DEFAULT '',
                source TEXT DEFAULT 'DHB',
                note TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                login_status TEXT DEFAULT '',
                last_login_check TEXT DEFAULT '',
                antidetect_profile_id TEXT DEFAULT ''
            )
            """
        )
        conn.execute(
            "INSERT INTO chrome_profiles (name, path, proxy, source) VALUES ('Alpha', 'C:/local/alpha', 'old:1', 'DHB')"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        db.APP_DB_PATH = self.old_path
        self.tmp.cleanup()

    def _rows(self):
        conn = db.get_conn()
        rows = {r["name"]: dict(r) for r in conn.execute("SELECT * FROM chrome_profiles").fetchall()}
        conn.close()
        return rows

    def test_sync_adds_without_overwriting_local_and_masks_secret(self):
        remote = [
            {
                "id": "ad-1", "name": "Alpha", "status": "running",
                "proxy": {"enabled": True, "host": "1.2.3.4", "port": 8080, "password": "proxy-secret"},
            },
            {"id": "ad-2", "name": "Beta", "status": "stopped", "proxy": {"enabled": False}},
        ]
        res = db.sync_antidetect_profiles(remote)
        self.assertEqual(res["added"], 2)
        self.assertEqual(res["updated"], 0)

        rows = self._rows()
        self.assertEqual(rows["Alpha"]["source"], "DHB")
        self.assertEqual(rows["Alpha"]["proxy"], "old:1")

        ad_name = next(name for name, row in rows.items() if row["antidetect_profile_id"] == "ad-1")
        self.assertNotEqual(ad_name, "Alpha")
        self.assertEqual(rows[ad_name]["source"], "Antidetect")
        self.assertEqual(rows[ad_name]["path"], "antidetect://ad-1")
        self.assertEqual(rows[ad_name]["proxy"], "1.2.3.4:8080")
        self.assertNotIn("proxy-secret", json.dumps(rows, ensure_ascii=False))
        self.assertEqual(rows["Beta"]["antidetect_profile_id"], "ad-2")

    def test_second_sync_updates_in_place_and_reports_missing(self):
        remote = [{"id": "ad-1", "name": "Alpha", "proxy": {"enabled": True, "host": "1.2.3.4", "port": 8080}}]
        db.sync_antidetect_profiles(remote)
        res = db.sync_antidetect_profiles(remote)
        self.assertEqual(res["added"], 0)
        self.assertEqual(res["updated"], 1)

        one = db.sync_antidetect_profiles([])
        self.assertEqual(len(one["missing"]), 1)
        rows = self._rows()
        self.assertEqual(len([r for r in rows.values() if r["antidetect_profile_id"] == "ad-1"]), 1)


class UploaderAntidetectTests(unittest.TestCase):
    def test_channel_id_resolution_prefers_explicit_value(self):
        self.assertEqual(
            yu._channel_antidetect_id({"antidetect_profile_id": "ad-9", "profile_name": "X"}),
            "ad-9",
        )

    def test_api_upload_is_blocked_for_antidetect_profile(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            video_path = f.name
        try:
            with self.assertRaises(RuntimeError) as ctx:
                yu.upload_video_to_youtube(
                    {"title": "Video test", "output_video_path": video_path},
                    {
                        "name": "Kênh A",
                        "handle": "@kenha",
                        "channel_id": "UC1234567890abcdefghijkl",
                        "upload_mode": "youtube_api",
                        "antidetect_profile_id": "ad-1",
                    },
                )
            self.assertIn("Antidetect", str(ctx.exception))
            self.assertIn("YouTube Data API", str(ctx.exception))
        finally:
            os.remove(video_path)

    def test_browser_session_uses_lease_and_never_local_chrome(self):
        created = {}

        class FakeLease:
            def __init__(self, profile_id, stop_if_started=True):
                created["profile_id"] = profile_id
                created["stop_if_started"] = stop_if_started
                self.tracked_context = object()
                self.page = object()
                created["tracked_context"] = self.tracked_context
                self.closed = False

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.closed = True
                created["closed"] = True

        with mock.patch.object(yu, "AntidetectBrowserLease", FakeLease), \
                mock.patch.object(yu, "add_log"), \
                mock.patch.object(yu, "find_system_chrome_exe") as chrome:
            session = yu._StudioBrowserSession(
                channel={"name": "Kênh A"},
                prof_name="AD Profile",
                prof_path="antidetect://ad-1",
                chrome_exe=None,
                is_mobile_mode=True,
                hide_browser=True,
                antidetect_profile_id="ad-1",
            )
            with session as opened:
                self.assertEqual(opened.init_scripts, [])
                self.assertFalse(opened.is_mobile_mode)
                self.assertIs(opened.context, created["tracked_context"])

        chrome.assert_not_called()
        self.assertEqual(created["profile_id"], "ad-1")
        self.assertTrue(created["stop_if_started"])
        self.assertTrue(created["closed"])

    def test_open_login_dispatches_to_antidetect_without_chrome_lookup(self):
        class FakeClient:
            def open_login(self, profile_id):
                return {"ok": True, "port": 9222}

        with mock.patch.object(yu, "AntidetectClient", FakeClient), \
                mock.patch.object(yu, "add_log"), \
                mock.patch.object(yu, "find_system_chrome_exe") as chrome:
            res = yu.open_profile_for_login({
                "name": "Kênh A",
                "profile_name": "AD Profile",
                "profile_path": "antidetect://ad-1",
                "antidetect_profile_id": "ad-1",
            })

        self.assertTrue(res["antidetect"])
        self.assertEqual(res["port"], 9222)
        chrome.assert_not_called()

    def test_open_login_fails_closed_for_pseudo_path_without_id(self):
        with mock.patch.object(yu, "resolve_antidetect_profile_id", return_value=""), \
                mock.patch.object(yu, "add_log"):
            with self.assertRaises(RuntimeError) as ctx:
                yu.open_profile_for_login({
                    "name": "Kênh A",
                    "profile_name": "AD Profile",
                    "profile_path": "antidetect://",
                })
        self.assertIn("chưa có ID liên kết", str(ctx.exception))

    def test_probe_dispatch_extracts_id_from_pseudo_path(self):
        with mock.patch.object(yu, "_probe_antidetect_login", return_value={"logged_in": True}) as probe:
            res = yu.probe_profile_login("antidetect://ad-77", "", grab_channels=False)
        self.assertTrue(res["logged_in"])
        probe.assert_called_once_with("ad-77", grab_channels=False)


if __name__ == "__main__":
    unittest.main()
