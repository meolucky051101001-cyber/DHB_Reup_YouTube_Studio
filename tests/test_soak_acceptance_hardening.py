"""Fast soak-harness regressions; no runtime DB, provider, media process or fake clock."""
import errno
import io
import json
import runpy
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing, redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import runtime_identity
import soak_review_release as soak


class _Entries:
    def __init__(self, entries):
        self.entries = entries

    def glob(self, _pattern):
        return iter(self.entries)

    def rglob(self, _pattern):
        return iter(self.entries)


class _StatFault:
    """Preserve an enumerated entry, then delete it at the size-read boundary."""
    def __init__(self, path, error=None):
        self.path = path
        self.name = path.name
        self.error = error
        self.reached = False

    def is_file(self):
        return self.path.is_file()

    def stat(self):
        self.reached = True
        if self.error is not None:
            raise self.error
        self.path.unlink()
        return self.path.stat()


class SoakSamplingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dhb-soak-sampling-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data, self.output, self.scratch = [self.root / name for name in ("data", "output", "temp")]
        for directory in (self.data, self.output, self.scratch):
            directory.mkdir()
        self.db_path = self.data / "soak.sqlite"
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("CREATE TABLE system_logs (message TEXT)")
            conn.executemany("INSERT INTO system_logs VALUES (?)", [("one",), ("two",)])
        self.wal = self.data / "soak.sqlite-wal"
        self.wal.write_bytes(b"wal")
        (self.data / "soak.sqlite-shm").write_bytes(b"excluded shared memory")
        self.artifact = self.output / "transient.mp4"
        self.artifact.write_bytes(b"artifact")
        (self.output / "empty-directory").mkdir()
        (self.scratch / "retained.bin").write_bytes(b"temp")
        self.subject = soak.Soak.__new__(soak.Soak)
        self.subject.config = SimpleNamespace(APP_DATA_DIR=self.data, APP_OUTPUT_DIR=self.output, APP_TEMP_DIR=self.scratch)
        def log_connection():
            # Keep SQLite from deleting the deliberately synthetic WAL fixture.
            conn = sqlite3.connect(":memory:")
            conn.execute("CREATE TABLE system_logs (message TEXT)")
            conn.executemany("INSERT INTO system_logs VALUES (?)", [("one",), ("two",)])
            return conn

        self.subject.db = SimpleNamespace(get_conn=log_connection)
        self.subject.measure_db_lock = lambda: 1.0

    def inject(self, kind, error=None):
        target = self.wal if kind == "wal" else self.artifact
        fault = _StatFault(target, error)
        if kind == "wal":
            self.subject.config.APP_DATA_DIR = _Entries([self.db_path, fault, self.data / "soak.sqlite-shm"])
        else:
            self.subject.config.APP_OUTPUT_DIR = _Entries([self.output / "empty-directory", fault])
        return target, fault

    def test_regular_files_are_counted_and_directories_and_shm_are_excluded(self):
        result = self.subject.sample()
        self.assertEqual(result["db_wal"], self.db_path.stat().st_size + 3)
        self.assertEqual(result["artifact"], 12)
        self.assertEqual(result["log_rows"], 2)
        self.assertEqual(result["lock_ms"], 1.0)

    def test_wal_deleted_between_enumeration_and_stat_counts_as_zero(self):
        target, fault = self.inject("wal")
        result = self.subject.sample()
        self.assertTrue(fault.reached)
        self.assertFalse(target.exists())
        self.assertEqual(result["db_wal"], self.db_path.stat().st_size)

    def test_artifact_deleted_between_enumeration_and_stat_counts_as_zero(self):
        target, fault = self.inject("artifact")
        result = self.subject.sample()
        self.assertTrue(fault.reached)
        self.assertFalse(target.exists())
        self.assertEqual(result["artifact"], 4)

    def test_stat_permission_and_other_io_errors_are_not_hidden(self):
        for kind in ("wal", "artifact"):
            for error in (PermissionError(errno.EACCES, "denied"), OSError(errno.EIO, "I/O failure")):
                with self.subTest(kind=kind, error=type(error).__name__):
                    _target, fault = self.inject(kind, error)
                    with self.assertRaises(type(error)) as raised:
                        self.subject.sample()
                    self.assertIs(raised.exception, error)
                    self.assertTrue(fault.reached)
                    self.subject.config.APP_DATA_DIR = self.data
                    self.subject.config.APP_OUTPUT_DIR = self.output


class SoakFinalLimitsTests(unittest.TestCase):
    def setUp(self):
        self.baseline = {"rss": 100, "handles": 10, "threads": 2, "db_wal": 200,
                         "artifact": 300, "log_rows": 4, "lock_ms": 1.0, "children": []}

    def run_pilot(self, final, round_changes=None):
        log = io.StringIO()
        manager = SimpleNamespace(is_running=False, _stop_event=threading.Event())
        uploader = SimpleNamespace(is_running=False, _stop_event=threading.Event())
        pool = mock.Mock()
        subject = SimpleNamespace(
            manager=manager, uploader=uploader,
            pm=SimpleNamespace(render_queue=pool, upload_video_to_youtube=mock.Mock(), render_video=mock.Mock()),
            fake_upload=mock.Mock(), render=mock.Mock(), log=log, violations=[], sources={},
            source=Path("synthetic-source-not-read.mp4"), source_hash="unchanged",
            hash_file=lambda _path: "unchanged", rendered=set(range(soak.WARMUP_JOBS + 16)),
            start_uploads=mock.Mock(return_value=[9001]),
            verify_uploads=mock.Mock(return_value={"gap_seconds": None, "attempts": 1, "history": 1, "reservations": 0}),
            sample=mock.Mock(side_effect=[dict(self.baseline), final]), wait=soak.Soak.wait,
        )
        report = soak.Soak.report
        subject.report = lambda event, **info: report(subject, event, **info)
        round_sample = {**self.baseline, **(round_changes or {})}
        subject.render_round = mock.Mock(side_effect=[None,
            {"label": "Round 1", "jobs": 8, "workers": 1, "peak_render": 1, "seconds": 0,
             "peak_lock_ms": 0, "sample": dict(round_sample)},
            {"label": "Round 2", "jobs": 8, "workers": 4, "peak_render": 4, "seconds": 0,
             "peak_lock_ms": 0, "sample": dict(round_sample)},
        ])
        self.subject, self.log = subject, log
        args = SimpleNamespace(pilot=True, minutes=60, revision="a" * 40)
        # Only workload/OS boundaries are stubbed. time, sleep and LIMITS stay real.
        with mock.patch.object(soak, "Soak", return_value=subject), \
             mock.patch.object(soak, "restrict_boundaries", return_value=[]), \
             mock.patch.object(soak, "children", return_value=[]), \
             mock.patch.object(soak.threading, "active_count", return_value=1):
            soak.run(args, log, Path("synthetic-root-not-created"))
        return [json.loads(line) for line in log.getvalue().splitlines()]

    def assert_failed_after_rounds(self):
        events = [json.loads(line) for line in self.log.getvalue().splitlines()]
        self.assertEqual([event["event"] for event in events], ["baseline", "round", "round"])
        self.assertEqual(self.subject.sample.call_count, 2)
        self.subject.pm.render_queue.shutdown.assert_called_once_with()
        self.assertTrue(self.subject.manager._stop_event.is_set())
        self.assertTrue(self.subject.uploader._stop_event.is_set())

    def test_final_only_growth_in_each_resource_rejects_completion(self):
        for key in ("rss", "handles", "threads", "db_wal", "artifact", "log_rows"):
            with self.subTest(resource=key):
                final = {**self.baseline, key: self.baseline[key] + soak.LIMITS[key + "_growth"] + 1}
                with self.assertRaisesRegex(RuntimeError, "Limit exceeded: " + key):
                    self.run_pilot(final)
                self.assert_failed_after_rounds()

    def test_final_only_lock_latency_rejects_completion(self):
        final = {**self.baseline, "lock_ms": soak.LIMITS["lock_ms"] + 0.01}
        with self.assertRaisesRegex(RuntimeError, "SQLite lock latency"):
            self.run_pilot(final)
        self.assert_failed_after_rounds()

    def test_final_only_child_process_rejects_completion(self):
        with self.assertRaisesRegex(RuntimeError, "children|Child processes"):
            self.run_pilot({**self.baseline, "children": [12345]})
        self.assert_failed_after_rounds()

    def test_unchanged_final_sample_completes_pilot_contract(self):
        events = self.run_pilot(dict(self.baseline))
        self.assertEqual([event["event"] for event in events], ["baseline", "round", "round", "complete"])
        summary = events[-1]
        self.assertIs(summary["pilot"], True)
        self.assertEqual(summary["revision"], "a" * 40)
        self.assertEqual(summary["jobs"], 16)
        self.assertEqual(summary["final"], self.baseline)
        self.assertIsNone(summary["uploads"]["gap_seconds"])
        self.assertGreaterEqual(summary["seconds"], 0)
        self.subject.start_uploads.assert_called_once_with(1)
        self.assertEqual(self.subject.render_round.call_args_list, [mock.call("Warmup", 32, 8),
                         mock.call("Round 1", 8, 1), mock.call("Round 2", 8, 4)])

    def test_exact_final_limits_remain_allowed(self):
        final = {key: self.baseline[key] + soak.LIMITS[key + "_growth"]
                 for key in ("rss", "handles", "threads", "db_wal", "artifact", "log_rows")}
        final.update(lock_ms=soak.LIMITS["lock_ms"], children=[])
        events = self.run_pilot(final)
        self.assertEqual(events[-1]["final"], final)

    def test_round_resource_limit_is_still_enforced(self):
        with self.assertRaisesRegex(RuntimeError, "Limit exceeded: handles"):
            self.run_pilot(dict(self.baseline), {"handles": self.baseline["handles"] + soak.LIMITS["handles_growth"] + 1})
        self.assertNotIn("complete", [json.loads(line)["event"] for line in self.log.getvalue().splitlines()])


class SoakDefaultContractTests(unittest.TestCase):
    def test_default_release_contract_is_60_minutes_ten_rounds_1000_jobs(self):
        args = soak.parse_args(["--output", "report-not-created.jsonl"])
        self.assertEqual(args.minutes, 60)
        self.assertIs(args.pilot, False)
        self.assertEqual(args.output, Path("report-not-created.jsonl"))
        self.assertEqual(soak.WORKERS, (1, 4, 8, 1, 4, 8, 1, 4, 8, 8))
        self.assertEqual(len(soak.WORKERS) * 100, 1000)
        self.assertEqual(soak.LIMITS, {"rss_growth": 256 * (1 << 20), "handles_growth": 32,
                         "threads_growth": 6, "db_wal_growth": 64 * (1 << 20),
                         "artifact_growth": 128 * (1 << 20), "log_rows_growth": 15000, "lock_ms": 2000})

    def test_pilot_is_explicit_and_does_not_change_minutes_default(self):
        args = soak.parse_args(["--output", "pilot-not-created.jsonl", "--pilot"])
        self.assertIs(args.pilot, True)
        self.assertEqual(args.minutes, 60)

    def test_duration_bounds_still_apply_to_release_and_pilot(self):
        for minutes in (59, 121):
            for extra in ([], ["--pilot"]):
                with self.subTest(minutes=minutes, pilot=bool(extra)), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        soak.parse_args(["--output", "not-created.jsonl", "--minutes", str(minutes), *extra])
                    self.assertEqual(raised.exception.code, 2)
        self.assertEqual(soak.parse_args(["--output", "not-created.jsonl", "--minutes", "120"]).minutes, 120)

    def test_dirty_checkout_cli_rejects_before_creating_output_or_starting_soak(self):
        with tempfile.TemporaryDirectory(prefix="dhb-soak-cli-test-") as directory:
            output = Path(directory) / "not-created.jsonl"
            stderr = io.StringIO()
            with mock.patch.object(soak.sys, "argv", [str(soak.__file__), "--output", str(output)]), \
                 mock.patch.object(runtime_identity, "git_output", side_effect=["a" * 40, " M soak_review_release.py"]), \
                 redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as raised:
                    runpy.run_path(soak.__file__, run_name="__main__")
            self.assertEqual(raised.exception.code, 2)
            self.assertIn("Run acceptance on a clean, committed revision.", stderr.getvalue())
            self.assertFalse(output.exists())

    def test_boundary_callback_still_rejects_provider_network_and_foreign_processes(self):
        root = Path(__file__).resolve().parent
        allowed = {name: str(root / (name + ".exe")) for name in ("ffmpeg", "ffprobe")}
        with mock.patch.object(soak.shutil, "which", side_effect=allowed.get), \
             mock.patch.object(soak.sys, "addaudithook") as register:
            denied = soak.restrict_boundaries()
        callback = register.call_args.args[0]
        for name in allowed:
            callback("subprocess.Popen", (allowed[name], [allowed[name]], None, None))
        self.assertEqual(denied, [])
        for event in ("socket.connect", "socket.getaddrinfo", "socket.gethostbyname",
                      "socket.gethostbyaddr", "socket.getnameinfo", "socket.sendto", "socket.sendmsg"):
            with self.subTest(event=event), self.assertRaises(PermissionError):
                callback(event, ())
        with self.assertRaises(PermissionError):
            callback("subprocess.Popen", (str(root / "foreign.exe"), [], None, None))
        self.assertEqual(len(denied), 8)


if __name__ == "__main__":
    unittest.main()
