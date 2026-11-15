"""Contract tests use a synthetic adapter; no YouTube selectors or provider calls."""
import json
import hashlib
from dataclasses import replace
from pathlib import Path
import unittest

from studio_metadata_contract import (
    CategoryControl, CategoryOption, PreparedCategory, SavedCategory, StudioCategoryContract,
    StudioMetadataError,
)


FIXTURE_ROOT = Path(__file__).parent / 'fixtures' / 'studio_metadata'
MAP = {'vi': {'22': 'people-blogs', '24': 'entertainment'},
       'en': {'22': 'people-blogs', '24': 'entertainment'}}


class FixtureAdapter:
    def __init__(self, locale='en', *, options=None, value='people-blogs', video_id='dQw4w9WgXcQ', saved=None):
        self.locale, self.options, self.value, self.video_id = locale, options or (
            CategoryOption('people-blogs'), CategoryOption('entertainment')), value, video_id
        self.saved = saved
        self.reveals = 0
        self.selected = []

    def reveal_category(self): self.reveals += 1

    def read_category(self):
        return CategoryControl(self.locale, 'category', 1, 1, True, True, self.value, tuple(self.options))

    def select_category(self, value):
        self.selected.append(value)
        self.value = value

    def read_saved_category(self, video_id):
        return self.saved or SavedCategory(video_id, self.locale, self.value)


class SyntheticCategoryBrowserAdapter:
    """Selectors refer ONLY to the authored fixtures under tests/fixtures."""
    def __init__(self, page):
        self.page = page

    def reveal_category(self):
        buttons = self.page.locator('[data-action="show-more"]')
        if buttons.count() != 1:
            raise ValueError('Synthetic reveal control ambiguous')
        buttons.click(timeout=500)
        self.page.locator('[data-details]').wait_for(state='visible', timeout=1000)

    def read_category(self):
        sections = self.page.locator('section[data-studio-section="category"]')
        section_count = sections.count()
        controls = sections.locator('select[data-semantic="category"]')
        control_count = controls.count()
        locale = self.page.locator('html').get_attribute('lang')
        if section_count != 1 or control_count != 1:
            return CategoryControl(locale, 'category', section_count, control_count, False, False, '', ())
        options = tuple(CategoryOption(item['value'], item['enabled']) for item in controls.locator('option').evaluate_all(
            '(items) => items.map(item => ({value:item.value,enabled:!item.disabled}))'))
        return CategoryControl(locale, 'category', section_count, control_count,
                               controls.is_visible(), controls.is_enabled(), controls.input_value(), options)

    def select_category(self, value):
        control = self.page.locator('section[data-studio-section="category"] select[data-semantic="category"]')
        if control.count() != 1:
            raise ValueError('Synthetic category control ambiguous')
        control.select_option(value=value, timeout=500)

    def read_saved_category(self, video_id):
        self.page.goto(f'http://127.0.0.1:8687/video/{video_id}', wait_until='domcontentloaded', timeout=1000)
        articles = self.page.locator('article[data-video-id]')
        values = articles.locator('output[data-category-value]')
        if articles.count() != 1 or values.count() != 1:
            raise ValueError('Synthetic saved category ambiguous')
        return SavedCategory(articles.get_attribute('data-video-id'), self.page.locator('html').get_attribute('lang'),
                             values.get_attribute('data-category-value'))


def contract(adapter):
    return StudioCategoryContract(adapter, MAP, provenance='synthetic:studio-category-v1')


class StudioMetadataContractTests(unittest.TestCase):
    def test_missing_adapter_or_mapping_and_duplicate_semantics_fail_closed(self):
        for adapter, maps, provenance, code in (
            (None, MAP, 'synthetic', 'ADAPTER_UNAVAILABLE'),
            (FixtureAdapter(), {}, 'synthetic', 'MAPPING_UNAVAILABLE'),
            (FixtureAdapter(), MAP, '', 'MAPPING_UNAVAILABLE'),
            (FixtureAdapter(), {'en': {'22': 'same', '24': 'same'}}, 'synthetic', 'MAPPING_AMBIGUOUS'),
            (FixtureAdapter(), {'en': {'22': []}}, 'synthetic', 'MAPPING_INVALID'),
        ):
            with self.subTest(code=code), self.assertRaisesRegex(StudioMetadataError, code):
                StudioCategoryContract(adapter, maps, provenance=provenance)

    def test_fixtures_are_explicit_synthetic_and_have_hash_manifest(self):
        manifest = json.loads((FIXTURE_ROOT / 'manifest.json').read_text(encoding='utf-8'))
        self.assertTrue(manifest['synthetic'])
        self.assertFalse(manifest['live_verified'])
        self.assertEqual(set(manifest['locales']), {'en', 'vi'})
        for name, digest in manifest['sha256'].items():
            normalized = (FIXTURE_ROOT / name).read_bytes().replace(b'\r\n', b'\n')
            self.assertEqual(hashlib.sha256(normalized).hexdigest(), digest)

    def test_vi_and_en_select_by_semantic_value_with_no_label_guess(self):
        for locale in ('vi', 'en'):
            adapter = FixtureAdapter(locale, value='entertainment')
            prepared = contract(adapter).prepare('24')
            self.assertEqual(prepared.value, 'entertainment')
            self.assertEqual(adapter.selected, [])
            self.assertEqual(adapter.reveals, 1)

    def test_duplicate_scope_or_control_is_blocked(self):
        adapter = FixtureAdapter()
        adapter.read_category = lambda: CategoryControl('en', 'category', 2, 1, True, True, 'people-blogs', tuple(adapter.options))
        with self.assertRaisesRegex(StudioMetadataError, 'SCOPE_AMBIGUOUS'):
            contract(adapter).prepare('22')
        adapter.read_category = lambda: CategoryControl('en', 'category', 1, 2, True, True, 'people-blogs', tuple(adapter.options))
        with self.assertRaisesRegex(StudioMetadataError, 'SCOPE_AMBIGUOUS'):
            contract(adapter).prepare('22')

    def test_malformed_control_observations_are_sanitized_before_selection(self):
        adapter = FixtureAdapter()
        baseline = adapter.read_category()
        for changes in (
            {'scope_matches': True}, {'scope_matches': 1.0}, {'control_matches': True},
            {'control_matches': 1.0}, {'locale': []}, {'section_key': {}},
            {'value': []}, {'visible': 1}, {'enabled': 1},
            {'options': [CategoryOption('people-blogs')]},
            {'options': (CategoryOption([], True),)},
            {'options': (CategoryOption('people-blogs', 1),)},
        ):
            with self.subTest(changes=changes):
                adapter.read_category = lambda changes=changes: replace(baseline, **changes)
                with self.assertRaisesRegex(StudioMetadataError, '(CONTROL|OPTIONS)_INVALID') as raised:
                    contract(adapter).prepare('24')
                self.assertFalse(raised.exception.after_send)
                self.assertEqual(adapter.selected, [])

    def test_wrong_section_disabled_control_unmapped_and_duplicate_option_fail(self):
        cases = [
            CategoryControl('en', 'audience', 1, 1, True, True, 'people-blogs', tuple()),
            CategoryControl('en', 'category', 1, 1, True, False, 'people-blogs', tuple()),
        ]
        for control in cases:
            with self.subTest(control=control):
                adapter = FixtureAdapter(); adapter.read_category = lambda control=control: control
                with self.assertRaises(StudioMetadataError): contract(adapter).prepare('22')
        with self.assertRaisesRegex(StudioMetadataError, 'OPTION_UNMAPPED'):
            contract(FixtureAdapter()).prepare('26')
        duplicate = FixtureAdapter(options=(CategoryOption('people-blogs'), CategoryOption('people-blogs')))
        with self.assertRaisesRegex(StudioMetadataError, 'OPTION_AMBIGUOUS'):
            contract(duplicate).prepare('22')

    def test_unknown_locale_and_disabled_option_fail_without_fallback(self):
        with self.assertRaisesRegex(StudioMetadataError, 'LOCALE_UNSUPPORTED'):
            contract(FixtureAdapter('fr')).prepare('22')
        disabled = FixtureAdapter(options=(CategoryOption('people-blogs', False), CategoryOption('entertainment')))
        with self.assertRaisesRegex(StudioMetadataError, 'OPTION_DISABLED'):
            contract(disabled).prepare('22')

    def test_selection_is_read_back_and_drift_before_publish_fails(self):
        adapter = FixtureAdapter(value='people-blogs')
        c = contract(adapter)
        prepared = c.prepare('24')
        self.assertEqual(adapter.selected, ['entertainment'])
        adapter.value = 'people-blogs'
        with self.assertRaisesRegex(StudioMetadataError, 'CHANGED_BEFORE_PUBLISH'):
            c.recheck(prepared, verify_binding=lambda: None, verify_approval=lambda: None)
        adapter.value = 'entertainment'
        self.assertIsNone(c.recheck(prepared, verify_binding=lambda: None, verify_approval=lambda: None))

    def test_selection_no_change_missing_control_and_provider_errors_fail_closed(self):
        adapter = FixtureAdapter()
        adapter.select_category = lambda value: None
        with self.assertRaisesRegex(StudioMetadataError, 'SET_MISMATCH'):
            contract(adapter).prepare('24')
        baseline = adapter.read_category()
        adapter.read_category = lambda: replace(baseline, control_matches=0)
        with self.assertRaisesRegex(StudioMetadataError, 'SCOPE_AMBIGUOUS'):
            contract(adapter).prepare('24')
        def fail(): raise RuntimeError('private provider details')
        adapter.read_category = fail
        with self.assertRaisesRegex(StudioMetadataError, '^STUDIO_CATEGORY_CONTROL_UNAVAILABLE$'):
            contract(adapter).prepare('24')
        adapter.reveal_category = fail
        with self.assertRaisesRegex(StudioMetadataError, '^STUDIO_CATEGORY_REVEAL_FAILED$'):
            contract(adapter).prepare('24')

    def test_guards_and_malformed_preparation_block_prepublication(self):
        adapter = FixtureAdapter()
        c = contract(adapter); prepared = c.prepare('24')
        calls = []
        def private_failure(): raise RuntimeError('private binding')
        for binding, approval, code in (
            (None, lambda: None, 'GUARD_REQUIRED'),
            (lambda: False, lambda: calls.append('approval'), 'BINDING_CHANGED'),
            (private_failure, lambda: calls.append('approval'), 'BINDING_CHANGED'),
            (lambda: None, lambda: False, 'APPROVAL_STALE'),
            (lambda: None, private_failure, 'APPROVAL_STALE'),
        ):
            with self.subTest(code=code), self.assertRaisesRegex(StudioMetadataError, code):
                c.recheck(prepared, verify_binding=binding, verify_approval=approval)
        self.assertEqual(calls, [])
        for invalid in (replace(prepared, locale=[]), replace(prepared, category_id={}),
                        PreparedCategory('24', 'en', 'people-blogs', prepared.provenance)):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(StudioMetadataError, 'PREPARATION_INVALID'):
                c.recheck(invalid, verify_binding=lambda: None, verify_approval=lambda: None)

    def test_exact_video_id_readback_persists_received_then_verified(self):
        adapter = FixtureAdapter(value='entertainment')
        c = contract(adapter); prepared = c.prepare('24'); events = []
        saved = c.verify_saved(prepared, 'dQw4w9WgXcQ', evidence_sink=events.append)
        self.assertEqual(saved.video_id, 'dQw4w9WgXcQ')
        self.assertEqual([item['verification_state'] for item in events], ['received', 'verified'])
        self.assertEqual(events[0]['video_id'], 'dQw4w9WgXcQ')
        self.assertEqual(events[1]['observed'], {'category_id': '24'})

    def test_wrong_id_readback_and_mismatch_are_uncertain_after_send(self):
        for saved, expected in [(SavedCategory('otherID1234', 'en', 'entertainment'), 'READBACK_ID_MISMATCH'),
                                (SavedCategory('dQw4w9WgXcQ', 'en', 'people-blogs'), 'READBACK_MISMATCH')]:
            with self.subTest(expected=expected):
                adapter = FixtureAdapter(value='entertainment', saved=saved); c = contract(adapter); prepared = c.prepare('24'); events = []
                with self.assertRaisesRegex(StudioMetadataError, expected) as raised: c.verify_saved(prepared, 'dQw4w9WgXcQ', evidence_sink=events.append)
                self.assertTrue(raised.exception.after_send)
                self.assertEqual(events[0]['verification_state'], 'received')
                self.assertIn(events[-1]['verification_state'], ('unknown', 'mismatch'))

    def test_saved_malformed_locale_or_value_and_readback_errors_record_unknown(self):
        def fail(video_id): raise RuntimeError('private readback')
        for saved, code in (
            (SavedCategory('dQw4w9WgXcQ', [], 'entertainment'), 'READBACK_INVALID'),
            (SavedCategory('dQw4w9WgXcQ', 'en', {}), 'READBACK_INVALID'),
            (SavedCategory([], 'en', 'entertainment'), 'READBACK_INVALID'),
            (SavedCategory('dQw4w9WgXcQ', 'vi', 'entertainment'), 'READBACK_LOCALE_CHANGED'),
            (SavedCategory('dQw4w9WgXcQ', 'en', 'unmapped'), 'READBACK_UNMAPPED'),
            (None, 'READBACK_FAILED'),
        ):
            with self.subTest(code=code):
                adapter = FixtureAdapter(saved=saved); c = contract(adapter); prepared = c.prepare('24'); events = []
                if saved is None: adapter.read_saved_category = fail
                with self.assertRaisesRegex(StudioMetadataError, '^STUDIO_CATEGORY_' + code + '$') as raised:
                    c.verify_saved(prepared, 'dQw4w9WgXcQ', evidence_sink=events.append)
                self.assertTrue(raised.exception.after_send)
                self.assertEqual([item['verification_state'] for item in events], ['received', 'unknown'])
                self.assertEqual(events[-1]['observed'], {})

    def test_receipt_persistence_is_required_before_saved_readback(self):
        adapter = FixtureAdapter(); c = contract(adapter); prepared = c.prepare('24'); readbacks = []
        adapter.read_saved_category = lambda video_id: readbacks.append(video_id)
        def fail(event): raise RuntimeError('private receipt persistence')
        for sink, code in ((None, 'EVIDENCE_REQUIRED'), (fail, 'EVIDENCE_FAILED')):
            with self.subTest(code=code), self.assertRaisesRegex(StudioMetadataError, code) as raised:
                c.verify_saved(prepared, 'dQw4w9WgXcQ', evidence_sink=sink)
            self.assertTrue(raised.exception.after_send)
        self.assertEqual(readbacks, [])

    def test_sink_failure_after_receipt_never_returns_success(self):
        calls = []
        def sink(event):
            calls.append(event)
            if len(calls) == 1: return
            raise RuntimeError('private sink')
        adapter = FixtureAdapter(value='entertainment'); prepared = contract(adapter).prepare('24')
        with self.assertRaisesRegex(StudioMetadataError, 'EVIDENCE_FAILED') as raised:
            contract(adapter).verify_saved(prepared, 'dQw4w9WgXcQ', evidence_sink=sink)
        self.assertTrue(raised.exception.after_send)
        self.assertEqual([item['verification_state'] for item in calls], ['received', 'verified'])


if __name__ == '__main__': unittest.main()
