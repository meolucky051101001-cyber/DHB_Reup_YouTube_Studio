"""Prepare, register and restore one Windows Task for a packaged schedule.

Default plan mode writes a reviewable XML/JSON only. Windows Task mutation needs
the explicit apply or restore subcommand; this module does nothing at import.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET

from scheduled_uploads import (ScheduleError, load_json, parse_time, save_json,
    validate_manifest, validate_release, validate_state)


TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def validate_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_. -]{0,100}", value):
        raise ScheduleError("TASK_NAME_INVALID")


def task_xml(*, task_name, python, runtime_dir, state_dir, start_at, interval_days=3):
    validate_name(task_name)
    parse_time(start_at)
    if type(interval_days) is not int or not 1 <= interval_days <= 365:
        raise ScheduleError("TASK_INTERVAL_INVALID")
    runtime, state, interpreter = (Path(value).resolve() for value in (runtime_dir, state_dir, python))
    if any('"' in str(value) for value in (runtime, state, interpreter)):
        raise ScheduleError("TASK_PATH_INVALID")
    ET.register_namespace("", TASK_NS)
    root = ET.Element(f"{{{TASK_NS}}}Task", {"version": "1.4"})
    def element(parent, name, text=None, attrs=None):
        node = ET.SubElement(parent, f"{{{TASK_NS}}}{name}", attrs or {})
        if text is not None:
            node.text = str(text)
        return node
    registration = element(root, "RegistrationInfo")
    element(registration, "Description", "DHB Private uploads; one video after at least 72 hours. Requires signed-in user and healthy local runtime.")
    triggers = element(root, "Triggers")
    trigger = element(triggers, "CalendarTrigger")
    element(trigger, "StartBoundary", start_at)
    element(trigger, "Enabled", "true")
    daily = element(trigger, "ScheduleByDay")
    element(daily, "DaysInterval", interval_days)
    principals = element(root, "Principals")
    principal = element(principals, "Principal", attrs={"id": "Operator"})
    element(principal, "LogonType", "InteractiveToken")
    element(principal, "RunLevel", "LeastPrivilege")
    settings = element(root, "Settings")
    for name, value in (("MultipleInstancesPolicy", "IgnoreNew"), ("DisallowStartIfOnBatteries", "false"),
                        ("StopIfGoingOnBatteries", "false"), ("AllowHardTerminate", "true"),
                        ("StartWhenAvailable", "true"), ("RunOnlyIfNetworkAvailable", "true"),
                        ("AllowStartOnDemand", "true"), ("Enabled", "true"), ("Hidden", "false"),
                        ("WakeToRun", "true"), ("ExecutionTimeLimit", "PT2H")):
        element(settings, name, value)
    actions = element(root, "Actions", attrs={"Context": "Operator"})
    action = element(actions, "Exec")
    element(action, "Command", interpreter)
    element(action, "Arguments", f'-B "{runtime / "scheduled_upload_runner.py"}" run --runtime-dir "{runtime}" --state-dir "{state}"')
    element(action, "WorkingDirectory", runtime)
    return '<?xml version="1.0" encoding="UTF-16"?>\n' + ET.tostring(root, encoding="unicode", xml_declaration=False)


def prepare_plan(*, task_name, python, runtime_dir, state_dir, output_dir):
    manifest = load_json(Path(state_dir) / "manifest.json")
    validate_manifest(manifest)
    validate_state(load_json(Path(state_dir) / "state.json"), manifest)
    release = load_json(Path(state_dir) / "release.json")
    validate_release(release, runtime_dir)
    destination = Path(output_dir)
    if destination.exists():
        raise ScheduleError("OUTPUT_EXISTS")
    xml = task_xml(task_name=task_name, python=python, runtime_dir=runtime_dir, state_dir=state_dir,
                   start_at=manifest["start_at"])
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "task.xml").write_text(xml, encoding="utf-8")
    plan = {"version": 1, "task_name": task_name, "runtime_root": str(Path(runtime_dir).resolve()),
            "state_dir": str(Path(state_dir).resolve()), "python": str(Path(python).resolve()),
            "task_xml": "task.xml", "interactive_session_required": True,
            "disable_after_finished": "Operator disables after UI confirms all jobs finished; no deletion of operational state.",
            "registration_changes": "Replace one named Task only; preserve an existing XML backup before applying.",
            "rollback": "Restore previous Task XML together with the matching app release and external state backup; never reset next_index."}
    save_json(destination / "plan.json", plan)
    return plan


def _powershell(script, env_vars):
    if os.name != "nt":
        raise ScheduleError("WINDOWS_REQUIRED")
    env = os.environ.copy()
    env.update({key: str(value) for key, value in env_vars.items()})
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
        env=env, capture_output=True, text=True, timeout=60,
    )
    if result.returncode:
        raise ScheduleError("TASK_OPERATION_FAILED")
    return result.stdout


def apply_plan(plan_dir):
    directory = Path(plan_dir).resolve()
    plan = load_json(directory / "plan.json")
    validate_name(plan.get("task_name"))
    if plan.get("version") != 1 or plan.get("task_xml") != "task.xml":
        raise ScheduleError("TASK_PLAN_INVALID")
    backup = directory / "previous-task.xml"
    if backup.exists() or (directory / "registration.json").exists():
        raise ScheduleError("TASK_PLAN_ALREADY_APPLIED")
    # Pass paths and names strictly through environment variables, never interpolated into code.
    script = (
        "$ErrorActionPreference='Stop'; "
        "$Name=$env:DHB_TASK_NAME; $XmlPath=$env:DHB_TASK_XML; $BackupPath=$env:DHB_TASK_BACKUP; "
        "$old=Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue; "
        "if($old){ Export-ScheduledTask -TaskName $Name | Set-Content -LiteralPath $BackupPath -Encoding UTF8 }; "
        "[xml]$doc=Get-Content -LiteralPath $XmlPath -Raw -Encoding UTF8; "
        "$ns=New-Object System.Xml.XmlNamespaceManager($doc.NameTable); "
        "$ns.AddNamespace('t','http://schemas.microsoft.com/windows/2004/02/mit/task'); "
        "$p=$doc.SelectSingleNode('//t:Principal',$ns); "
        "if($p -and -not $p.SelectSingleNode('t:UserId',$ns)){ "
        "  $uid=$doc.CreateElement('UserId','http://schemas.microsoft.com/windows/2004/02/mit/task'); "
        "  $uid.InnerText=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value; "
        "  $p.PrependChild($uid) | Out-Null "
        "}; "
        "$xmlText='<?xml version=\"1.0\" encoding=\"UTF-16\"?>' + [Environment]::NewLine + $doc.DocumentElement.OuterXml; "
        "Register-ScheduledTask -TaskName $Name -Xml $xmlText -Force | Out-Null"
    )
    _powershell(script, {
        "DHB_TASK_NAME": plan["task_name"],
        "DHB_TASK_XML": directory / "task.xml",
        "DHB_TASK_BACKUP": backup,
    })
    save_json(directory / "registration.json", {"version": 1, "task_name": plan["task_name"], "had_previous_task": backup.exists()})


def restore_plan(plan_dir):
    directory = Path(plan_dir).resolve()
    plan, registration = load_json(directory / "plan.json"), load_json(directory / "registration.json")
    validate_name(plan.get("task_name"))
    if registration.get("task_name") != plan["task_name"] or registration.get("version") != 1:
        raise ScheduleError("TASK_PLAN_INVALID")
    if registration.get("had_previous_task"):
        backup = directory / "previous-task.xml"
        if not backup.is_file():
            raise ScheduleError("TASK_BACKUP_MISSING")
        _powershell(
            "$ErrorActionPreference='Stop'; Register-ScheduledTask -TaskName $env:DHB_TASK_NAME -Xml (Get-Content -LiteralPath $env:DHB_TASK_BACKUP -Raw -Encoding UTF8) -Force | Out-Null",
            {"DHB_TASK_NAME": plan["task_name"], "DHB_TASK_BACKUP": backup},
        )
    else:
        # Disabling keeps audit history and makes rollback reviewable; never delete a Task here.
        _powershell(
            "$ErrorActionPreference='Stop'; Disable-ScheduledTask -TaskName $env:DHB_TASK_NAME | Out-Null",
            {"DHB_TASK_NAME": plan["task_name"]},
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    for name in ("task-name", "python", "runtime-dir", "state-dir", "output-dir"):
        plan.add_argument("--" + name, required=True)
    for name in ("apply", "restore"):
        command = sub.add_parser(name)
        command.add_argument("--plan-dir", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            result = prepare_plan(task_name=args.task_name, python=args.python, runtime_dir=args.runtime_dir,
                                  state_dir=args.state_dir, output_dir=args.output_dir)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "apply":
            apply_plan(args.plan_dir)
            print("One named Windows Task registered; no Task was manually started.")
        else:
            restore_plan(args.plan_dir)
            print("Task rollback applied; verify app release/state compatibility before the next trigger.")
        return 0
    except (ScheduleError, OSError, ValueError, TypeError, KeyError) as error:
        print(json.dumps({"ok": False, "code": error.code if isinstance(error, ScheduleError) else "TASK_PLAN_FAILED"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
