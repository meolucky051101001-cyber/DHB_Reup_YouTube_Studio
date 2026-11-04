import sys
from pathlib import Path
from unittest import TestCase, mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ai_engine


class Phase6AiFallbackTests(TestCase):
    def test_source_prefix_does_not_damage_other_complete_urls(self):
        source = "https://shop.example/item"
        others = [source + "?aff=NEW", source + "/child", source + "#details"]
        result, count = ai_engine._preserve_source_links(source, "Related: " + " ".join(others))
        for url in others:
            self.assertIn(url, result.split())
        self.assertIn(source, result.split())
        self.assertEqual(count, 1)

    def test_urls_at_the_limit_fit_with_or_without_generated_prose(self):
        prefix = "https://example.test/"
        for length in (4998, 4999, 5000):
            url = prefix + "x" * (length - len(prefix))
            for generated in (url, "prose " + url, "prose"):
                with self.subTest(length=length, prose=generated != url):
                    result, count = ai_engine._preserve_source_links(url, generated)
                    self.assertEqual(result, url)
                    self.assertEqual(count, 1)
        with self.assertRaises(ai_engine.SourceLinksTooLongError):
            ai_engine._preserve_source_links(prefix + "x" * (5001 - len(prefix)), "")

    def test_long_generated_description_reserves_complete_source_url(self):
        url = "https://shop.example/item?aff=sample123"
        generated = "x" * 4990 + " " + url
        result, preserved = ai_engine._preserve_source_links(url, generated)
        self.assertLessEqual(len(result), 5000)
        self.assertTrue(result.endswith(url))
        self.assertIn(url, result)
        self.assertEqual(preserved, 1)

    def test_source_urls_are_deduplicated_exactly_without_casefolding_path(self):
        upper = "https://shop.example/item/A?aff=one"
        lower = "https://shop.example/item/a?aff=one"
        result, preserved = ai_engine._preserve_source_links(f"{upper} {lower} {upper}", "rewritten")
        self.assertEqual(preserved, 2)
        self.assertIn(upper, result)
        self.assertIn(lower, result)

    def test_source_urls_that_cannot_fit_fail_explicitly(self):
        url = "https://example.test/" + "x" * 100
        with self.assertRaises(ai_engine.SourceLinksTooLongError):
            ai_engine._preserve_source_links(url, "generated", limit=40)

    def test_generated_text_is_shortened_when_source_links_are_reserved(self):
        url = "https://shop.example/item?aff=sample123"
        generated = "prose " + url + " " + "x" * 5100
        result, preserved = ai_engine._preserve_source_links(url, generated)
        self.assertLessEqual(len(result), 5000)
        self.assertTrue(result.endswith(url))
        self.assertEqual(preserved, 1)

    def test_provider_failure_is_reported_and_source_link_is_preserved(self):
        with mock.patch.object(ai_engine, "call_llm", side_effect=RuntimeError("temporary provider error")), \
             mock.patch.object(ai_engine, "translate_google_free", side_effect=lambda text, lang: "translated"), \
             mock.patch.object(ai_engine, "add_log"):
            result = ai_engine.ai_optimize_youtube_metadata(
                "A title",
                "Original copy https://shop.example/item?aff=sample123",
                provider="gemini",
            )

        self.assertEqual(result["provider_used"], "google_translate")
        self.assertTrue(result["fallback"])
        self.assertEqual(result["links_preserved"], 1)
        self.assertIn("https://shop.example/item?aff=sample123", result["description"])

    def test_selected_translate_provider_is_not_mislabeled_as_fallback(self):
        with mock.patch.object(ai_engine, "translate_google_free", side_effect=lambda text, lang: text):
            result = ai_engine.ai_optimize_youtube_metadata("Title", provider="google_translate")
        self.assertEqual(result["provider_used"], "google_translate")
        self.assertFalse(result["fallback"])
