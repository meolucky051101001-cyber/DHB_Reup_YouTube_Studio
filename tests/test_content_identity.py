import hashlib
import io
import tempfile
import unittest
from pathlib import Path

import content_identity


class ContentIdentityTests(unittest.TestCase):
    def test_file_hash_reads_the_entire_file(self):
        with tempfile.TemporaryDirectory() as td:
            path_a = Path(td) / "a.mp4"
            path_b = Path(td) / "renamed.mp4"
            prefix = b"x" * (1024 * 1024 + 23)
            path_a.write_bytes(prefix + b"tail-a")
            path_b.write_bytes(prefix + b"tail-b")

            self.assertNotEqual(content_identity.sha256_file(path_a), content_identity.sha256_file(path_b))
            self.assertEqual(
                content_identity.sha256_file(path_a),
                "sha256:" + hashlib.sha256(path_a.read_bytes()).hexdigest(),
            )

    def test_stream_copy_hash_matches_file_hash(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "upload.mp4"
            payload = b"video" * 450_000
            key, total = content_identity.copy_stream_and_hash(io.BytesIO(payload), path, chunk_size=8191)
            self.assertEqual(total, len(payload))
            self.assertEqual(key, content_identity.sha256_file(path))

    def test_url_normalization_keeps_query_values_and_drops_fragment(self):
        a = content_identity.normalize_source_url(
            "HTTPS://Example.COM:443/watch?z=2&a=1#comments"
        )
        b = content_identity.normalize_source_url(
            "https://example.com/watch?a=1&z=2"
        )
        self.assertEqual(a, b)
        self.assertEqual(
            content_identity.normalize_source_url("https://example.com/watch?video=1"),
            "https://example.com/watch?video=1",
        )

    def test_url_normalization_rejects_unsupported_or_credentialed_urls(self):
        self.assertEqual(content_identity.normalize_source_url("ftp://example.com/video"), "")
        self.assertEqual(content_identity.normalize_source_url("https://user@example.com/video"), "")

    def test_dedupe_key_prefers_fingerprint_then_source_id_then_url(self):
        self.assertEqual(
            content_identity.source_dedupe_key({"content_key": "sha256:abc", "post_id": "p1"}),
            "sha256:abc",
        )
        self.assertEqual(
            content_identity.source_dedupe_key({"platform": "YouTube", "post_id": "AbC"}),
            "source:youtube:AbC",
        )
        self.assertEqual(
            content_identity.source_dedupe_key({"source_url": "https://EXAMPLE.com/v#x"}),
            "url:https://example.com/v",
        )


if __name__ == "__main__":
    unittest.main()
