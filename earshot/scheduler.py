"""`earshot schedule` — register a daily Windows Task Scheduler entry that
runs the pipeline. On non-Windows we print a cron line instead since the
project is currently Windows-targeted.

The wrapper script ``scripts/run_earshot.ps1`` is what gets registered. It
locates the venv, runs ``earshot run``, and appends output to
``data/logs/earshot-<date>.log``.
"""
from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path

from earshot.config import Config


TASK_NAME = "Earshot Daily Digest"


def register_windows(cfg: Config, hour_local: int = 18, minute_local: int = 30) -> tuple[bool, str]:
    """Register the daily Task Scheduler job. Returns (success, message).

    The default 18:30 hour is set in *the local PC timezone* so that for
    Arab Standard Time it lands at 11:30 EDT during US summer. Trial users in
    other timezones should override via the args.
    """
    from earshot import config as config_mod
    wrapper = config_mod.REPO_ROOT / "scripts" / "run_earshot.ps1"
    if not wrapper.exists():
        return False, f"wrapper script missing at {wrapper}"

    time_arg = f"{hour_local:02d}:{minute_local:02d}"
    ps_script = f"""
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -ExecutionPolicy Bypass -File "{wrapper}"'
$trigger = New-ScheduledTaskTrigger -Daily -At {time_arg}
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -ExecutionTimeLimit (New-TimeSpan -Hours 1) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Earshot daily pipeline' -Force | Out-Null
Write-Output 'OK'
""".strip()

    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", ps_script],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 0 and "OK" in result.stdout:
        return True, (
            f"Registered Windows task '{TASK_NAME}' at {time_arg} daily.\n"
            "  Trigger manually:  Start-ScheduledTask -TaskName 'Earshot Daily Digest'\n"
            "  Check last run:    Get-ScheduledTaskInfo -TaskName 'Earshot Daily Digest'\n"
            "  Remove:            Unregister-ScheduledTask -TaskName 'Earshot Daily Digest' -Confirm:$false"
        )
    return False, f"PowerShell exit {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"


def unregister_windows() -> tuple[bool, str]:
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command",
         f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false; Write-Output 'OK'"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 0 and "OK" in result.stdout:
        return True, f"Removed Windows task '{TASK_NAME}'."
    return False, f"PowerShell exit {result.returncode}\nstderr: {result.stderr}"


def cron_line_for_unix(cfg: Config, hour_local: int = 18, minute_local: int = 30) -> str:
    """Return a cron line the user can paste into ``crontab -e``."""
    from earshot import config as config_mod
    repo = config_mod.REPO_ROOT
    venv_python = repo / ".venv" / "bin" / "earshot"
    log_path = repo / "data" / "logs"
    return (
        f"# Earshot daily run — paste into `crontab -e`\n"
        f"{minute_local} {hour_local} * * * mkdir -p {log_path} && "
        f"cd {repo} && {venv_python} run >> {log_path}/earshot-$(date +\\%F).log 2>&1"
    )


def is_windows() -> bool:
    return platform.system().lower().startswith("win") or sys.platform.startswith("win")
