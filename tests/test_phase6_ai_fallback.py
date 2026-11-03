import sys
from pathlib import Path
from unittest import TestCase, mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ai_engine


class Phase6AiFallbackTests(TestCase):
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
