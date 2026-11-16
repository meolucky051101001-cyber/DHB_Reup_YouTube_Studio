"""Synthetic spoken-language contract tests; no captured Studio DOM or provider I/O."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import unittest

from studio_metadata_contract import (
    LanguageControl, LanguageOption, SavedLanguage, StudioLanguageContract,
    StudioMetadataError, verify_saved_metadata,
)
from tests.test_studio_metadata_contract import FixtureAdapter, contract as category_contract
from upload_evidence import _normalize_event


FIXTURE_ROOT = Path(__file__).parent / 'fixtures' / 'studio_metadata'
LANGUAGE_MAP = {locale: {'vi': 'spoken-vi', 'en': 'spoken-en', 'en-US': 'spoken-en-us'}
                for locale in ('vi', 'en')}
VIDEO_ID = 'dQw4w9WgXcQ'


class LanguageFixtureAdapter:
    def __init__(self, locale='en', value='spoken-en', saved=None):
        self.locale, self.value, self.saved = locale, value, saved
        self.options = tuple(LanguageOption(value) for value in LANGUAGE_MAP['en'].values())
        self.calls = []

    def reveal_video_language(self): self.calls.append(('reveal',))

    def read_video_language(self):
        self.calls.append(('read',))
        return LanguageControl(self.locale, 'video_language', 1, 1, True, True, self.value, self.options)

    def select_video_language(self, value):
        self.calls.append(('select', value))
        self.value = value

    def read_saved_video_language(self, video_id):
        self.calls.append(('saved', video_id))
        return self.saved if self.saved is not None else SavedLanguage(video_id, self.locale, self.value)


class SyntheticLanguageBrowserAdapter:
    """All selectors target authored test fixtures, not YouTube Studio."""
    def __init__(self, page): self.page = page

    def reveal_video_language(self):
        buttons = self.page.locator('[data-action="show-more"]')
        if buttons.count() != 1: raise ValueError('Synthetic reveal ambiguous')
        buttons.click(timeout=500)
        self.page.locator('[data-details]').wait_for(state='visible', timeout=1000)

    def read_video_language(self):
        sections = self.page.locator('section[data-studio-section="video_language"]')
        controls = sections.locator('select[data-semantic="video-language"]')
        locale = self.page.locator('html').get_attribute('lang')
        if sections.count() != 1 or controls.count() != 1:
            return LanguageControl(locale, 'video_language', sections.count(), controls.count(), False, False, '', ())
        options = tuple(LanguageOption(item['value'], item['enabled']) for item in controls.locator('option').evaluate_all(
            '(items) => items.map(item => ({value:item.value,enabled:!item.disabled}))'))
        return LanguageControl(locale, 'video_language', 1, 1, controls.is_visible(),
                               controls.is_enabled(), controls.input_value(), options)

    def select_video_language(self, value):
        controls = self.page.locator('section[data-studio-section="video_language"] select[data-semantic="video-language"]')
        if controls.count() != 1: raise ValueError('Synthetic language ambiguous')
        controls.select_option(value=value, timeout=500)

    def read_saved_video_language(self, video_id):
        self.page.goto(f'http://127.0.0.1:8687/video/{video_id}', wait_until='domcontentloaded', timeout=1000)
        articles = self.page.locator('article[data-video-id]')
        values = articles.locator('output[data-video-language-value]')
        if articles.count() != 1 or values.count() != 1: raise ValueError('Synthetic saved language ambiguous')
        return SavedLanguage(articles.get_attribute('data-video-id'), self.page.locator('html').get_attribute('lang'),
                             values.get_attribute('data-video-language-value'))


def language_contract(adapter):
    return StudioLanguageContract(adapter, LANGUAGE_MAP, provenance='synthetic:studio-language-v1')


class StudioLanguageContractTests(unittest.TestCase):
    def test_fixture_manifest_hashes_and_exact_tags_are_explicitly_synthetic(self):
        manifest = json.loads((FIXTURE_ROOT / 'manifest.json').read_text(encoding='utf-8'))
        self.assertTrue(manifest['synthetic']); self.assertFalse(manifest['live_verified'])
        self.assertEqual(manifest['language_semantic_values'], LANGUAGE_MAP['en'])
        for locale in ('vi', 'en'):
            name = f'language_{locale}.html'
            self.assertEqual(hashlib.sha256((FIXTURE_ROOT / name).read_bytes().replace(b'\r\n', b'\n')).hexdigest(),
                             manifest['sha256'][name])

    def test_keep_never_reads_selects_checks_guards_or_emits_evidence(self):
        adapter = LanguageFixtureAdapter(); c = language_contract(adapter); events = []
        self.assertIsNone(c.prepare('invalid / retained legacy tag', mode='keep'))
        self.assertIsNone(c.recheck(None, verify_binding=lambda: events.append('binding'), verify_approval=lambda: events.append('approval')))
        self.assertIsNone(c.verify_saved(None, 'unused', evidence_sink=events.append))
        self.assertEqual(verify_saved_metadata('unused', evidence_sink=events.append), {})
        self.assertEqual(adapter.calls, []); self.assertEqual(events, [])
        absent = StudioLanguageContract(None)
        self.assertIsNone(absent.prepare(mode='keep'))
        with self.assertRaisesRegex(StudioMetadataError, 'ADAPTER_UNAVAILABLE'): absent.prepare('vi')

    def test_exact_vi_en_and_region_tag_use_distinct_semantic_values_for_both_locales(self):
        for locale in ('vi', 'en'):
            for tag, value in LANGUAGE_MAP[locale].items():
                with self.subTest(locale=locale, tag=tag):
                    adapter = LanguageFixtureAdapter(locale); prepared = language_contract(adapter).prepare(tag)
                    self.assertEqual((prepared.video_language, prepared.value, adapter.value), (tag, value, value))
                    selected = [call for call in adapter.calls if call[0] == 'select']
                    self.assertEqual(selected, [] if tag == 'en' else [('select', value)])
        for unsupported in ('en-us', 'en-GB', 'VI'):
            with self.subTest(tag=unsupported), self.assertRaisesRegex(StudioMetadataError, 'OPTION_UNMAPPED'):
                language_contract(LanguageFixtureAdapter()).prepare(unsupported)

    def test_invalid_tags_maps_and_modes_fail_before_adapter_io(self):
        adapter = LanguageFixtureAdapter(); c = language_contract(adapter)
        for invalid in ('', None, [], 'en_US', 'en-<script>', 'en-' + 'x' * 65):
            with self.subTest(tag=invalid), self.assertRaisesRegex(StudioMetadataError, 'TAG_INVALID'): c.prepare(invalid)
        with self.assertRaisesRegex(StudioMetadataError, 'MODE_INVALID'): c.prepare('vi', mode='inherit')
        self.assertEqual(adapter.calls, [])
        for mapping, code in (({}, 'MAPPING_UNAVAILABLE'), ({'en': {'en': 'same', 'en-US': 'same'}}, 'MAPPING_AMBIGUOUS')):
            with self.assertRaisesRegex(StudioMetadataError, code): StudioLanguageContract(adapter, mapping, provenance='synthetic')

    def test_wrong_scopes_duplicate_disabled_and_malformed_observations_fail_closed(self):
        adapter = LanguageFixtureAdapter(); baseline = adapter.read_video_language()
        cases = (
            ({'section_key': 'caption_certification'}, 'SCOPE_AMBIGUOUS'),
            ({'section_key': 'title_language'}, 'SCOPE_AMBIGUOUS'),
            ({'scope_matches': 0}, 'SCOPE_AMBIGUOUS'), ({'scope_matches': 2}, 'SCOPE_AMBIGUOUS'),
            ({'control_matches': 2}, 'SCOPE_AMBIGUOUS'), ({'control_matches': True}, 'CONTROL_INVALID'),
            ({'scope_matches': 1.0}, 'CONTROL_INVALID'), ({'locale': []}, 'CONTROL_INVALID'),
            ({'locale': 'fr'}, 'LOCALE_UNSUPPORTED'), ({'value': {}}, 'CONTROL_INVALID'),
            ({'enabled': False}, 'CONTROL_DISABLED'), ({'visible': False}, 'CONTROL_DISABLED'),
            ({'options': (LanguageOption('spoken-vi', False),)}, 'OPTION_DISABLED'),
            ({'options': (LanguageOption('spoken-vi'), LanguageOption('spoken-vi'))}, 'OPTION_AMBIGUOUS'),
            ({'options': (LanguageOption([], True),)}, 'OPTIONS_INVALID'),
        )
        for changes, code in cases:
            with self.subTest(changes=changes):
                adapter.read_video_language = lambda changes=changes: replace(baseline, **changes)
                with self.assertRaisesRegex(StudioMetadataError, code): language_contract(adapter).prepare('vi')
        self.assertFalse(any(call[0] == 'select' for call in adapter.calls))

    def test_no_change_then_prepublication_drift_missing_control_and_guards_fail(self):
        adapter = LanguageFixtureAdapter(); c = language_contract(adapter)
        original_select = adapter.select_video_language
        adapter.select_video_language = lambda value: None
        with self.assertRaisesRegex(StudioMetadataError, 'SET_MISMATCH'): c.prepare('vi')
        adapter.select_video_language = original_select
        prepared = c.prepare('vi')
        for binding, approval, code in ((None, None, 'GUARD_REQUIRED'), (lambda: False, lambda: True, 'BINDING_CHANGED'),
                                        (lambda: True, lambda: False, 'APPROVAL_STALE')):
            with self.assertRaisesRegex(StudioMetadataError, code): c.recheck(prepared, verify_binding=binding, verify_approval=approval)
        adapter.value = 'spoken-en'
        with self.assertRaisesRegex(StudioMetadataError, 'CHANGED_BEFORE_PUBLISH'):
            c.recheck(prepared, verify_binding=lambda: True, verify_approval=lambda: True)
        control = adapter.read_video_language()
        adapter.read_video_language = lambda: replace(control, control_matches=0)
        with self.assertRaisesRegex(StudioMetadataError, 'SCOPE_AMBIGUOUS'):
            c.recheck(prepared, verify_binding=lambda: True, verify_approval=lambda: True)

    def test_language_only_evidence_does_not_invent_category(self):
        adapter = LanguageFixtureAdapter(); c = language_contract(adapter); prepared = c.prepare('en-US'); events = []
        saved = c.verify_saved(prepared, VIDEO_ID, evidence_sink=lambda event: events.append(_normalize_event(event)))
        self.assertEqual(saved.value, 'spoken-en-us')
        self.assertEqual([event['verification_state'] for event in events], ['received', 'verified'])
        self.assertEqual(events[0]['expected'], {'video_language': 'en-US'})
        self.assertEqual(events[-1]['observed'], {'video_language': 'en-US'})

    def test_combined_receipt_precedes_both_exact_id_readbacks_and_one_terminal(self):
        category_adapter, language_adapter = FixtureAdapter(), LanguageFixtureAdapter()
        cat, lang = category_contract(category_adapter), language_contract(language_adapter)
        prepared_cat, prepared_lang = cat.prepare('24'), lang.prepare('en-US')
        trace, events = [], []
        read_cat, read_lang = category_adapter.read_saved_category, language_adapter.read_saved_video_language
        category_adapter.read_saved_category = lambda video_id: (trace.append(('category', video_id)), read_cat(video_id))[1]
        language_adapter.read_saved_video_language = lambda video_id: (trace.append(('language', video_id)), read_lang(video_id))[1]
        def sink(event):
            trace.append((event['verification_state'],)); events.append(_normalize_event(event))
        result = verify_saved_metadata(VIDEO_ID, category=(cat, prepared_cat), language=(lang, prepared_lang), evidence_sink=sink)
        self.assertEqual(trace, [('received',), ('category', VIDEO_ID), ('language', VIDEO_ID), ('verified',)])
        self.assertEqual(set(result), {'category_id', 'video_language'})
        self.assertEqual(events[-1]['expected'], {'category_id': '24', 'video_language': 'en-US'})
        self.assertEqual(events[-1]['observed'], events[-1]['expected'])

    def test_receipt_callback_cannot_change_the_expected_language(self):
        adapter = LanguageFixtureAdapter(saved=SavedLanguage(VIDEO_ID, 'en', 'spoken-en'))
        contract = language_contract(adapter)
        events = []

        def sink(event):
            events.append(_normalize_event(event))
            event['expected']['video_language'] = 'en'

        with self.assertRaisesRegex(StudioMetadataError, 'READBACK_MISMATCH'):
            contract.verify_saved(contract.prepare('en-US'), VIDEO_ID, evidence_sink=sink)
        self.assertEqual(events[-1]['verification_state'], 'mismatch')
        self.assertEqual(events[-1]['expected'], {'video_language': 'en-US'})

    def test_category_success_language_failure_cannot_emit_early_verified(self):
        for saved, suffix, state in (
            (SavedLanguage(VIDEO_ID, 'en', 'spoken-en'), 'READBACK_MISMATCH', 'mismatch'),
            (SavedLanguage('otherID1234', 'en', 'spoken-en-us'), 'READBACK_ID_MISMATCH', 'unknown'),
            (SavedLanguage(VIDEO_ID, 'vi', 'spoken-en-us'), 'READBACK_LOCALE_CHANGED', 'unknown'),
            (SavedLanguage(VIDEO_ID, 'en', 'unsupported'), 'READBACK_UNMAPPED', 'unknown'),
            (SavedLanguage(VIDEO_ID, 'en', []), 'READBACK_INVALID', 'unknown'),
            (SavedLanguage(VIDEO_ID, {}, 'spoken-en-us'), 'READBACK_INVALID', 'unknown'),
        ):
            with self.subTest(suffix=suffix):
                cat = category_contract(FixtureAdapter()); lang = language_contract(LanguageFixtureAdapter(saved=saved)); events = []
                pc, pl = cat.prepare('24'), lang.prepare('en-US')
                with self.assertRaisesRegex(StudioMetadataError, suffix) as raised:
                    verify_saved_metadata(VIDEO_ID, category=(cat, pc), language=(lang, pl), evidence_sink=lambda event: events.append(_normalize_event(event)))
                self.assertTrue(raised.exception.after_send)
                self.assertEqual([event['verification_state'] for event in events], ['received', state])
                self.assertEqual(events[-1]['observed'].get('category_id'), '24')
                if state == 'mismatch': self.assertEqual(events[-1]['observed']['video_language'], 'en')

    def test_unknown_overrides_mismatch_and_both_fields_are_attempted_once(self):
        from studio_metadata_contract import SavedCategory
        category_adapter = FixtureAdapter(saved=SavedCategory(VIDEO_ID, 'en', 'people-blogs'))
        language_adapter = LanguageFixtureAdapter(); cat = category_contract(category_adapter); lang = language_contract(language_adapter)
        pc, pl = cat.prepare('24'), lang.prepare('vi'); events = []
        def failed_read(video_id):
            language_adapter.calls.append(('failed-read', video_id)); raise RuntimeError('private provider payload')
        language_adapter.read_saved_video_language = failed_read
        with self.assertRaisesRegex(StudioMetadataError, '^STUDIO_LANGUAGE_READBACK_FAILED$'):
            verify_saved_metadata(VIDEO_ID, category=(cat, pc), language=(lang, pl), evidence_sink=events.append)
        self.assertEqual([event['verification_state'] for event in events], ['received', 'unknown'])
        self.assertEqual(events[-1]['observed'], {'category_id': '22'})
        self.assertEqual(language_adapter.calls.count(('failed-read', VIDEO_ID)), 1)

    def test_receipt_sink_failure_prevents_both_reads_and_terminal_failure_never_succeeds(self):
        for fail_at in ('received', 'verified'):
            with self.subTest(fail_at=fail_at):
                ca, la = FixtureAdapter(), LanguageFixtureAdapter(); cat, lang = category_contract(ca), language_contract(la)
                pc, pl = cat.prepare('24'), lang.prepare('vi'); events, reads = [], []
                ca.read_saved_category = lambda video_id: reads.append(video_id)
                if fail_at == 'verified':
                    from studio_metadata_contract import SavedCategory
                    ca.read_saved_category = lambda video_id: SavedCategory(video_id, 'en', 'entertainment')
                def sink(event):
                    events.append(event)
                    if event['verification_state'] == fail_at: raise RuntimeError('private evidence error')
                with self.assertRaisesRegex(StudioMetadataError, '^STUDIO_METADATA_EVIDENCE_FAILED$') as raised:
                    verify_saved_metadata(VIDEO_ID, category=(cat, pc), language=(lang, pl), evidence_sink=sink)
                self.assertTrue(raised.exception.after_send)
                if fail_at == 'received':
                    self.assertEqual(reads, []); self.assertFalse(any(call[0] == 'saved' for call in la.calls))
                self.assertEqual([event['verification_state'] for event in events], ['received'] if fail_at == 'received' else ['received', 'verified'])


if __name__ == '__main__': unittest.main()
