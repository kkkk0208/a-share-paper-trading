"""Exercise Windows launchers in child processes without changing system tasks."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parent.parent
POWERSHELL = shutil.which("powershell.exe")


def ps_quote(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Windows PowerShell required")
class WindowsLauncherTests(unittest.TestCase):
    def run_ps(self, code: str, env: dict[str, str] | None = None) -> dict:
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command",
             "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new(); " + code],
            capture_output=True, encoding="utf-8", env=env, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def helper_result(self, existing: str | None, explicit_proxy: bool = False) -> dict:
        env = dict(os.environ)
        for key in list(env):
            if key.lower().endswith("_proxy"):
                del env[key]
        if existing is not None:
            env["NO_PROXY"] = existing
        if explicit_proxy:
            env["HTTPS_PROXY"] = "http://explicit-proxy.invalid:8080"
        helper = ps_quote(ROOT / "scripts" / "_python.ps1")
        python = ps_quote(sys.executable)
        return self.run_ps(
            f". {helper}; $first=$env:NO_PROXY; . {helper}; "
            f"$proxies=& {python} -c 'import json,urllib.request; print(json.dumps(urllib.request.getproxies()))'; "
            "@{first=$first; twice=$env:NO_PROXY; proxies=($proxies | ConvertFrom-Json)} | ConvertTo-Json -Compress",
            env,
        )

    def test_quote_bypass_preserves_existing_hosts_and_is_idempotent(self):
        result = self.helper_result("localhost,.example.org,QT.GTIMG.CN")
        self.assertEqual(result["first"], "localhost,.example.org,QT.GTIMG.CN,hq.sinajs.cn")
        self.assertEqual(result["twice"], result["first"])

    def test_quote_bypass_initializes_missing_or_empty_value_and_disables_registry_proxy(self):
        for existing in (None, ""):
            with self.subTest(existing=existing):
                result = self.helper_result(existing)
                self.assertEqual(result["first"], "qt.gtimg.cn,hq.sinajs.cn")
                self.assertEqual(result["proxies"], {"no": result["first"]})

    def test_explicit_proxy_environment_is_preserved(self):
        result = self.helper_result("localhost", explicit_proxy=True)
        self.assertEqual(result["proxies"]["https"], "http://explicit-proxy.invalid:8080")
        self.assertIn("qt.gtimg.cn", result["proxies"]["no"])

    def test_collector_registration_uses_repo_runner_and_weekday_settings(self):
        script = ps_quote(ROOT / "scripts" / "register_quote_collector_task.ps1")
        python = ps_quote(sys.executable)
        result = self.run_ps("""
function New-ScheduledTaskAction { param($Execute,$Argument,$WorkingDirectory); @{execute=$Execute;arguments=$Argument;cwd=$WorkingDirectory} }
function New-ScheduledTaskTrigger { param([switch]$Weekly,$DaysOfWeek,$At); @{weekly=[bool]$Weekly;days=@($DaysOfWeek);at=$At.ToString('HH:mm')} }
function New-ScheduledTaskPrincipal { param($UserId,$LogonType,$RunLevel); @{logon=$LogonType;level=$RunLevel} }
function New-ScheduledTaskSettingsSet { param([switch]$WakeToRun,[switch]$StartWhenAvailable,[switch]$AllowStartIfOnBatteries,[switch]$DontStopIfGoingOnBatteries,$MultipleInstances,$ExecutionTimeLimit); @{wake=[bool]$WakeToRun;catchup=[bool]$StartWhenAvailable;hours=$ExecutionTimeLimit.TotalHours;instances=$MultipleInstances} }
function Register-ScheduledTask { param($TaskName,$Action,$Trigger,$Settings,$Principal,$Description,[switch]$Force); $global:registered=@{name=$TaskName;action=$Action;trigger=$Trigger;settings=$Settings;principal=$Principal} }
function New-Item { param($ItemType,$Path,[switch]$Force) }
""" + f"$null=& {script} -PythonPath {python}; $global:registered | ConvertTo-Json -Compress -Depth 6")
        self.assertEqual(result["name"], "AShareQuoteCollector")
        self.assertIn("-File", result["action"]["arguments"])
        self.assertIn("run_quote_acceptance.ps1", result["action"]["arguments"])
        self.assertIn("-Mode Collect", result["action"]["arguments"])
        self.assertIn(str(Path(sys.executable)), result["action"]["arguments"])
        self.assertEqual(Path(result["action"]["cwd"]), ROOT)
        self.assertEqual(result["trigger"]["at"], "09:05")
        self.assertTrue(result["trigger"]["weekly"])
        self.assertEqual(result["trigger"]["days"], ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"])
        self.assertEqual(result["settings"], {"wake": True, "catchup": True, "hours": 8, "instances": "IgnoreNew"})
        self.assertEqual(result["principal"], {"logon": "Interactive", "level": "Limited"})


if __name__ == "__main__":
    unittest.main()
