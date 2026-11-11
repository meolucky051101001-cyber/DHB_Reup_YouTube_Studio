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
