import tempfile
import unittest
from pathlib import Path

from runtime_identity import APPLICATION_ID, capture_runtime, runtime_status


class RuntimeIdentityTests(unittest.TestCase):
    def test_disk_change_preserves_fingerprint_and_revision_at_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "app.py"
            source.write_text("VERSION = 1", encoding="utf-8")
            startup = capture_runtime(root)
            self.assertEqual(startup["application"], APPLICATION_ID)
            self.assertFalse(runtime_status(root, startup)["restart_required"])

            source.write_text("VERSION = 2", encoding="utf-8")
            current = runtime_status(root, startup)

            self.assertTrue(current["restart_required"])
            self.assertEqual(current["startup_fingerprint"], startup["startup_fingerprint"])
            self.assertEqual(current["revision"], startup["revision"])
            self.assertNotEqual(current["disk_fingerprint"], startup["startup_fingerprint"])
