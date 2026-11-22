import threading
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import requests

import connect_youtube_oauth as oauth


class DesktopOAuthTests(unittest.TestCase):
    def test_loopback_uses_pkce_and_rejects_wrong_state(self):
        responses = []
        threads = []

        def simulate_browser(url):
            params = parse_qs(urlsplit(url).query)
            redirect = params["redirect_uri"][0]
            self.assertEqual(params["code_challenge_method"], ["S256"])
            self.assertEqual(params["access_type"], ["offline"])

            def callback():
                for state in ("wrong", params["state"][0]):
                    response = requests.get(redirect, params={"state": state, "code": "fixture"}, timeout=5)
                    responses.append(response.status_code)

            worker = threading.Thread(target=callback)
            threads.append(worker)
            worker.start()
            return True

        with mock.patch.object(oauth.webbrowser, "open", side_effect=simulate_browser):
            code, redirect, verifier = oauth.authorize("test.apps.googleusercontent.com")
        threads[0].join(5)
        self.assertEqual(responses, [400, 200])
        self.assertEqual(code, "fixture")
        self.assertTrue(redirect.startswith("http://127.0.0.1:"))
        self.assertGreaterEqual(len(verifier), 43)

    def test_refresh_and_exact_identity_are_required_before_saving(self):
        http = mock.Mock()
        http.post.side_effect = [
            mock.Mock(ok=True, json=mock.Mock(return_value={
                "refresh_token": "refresh", "scope": " ".join(oauth.SCOPES),
            })),
            mock.Mock(ok=True, json=mock.Mock(return_value={"access_token": "fresh"})),
        ]
        http.get.return_value = mock.Mock(ok=True, json=mock.Mock(return_value={"items": [{"id": "other"}]}))
        with mock.patch.object(oauth, "authorize", return_value=("code", "loopback", "verifier")):
            with self.assertRaisesRegex(RuntimeError, "đúng Channel ID"):
                oauth.authorized_credentials(http, "client", "secret", "expected")
        self.assertEqual(http.post.call_args.kwargs["data"]["grant_type"], "refresh_token")
        self.assertEqual(http.get.call_args.kwargs["headers"]["Authorization"], "Bearer fresh")

    def test_parse_desktop_client_json_and_antidetect_browser_isolation(self):
        cid, csec = oauth.parse_desktop_client_json(
            '{"installed":{"client_id":"123-abc.apps.googleusercontent.com","client_secret":"GOCSPX-secret"}}'
        )
        self.assertEqual(cid, "123-abc.apps.googleusercontent.com")
        self.assertEqual(csec, "GOCSPX-secret")

        with self.assertRaisesRegex(RuntimeError, "Desktop app"):
            oauth.parse_desktop_client_json('{"web":{"client_id":"123.apps.googleusercontent.com","client_secret":"x"}}')

        # Antidetect-bound channel must NEVER fall back to system webbrowser.open
        with mock.patch.object(oauth.webbrowser, "open") as sys_open:
            with self.assertRaisesRegex(RuntimeError, "thiếu ID liên kết"):
                oauth.open_url_in_channel_browser(
                    {"profile_path": ""},
                    "https://accounts.google.com/o/oauth2/v2/auth",
                    profile_binding={"source": "Antidetect", "antidetect_profile_id": ""},
                )
            sys_open.assert_not_called()

    def test_bound_antidetect_channel_uses_open_url_without_system_browser_fallback(self):
        url = "https://accounts.google.com/o/oauth2/v2/auth?client_id=fixture"
        client = mock.Mock()
        client.open_url.return_value = {"port": 9222, "launched_now": True}

        with mock.patch("antidetect_client.AntidetectClient", return_value=client), \
                mock.patch.object(oauth.webbrowser, "open") as system_browser:
            result = oauth.open_url_in_channel_browser(
                {"antidetect_profile_id": "ad-bound"},
                url,
            )

        self.assertEqual(result, {
            "browser": "antidetect",
            "antidetect_profile_id": "ad-bound",
            "port": 9222,
        })
        client.open_url.assert_called_once_with("ad-bound", url)
        client.start_profile.assert_not_called()
        system_browser.assert_not_called()

    def test_same_profile_client_isolation_exposes_no_secrets(self):
        import sqlite3
        import app as studio_app

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE youtube_channels (id INTEGER PRIMARY KEY, name TEXT, profile_name TEXT, oauth_token_json TEXT)"
        )
        conn.execute(
            "INSERT INTO youtube_channels VALUES (66, 'Chị Đẹp Biết Tuốt', 'Profile 5', "
            "'{\"client_id\":\"879-xyz.apps.googleusercontent.com\",\"client_secret\":\"sec-5\",\"refresh_token\":\"rt-66\"}')"
        )
        conn.execute("INSERT INTO youtube_channels VALUES (65, 'Vô tri mỗi ngày', 'Profile 5', '')")
        conn.execute("INSERT INTO youtube_channels VALUES (62, 'Other Profile Channel', 'Profile 2', '')")

        found_p5 = studio_app._find_same_profile_oauth_client(conn, "Profile 5", 65)
        self.assertIsNotNone(found_p5)
        self.assertEqual(found_p5["source_id"], 66)
        self.assertEqual(found_p5["client_id"], "879-xyz.apps.googleusercontent.com")

        found_p2 = studio_app._find_same_profile_oauth_client(conn, "Profile 2", 62)
        self.assertIsNone(found_p2)
        conn.close()


if __name__ == "__main__":
    unittest.main()
