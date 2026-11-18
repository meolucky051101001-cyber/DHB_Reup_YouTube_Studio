"""Channel routing must survive Requests' environment merging without network I/O."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests
import youtube_uploader as uploader


class ChannelSessionPolicyTests(unittest.TestCase):
    def test_environment_cannot_replace_channel_route_or_force_direct(self):
        for variable in ('HTTPS_PROXY', 'https_proxy', 'ALL_PROXY', 'all_proxy'):
            for bypass in ('', '*'):
                with self.subTest(variable=variable, bypass=bypass), mock.patch.dict(
                    os.environ, {variable: 'http://environment.invalid:9999', 'NO_PROXY': bypass}, clear=True
                ), uploader._http_session_for_channel({'proxy': 'channel.invalid:8080'}) as session:
                    settings = session.merge_environment_settings('https://www.googleapis.com/', {}, None, None, None)
                    self.assertEqual(settings['proxies'], {
                        'http': 'http://channel.invalid:8080', 'https': 'http://channel.invalid:8080'})

    def test_channel_without_proxy_stays_direct(self):
        with mock.patch.dict(os.environ, {'HTTPS_PROXY': 'http://environment.invalid:9999'}, clear=True):
            with uploader._http_session_for_channel({}) as session:
                settings = session.merge_environment_settings('https://oauth2.googleapis.com/token', {}, None, None, None)
                self.assertEqual(settings['proxies'], {})

    def test_custom_ca_bundle_remains_enabled(self):
        for env, expected in (({}, True), ({'CURL_CA_BUNDLE': 'curl-ca.pem'}, 'curl-ca.pem'),
                              ({'REQUESTS_CA_BUNDLE': 'requests-ca.pem', 'CURL_CA_BUNDLE': 'curl-ca.pem'},
                               'requests-ca.pem')):
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True):
                with uploader._http_session_for_channel({}) as session:
                    settings = session.merge_environment_settings('https://www.googleapis.com/', {}, None, None, None)
                    self.assertEqual(settings['verify'], expected)

    def test_netrc_cannot_override_explicit_oauth_bearer(self):
        with tempfile.TemporaryDirectory() as folder:
            netrc = Path(folder) / 'test.netrc'
            netrc.write_text('machine www.googleapis.com login fixture password offline\n', encoding='utf-8')
            with mock.patch.dict(os.environ, {'NETRC': str(netrc)}, clear=True):
                with uploader._http_session_for_channel({}) as session:
                    prepared = session.prepare_request(requests.Request(
                        'GET', 'https://www.googleapis.com/youtube/v3/channels',
                        headers={'Authorization': 'Bearer offline-sentinel'}))
                    self.assertEqual(prepared.headers['Authorization'], 'Bearer offline-sentinel')

    def test_invalid_proxy_does_not_allocate_a_session(self):
        with mock.patch.object(uploader.requests, 'Session') as factory:
            with self.assertRaises(ValueError):
                uploader._http_session_for_channel({'proxy': 'host:not-a-port'})
            factory.assert_not_called()

    def test_internal_uploader_requires_owned_channel_session(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'fixture.mp4'
            source.write_bytes(b'not-empty')
            with mock.patch.object(uploader, 'require_capabilities'), \
                    mock.patch.object(uploader.requests, 'Session') as factory, \
                    mock.patch.object(uploader, '_refresh_google_access_token') as refresh:
                with self.assertRaisesRegex(ValueError, 'session'):
                    uploader._upload_via_youtube_api_with_session(str(source), {}, {})
                factory.assert_not_called()
                refresh.assert_not_called()


if __name__ == '__main__':
    unittest.main()
