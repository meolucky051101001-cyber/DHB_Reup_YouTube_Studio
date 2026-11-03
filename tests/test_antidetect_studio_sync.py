"""Test tính năng 'Đồng bộ Antidetect với YouTube Studio' (2026-11-02):

- sync_antidetect_studio_channels: mở từng Profile Antidetect (được mock qua
  probe_profile_login), đọc kênh Studio và đồng bộ về youtube_channels.
- POST /api/antidetect/sync nhận {"scan_channels": true} để chạy luồng đầy đủ.
- Không mở trình duyệt thật, không gọi Antidetect thật (stub + mock hoàn toàn).
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import database
from fastapi.testclient import TestClient

_OLD_DB = database.APP_DB_PATH
_OLD_DATA = database.APP_DATA_DIR
_OLD_TEMP = database.APP_TEMP_DIR
_TMP = None
app_module = None
client = None


class _StubAntidetectClient:
    """Thay AntidetectClient để endpoint không gọi ra máy chủ Antidetect thật."""

    def __init__(self, *args, **kwargs):
        pass

    def list_profiles(self):
        return [{"id": "ad_001", "name": "AD Alpha", "status": "stopped", "proxy": None}]


_PROBE_RESULT_OK = {
    "logged_in": True,
    "message": "Profile Antidetect đã đăng nhập Google (a@x.com), phát hiện 2 kênh YouTube.",
    "email": "a@x.com",
    "channels": [
        {"name": "Kênh Alpha", "handle": "@alpha", "email": "a@x.com", "channel_id": "UC_ALPHA_0001"},
        {"name": "Kênh Beta", "handle": "@beta", "email": "a@x.com", "channel_id": "UC_BETA_0002"},
    ],
}


def _seed_profile(name: str, ad_id: str):
    conn = database.get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO chrome_profiles (name, path, source, antidetect_profile_id) "
        "VALUES (?, ?, 'Antidetect', ?)",
        (name, f"antidetect://{ad_id}", ad_id),
    )
    conn.commit()
    conn.close()


def _channels_of(profile_name: str):
    conn = database.get_conn()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM youtube_channels WHERE profile_name = ? ORDER BY id ASC", (profile_name,)
    ).fetchall()]
    conn.close()
    return rows


def _login_status(name: str) -> str:
    conn = database.get_conn()
    row = conn.execute(
        "SELECT login_status FROM chrome_profiles WHERE name = ?", (name,)
    ).fetchone()
    conn.close()
    return (row["login_status"] if row else "") or ""


def setUpModule():
    global _TMP, app_module, client
    _TMP = Path(tempfile.mkdtemp(prefix="dhb_test_adsync_"))
    database.APP_DB_PATH = _TMP / "db.sqlite"
    database.APP_DATA_DIR = _TMP / "data"
    database.APP_TEMP_DIR = _TMP / "temp"
    database.init_db()
    import app
    app_module = app
    client = TestClient(app_module.app, base_url="http://127.0.0.1:8686")


def tearDownModule():
    database.APP_DB_PATH = _OLD_DB
    database.APP_DATA_DIR = _OLD_DATA
    database.APP_TEMP_DIR = _OLD_TEMP
    shutil.rmtree(_TMP, ignore_errors=True)


def _auth_headers() -> dict:
    token = client.get("/api/auth/token").json()["token"]
    return {"x-dhb-token": token, "Origin": "http://127.0.0.1:8686"}


class StudioSyncTests(unittest.TestCase):
    def test_sync_creates_channels_with_channel_id(self):
        _seed_profile("AD Direct1", "ad_d01")
        with mock.patch.object(app_module, "probe_profile_login", return_value=dict(_PROBE_RESULT_OK)):
            res = app_module.sync_antidetect_studio_channels(
                [{"name": "AD Direct1", "antidetect_profile_id": "ad_d01"}]
            )
        self.assertEqual(res["scanned"], 1)
        self.assertEqual(res["logged_in"], 1)
        self.assertEqual(res["channels_added"], 2)

        rows = _channels_of("AD Direct1")
        self.assertEqual(len(rows), 2)
        alpha = next(r for r in rows if r["name"] == "Kênh Alpha")
        self.assertEqual(alpha["channel_id"], "UC_ALPHA_0001")
        self.assertEqual(alpha["upload_mode"], "playwright")
        self.assertEqual(alpha["profile_path"], "antidetect://ad_d01")
        self.assertEqual(alpha["emulate_mobile"], 0)
        self.assertEqual(_login_status("AD Direct1"), "Đã đăng nhập")

    def test_sync_upserts_without_duplicates(self):
        with mock.patch.object(app_module, "probe_profile_login", return_value=dict(_PROBE_RESULT_OK)):
            app_module.sync_antidetect_studio_channels(
                [{"name": "AD Direct1", "antidetect_profile_id": "ad_d01"}]
            )
            res = app_module.sync_antidetect_studio_channels(
                [{"name": "AD Direct1", "antidetect_profile_id": "ad_d01"}]
            )
        self.assertEqual(res["channels_added"], 0)
        self.assertEqual(res["channels_updated"], 2)
        self.assertEqual(len(_channels_of("AD Direct1")), 2, "Chạy lại không được nhân đôi kênh")

    def test_sync_records_not_logged_in(self):
        _seed_profile("AD Beta", "ad_002")
        probe = {"logged_in": False, "message": "Profile Antidetect này chưa đăng nhập tài khoản Google/YouTube.", "channels": []}
        with mock.patch.object(app_module, "probe_profile_login", return_value=probe):
            res = app_module.sync_antidetect_studio_channels(
                [{"name": "AD Beta", "antidetect_profile_id": "ad_002"}]
            )
        entry = res["profiles"][0]
        self.assertFalse(entry["logged_in"])
        self.assertEqual(res["channels_added"], 0)
        self.assertEqual(_channels_of("AD Beta"), [])
        self.assertEqual(_login_status("AD Beta"), "Chưa đăng nhập")

    def test_sync_continues_after_probe_error(self):
        _seed_profile("AD Err1", "ad_e01")
        _seed_profile("AD Ok2", "ad_o02")
        with mock.patch.object(
            app_module, "probe_profile_login",
            side_effect=[RuntimeError("Không kết nối được CDP tới profile"), dict(_PROBE_RESULT_OK)],
        ):
            res = app_module.sync_antidetect_studio_channels([
                {"name": "AD Err1", "antidetect_profile_id": "ad_e01"},
                {"name": "AD Ok2", "antidetect_profile_id": "ad_o02"},
            ])
        self.assertTrue(res["profiles"][0]["error"])
        self.assertTrue(res["profiles"][1]["logged_in"])
        self.assertEqual(len(_channels_of("AD Ok2")), 2)

    def test_endpoint_default_keeps_list_sync_only(self):
        with mock.patch.object(app_module, "AntidetectClient", _StubAntidetectClient):
            res = client.post("/api/antidetect/sync", headers=_auth_headers())
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("studio", res.json(), "Không truyền scan_channels thì KHÔNG quét Studio")

    def test_endpoint_with_scan_flag_runs_studio_sync(self):
        with mock.patch.object(app_module, "AntidetectClient", _StubAntidetectClient), \
                mock.patch.object(app_module, "probe_profile_login", return_value=dict(_PROBE_RESULT_OK)):
            res = client.post("/api/antidetect/sync", json={"scan_channels": True}, headers=_auth_headers())
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertIn("studio", body)
        self.assertEqual(body["studio"]["scanned"], 1)
        self.assertEqual(body["studio"]["logged_in"], 1)
        self.assertTrue(body["studio"]["profiles"][0]["logged_in"])


if __name__ == "__main__":
    unittest.main()
