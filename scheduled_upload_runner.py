"""CLI for packaged Private upload schedules; no account-specific constants."""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from scheduled_uploads import (CONTRACT_VERSION, ScheduleError, ScheduleRunner, load_json,
    migrate_legacy, new_state, read_schedule_status, save_json, sha256,
    sync_schedule_folder, validate_manifest, validate_release)


def release_pin(runtime_dir, revision, build):
    package = Path(__file__).resolve().parent
    result = {"version": 1, "runner_contract": CONTRACT_VERSION,
              "runtime_root": str(Path(runtime_dir).resolve()), "revision": revision, "build": build,
              "runner_hashes": {name: sha256(package / name) for name in
                  ("scheduled_uploads.py", "scheduled_upload_runner.py", "scheduled_upload_task.py")}}
    validate_release(result, runtime_dir)
    return result


def initialize_directory(target, manifest, state, release):
    """Never replace an existing operational directory or infer a fresh cursor."""
    target = Path(target)
    if target.exists():
        raise ScheduleError("TARGET_EXISTS")
    validate_manifest(manifest)
    target.mkdir(parents=True, exist_ok=False)
    try:
        save_json(target / "manifest.json", manifest)
        save_json(target / "state.json", state)
        save_json(target / "release.json", release)
    except BaseException:
        # Retain partial initialization for diagnosis. Do not recursively remove a computed path.
        raise


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("run", "observe"):
        command = commands.add_parser(name)
        command.add_argument("--runtime-dir", required=True, type=Path)
        command.add_argument("--state-dir", required=True, type=Path)
        command.add_argument("--base-url", default="http://127.0.0.1:8686")
        command.add_argument("--dry-run", action="store_true")
        command.add_argument("--observer-seconds", type=int, default=3600)
    status = commands.add_parser("status")
    status.add_argument("--state-dir", required=True, type=Path)
    sync_cmd = commands.add_parser("sync-folder")
    sync_cmd.add_argument("--runtime-dir", required=True, type=Path)
    sync_cmd.add_argument("--state-dir", required=True, type=Path)
    sync_cmd.add_argument("--base-url", default="http://127.0.0.1:8686")
    prepare = commands.add_parser("prepare-release")
    prepare.add_argument("--runtime-dir", required=True, type=Path)
    prepare.add_argument("--revision", required=True)
    prepare.add_argument("--build", required=True)
    prepare.add_argument("--output", required=True, type=Path)
    for name in ("initialize", "migrate-legacy"):
        command = commands.add_parser(name)
        command.add_argument("--state-dir", required=True, type=Path)
        command.add_argument("--release", required=True, type=Path)
        if name == "initialize":
            command.add_argument("--manifest", required=True, type=Path)
        else:
            command.add_argument("--legacy-dir", required=True, type=Path)
            command.add_argument("--start-at", required=True)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "status":
            print(json.dumps(read_schedule_status(args.state_dir), ensure_ascii=False, indent=2))
            return 0
        if args.command == "sync-folder":
            result = sync_schedule_folder(args.state_dir, args.runtime_dir, base_url=args.base_url)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.command == "prepare-release":
            value = release_pin(args.runtime_dir, args.revision, args.build)
            if args.output.exists():
                raise ScheduleError("OUTPUT_EXISTS")
            save_json(args.output, value)
            print("Release pin prepared; no runtime or Task was changed.")
            return 0
        if args.command in ("initialize", "migrate-legacy"):
            release = load_json(args.release)
            validate_release(release, release.get("runtime_root", ""))
            if args.command == "initialize":
                manifest = load_json(args.manifest)
                validate_manifest(manifest)
                state = new_state(manifest)
            else:
                manifest, state = migrate_legacy(
                    load_json(args.legacy_dir / "private-upload-manifest.json"),
                    load_json(args.legacy_dir / "private-upload-state.json"), start_at=args.start_at)
            initialize_directory(args.state_dir, manifest, state, release)
            print("Operational files prepared; no Task was registered and no upload was started.")
            return 0
        # Logging is installed only in the explicit run path, never at import.
        if not args.state_dir.is_dir():
            raise ScheduleError("SCHEDULE_UNINITIALIZED")
        handler = logging.FileHandler(args.state_dir / "schedule.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger = logging.getLogger("dhb.scheduled_upload")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        try:
            runner = ScheduleRunner(args.state_dir, args.runtime_dir, base_url=args.base_url,
                                    observer_seconds=args.observer_seconds)
            code = runner.run(dry_run=args.dry_run, observe_only=args.command == "observe")
            logger.info("Schedule check finished; exit_code=%d dry_run=%s", code, args.dry_run)
            print(json.dumps(read_schedule_status(args.state_dir), ensure_ascii=False))
            return code
        except Exception as error:
            # Never include provider/HTTP bodies or arbitrary exception text in logs.
            code = error.code if isinstance(error, ScheduleError) else "SCHEDULE_FAILED"
            logger.error("Schedule check stopped; code=%s", code)
            raise
        finally:
            logger.removeHandler(handler)
            handler.close()
    except (ScheduleError, OSError, ValueError, TypeError, KeyError) as error:
        print(json.dumps({"ok": False, "code": error.code if isinstance(error, ScheduleError) else "SCHEDULE_FAILED"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
