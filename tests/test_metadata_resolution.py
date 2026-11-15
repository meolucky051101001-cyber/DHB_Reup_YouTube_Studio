"""Pure metadata semantics and provenance, without DB or provider fixtures."""
import copy
import json
import unittest
from unittest import mock

import metadata_resolution as resolution
from upload_capabilities import UploadCapabilityError, capability_matrix


def confirmed(category='inherit', language=None, remix='keep'):
    return {'version': 1, 'studio_category_mode': category,
            'video_language': language or {'mode': 'keep'}, 'remix': remix}


class MetadataResolutionTests(unittest.TestCase):
    def resolve(self, mode='playwright', options=None, **channel):
        return resolution.resolve_metadata({'upload_mode': mode, **channel}, options)

    def codes(self, result):
        return [issue['code'] for issue in result['issues']]

    def test_category_value_precedence_is_independent_from_studio_intent(self):
        for category_mode in ('keep', 'apply'):
            for options, channel_value, value, source in (
                ({'category_id': '27'}, '24', '27', 'option'),
                ({'category_id': None}, '24', '24', 'channel'),
                ({'category_id': ''}, '24', '24', 'channel'),
                (None, '24', '24', 'channel'),
                ({}, None, '22', 'fallback'),
                ({}, '', '22', 'fallback'),
                ({'category_id': 27}, 24, '27', 'option'),
            ):
                with self.subTest(mode=category_mode, options=options, channel_value=channel_value):
                    result = self.resolve(options=options, default_category_id=channel_value,
                                          studio_category_mode=category_mode)
                    self.assertEqual(result['category'], {
                        'value': value, 'source': source, 'mode': category_mode, 'intent_source': 'channel'})
                    self.assertEqual(self.codes(result), ['UPLOAD_CATEGORY_UNSUPPORTED'] if category_mode == 'apply' else [])

    def test_option_category_intent_overrides_channel_but_inherit_uses_channel(self):
        for channel_mode in ('legacy_unconfirmed', 'keep', 'apply'):
            for option_mode in ('inherit', 'keep', 'apply'):
                with self.subTest(channel=channel_mode, option=option_mode):
                    result = self.resolve(options={
                        'category_id': '27', 'metadata_intent_json': json.dumps(confirmed(category=option_mode))},
                        default_category_id='24', studio_category_mode=channel_mode)
                    expected_mode = channel_mode if option_mode == 'inherit' else option_mode
                    self.assertEqual(result['category'], {
                        'value': '27', 'source': 'option', 'mode': expected_mode,
                        'intent_source': 'channel' if option_mode == 'inherit' else 'option'})
                    self.assertEqual(self.codes(result), {
                        'legacy_unconfirmed': ['UPLOAD_CATEGORY_CONFIRMATION_REQUIRED'],
                        'keep': [], 'apply': ['UPLOAD_CATEGORY_UNSUPPORTED'],
                    }[expected_mode])

    def test_api_category_always_applies_existing_value_regardless_of_studio_intent(self):
        for channel_mode in ('legacy_unconfirmed', 'keep', 'apply'):
            for option_mode in ('inherit', 'keep', 'apply'):
                with self.subTest(channel=channel_mode, option=option_mode):
                    result = self.resolve('youtube_api', {
                        'category_id': '27', 'metadata_intent_json': confirmed(category=option_mode)},
                        default_category_id='24', studio_category_mode=channel_mode)
                    self.assertEqual(result['category'], {
                        'value': '27', 'source': 'option', 'mode': 'apply', 'intent_source': 'api_default'})
                    self.assertEqual(result['issues'], [])

    def test_optionless_studio_legacy_requires_explicit_confirmation(self):
        for channel in ({}, {'studio_category_mode': None}, {'studio_category_mode': ''},
                        {'studio_category_mode': 'legacy_unconfirmed'}):
            with self.subTest(channel=channel):
                result = self.resolve(**channel)
                self.assertEqual(result['category']['value'], '22')
                self.assertEqual(result['category']['mode'], 'legacy_unconfirmed')
                self.assertEqual(self.codes(result), ['UPLOAD_CATEGORY_CONFIRMATION_REQUIRED'])
                self.assertIn('giữ thiết lập', result['issues'][0]['message'])
                self.assertIn('bộ tùy chọn', result['issues'][0]['message'])

    def test_legacy_language_never_becomes_an_apply_request_or_leaks_raw_values(self):
        for language in (None, '', 'vi', 'en-US', 'private-sensitive-value', {'token': 'sensitive'}):
            with self.subTest(language=language):
                result = self.resolve(options={'language': language, 'disable_shorts_remix': 0},
                                      studio_category_mode='keep')
                self.assertEqual(result['video_language'], {
                    'value': None, 'source': 'legacy_option', 'mode': 'legacy_unconfirmed'})
                self.assertEqual(result['issues'], [])
                self.assertTrue(result['warnings'])
                self.assertNotIn('sensitive', json.dumps(result))

    def test_legacy_remix_false_is_unconfirmed_and_true_remains_restrictive(self):
        for value in (None, '', False, 0, '0', 'false', True, 1, '1', 'true'):
            for mode in ('youtube_api', 'playwright'):
                with self.subTest(value=value, mode=mode):
                    result = self.resolve(mode, {'disable_shorts_remix': value}, studio_category_mode='keep')
                    restrictive = value in (True, 1, '1', 'true')
                    self.assertEqual(result['remix'], {
                        'value': True if restrictive else None, 'source': 'legacy_option',
                        'mode': 'disable' if restrictive else 'legacy_unconfirmed'})
                    self.assertEqual(self.codes(result), ['UPLOAD_REMIX_UNSUPPORTED'] if restrictive else [])

    def test_canonical_keep_ignores_legacy_language_and_mirrored_remix(self):
        for mode in ('youtube_api', 'playwright'):
            result = self.resolve(mode, {'language': 'vi', 'disable_shorts_remix': 0,
                                         'metadata_intent_json': confirmed()}, studio_category_mode='keep')
            for feature in ('video_language', 'remix'):
                self.assertEqual(result[feature], {'value': None, 'source': 'option', 'mode': 'keep'})
            self.assertEqual(result['issues'], [])
            self.assertEqual(result['warnings'], [])

    def test_canonical_language_and_remix_requests_are_independently_gated(self):
        for mode in ('youtube_api', 'playwright'):
            for tag in ('vi', 'en-US', 'zh-Hant-TW'):
                with self.subTest(mode=mode, language=tag):
                    result = self.resolve(mode, {'metadata_intent_json': confirmed(
                        category='keep', language={'mode': 'apply', 'value': tag}, remix='disable')},
                        studio_category_mode='keep')
                    self.assertEqual(result['video_language'], {'value': tag, 'source': 'option', 'mode': 'apply'})
                    self.assertEqual(result['remix'], {'value': True, 'source': 'option', 'mode': 'disable'})
                    self.assertEqual(self.codes(result), ['UPLOAD_VIDEO_LANGUAGE_UNSUPPORTED', 'UPLOAD_REMIX_UNSUPPORTED'])

    def test_registry_revision_and_enablement_drive_the_same_resolved_contract(self):
        matrix = capability_matrix()
        matrix['playwright']['revision'] = 'controlled-offline-revision'
        for feature in ('category', 'video_language', 'remix'):
            matrix['playwright']['features'][feature]['enabled'] = True
        options = {'metadata_intent_json': confirmed(category='apply',
                   language={'mode': 'apply', 'value': 'en-US'}, remix='disable')}
        before = self.resolve(options=options)
        self.assertEqual(len(before['issues']), 3)
        with mock.patch.object(resolution, 'capability_matrix', return_value=matrix):
            result = self.resolve(options=options)
        self.assertEqual(result['capability_revision'], 'controlled-offline-revision')
        self.assertEqual(result['issues'], [])
        for feature in ('category', 'video_language', 'remix'):
            self.assertEqual(before[feature], result[feature])

    def test_false_zero_and_invalid_category_values_never_silently_fall_back(self):
        for value in (False, True, 0, '0', 1000, '00022', [], {}, 'private-sensitive-value'):
            with self.subTest(value=value):
                with self.assertRaises(UploadCapabilityError) as raised:
                    self.resolve('youtube_api', {'category_id': value}, default_category_id='24')
                self.assertEqual(raised.exception.code, 'UPLOAD_CATEGORY_INVALID')
                self.assertNotIn('sensitive', str(raised.exception))

    def test_invalid_intent_backend_mode_and_flag_are_safe_structured_failures(self):
        for raw in (None, '', 'not-json-sensitive', [], {'secret': 'sensitive'},
                    {**confirmed(), 'version': 2}, {**confirmed(), 'remix': False}):
            with self.subTest(raw=raw):
                with self.assertRaises(UploadCapabilityError) as raised:
                    self.resolve(options={'metadata_intent_json': raw}, studio_category_mode='keep')
                self.assertEqual(raised.exception.code, 'UPLOAD_METADATA_INTENT_INVALID')
                self.assertNotIn('sensitive', str(raised.exception))
        result = self.resolve('private-sensitive-backend', {'metadata_intent_json': confirmed()})
        self.assertEqual(result['backend'], 'unknown')
        self.assertEqual(result['capability_revision'], '')
        self.assertEqual(self.codes(result), ['UPLOAD_BACKEND_UNSUPPORTED'])
        self.assertNotIn('sensitive', json.dumps(result))
        for invalid in (False, 0, [], {}, 'private-sensitive-mode'):
            with self.subTest(mode=invalid):
                with self.assertRaises(UploadCapabilityError) as raised:
                    self.resolve(studio_category_mode=invalid)
                self.assertEqual(raised.exception.code, 'UPLOAD_CATEGORY_INTENT_INVALID')
                self.assertNotIn('sensitive', str(raised.exception))
        for invalid in (2, -1, 1.0, [], {}, 'private-sensitive-flag'):
            with self.subTest(flag=invalid):
                with self.assertRaises(UploadCapabilityError) as raised:
                    self.resolve(options={'disable_shorts_remix': invalid}, studio_category_mode='keep')
                self.assertEqual(raised.exception.code, 'UPLOAD_METADATA_FLAG_INVALID')
                self.assertNotIn('sensitive', str(raised.exception))

    def test_resolution_never_mutates_inputs_or_includes_credentials(self):
        channel = {'upload_mode': 'playwright', 'studio_category_mode': 'keep',
                   'default_category_id': '24', 'oauth_token_json': 'secret-token',
                   'cookie': 'secret-cookie', 'proxy': 'secret-proxy'}
        options = {'category_id': '27', 'metadata_intent_json': confirmed(),
                   'note': 'private note', 'language': 'vi', 'disable_shorts_remix': 0}
        before = copy.deepcopy((channel, options))
        result = resolution.resolve_metadata(channel, options)
        result['category']['value'] = '23'
        self.assertEqual((channel, options), before)
        serialized = json.dumps(result)
        self.assertNotIn('secret', serialized)
        self.assertNotIn('private note', serialized)


if __name__ == '__main__':
    unittest.main()
