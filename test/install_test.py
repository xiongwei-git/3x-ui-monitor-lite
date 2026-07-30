import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
INSTALLER = ROOT / "monitor" / "install.sh"


def make_installation(root: Path) -> tuple[Path, Path]:
    executable = root / "existing-x-ui"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chmod(executable, 0o755)
    database = root / "x-ui.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE settings (id INTEGER PRIMARY KEY, key TEXT, value TEXT);
        CREATE TABLE inbounds (id INTEGER PRIMARY KEY);
        CREATE TABLE client_traffics (email TEXT, up INTEGER, down INTEGER);
        """
    )
    connection.close()
    return executable, database


class InstallerTest(unittest.TestCase):
    def test_stage_install_generates_product_paths_and_interval(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            executable, database = make_installation(workspace)
            stage = workspace / "stage"
            notification = workspace / "alertbridge.json"
            notification.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "mode": "notifications",
                        "base_url": "https://notify.example.com",
                        "token": "test-token",
                        "category": "3x-ui",
                    }
                ),
                encoding="utf-8",
            )
            os.chmod(notification, 0o600)

            result = subprocess.run(
                [
                    "sh",
                    str(INSTALLER),
                    "--stage-root",
                    str(stage),
                    "--platform",
                    "linux",
                    "--xui-executable",
                    str(executable),
                    "--database",
                    str(database),
                    "--instance-name",
                    "SG-Panel-01",
                    "--instance-location",
                    "Singapore",
                    "--interval",
                    "420",
                    "--notification-config",
                    str(notification),
                ],
                cwd=ROOT,
                env={**os.environ, "PYTHON": sys.executable},
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            config_path = (
                stage / "etc/threexui-script-monitor/monitor.json"
            )
            config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(config["database_path"], str(database.resolve()))
            self.assertEqual(config["runtime"]["interval_seconds"], 420)
            self.assertEqual(config["runtime"]["service_manager"], "systemd")
            self.assertEqual(config["instance"]["name"], "SG-Panel-01")
            self.assertTrue(config["notifications"]["enabled"])
            self.assertEqual(
                config["notifications"]["notifier_path"],
                "/usr/local/lib/threexui-script-monitor/alertbridge_notify.py",
            )
            timer = (
                stage
                / "etc/systemd/system/threexui-script-monitor.timer"
            ).read_text(encoding="utf-8")
            self.assertIn("OnUnitActiveSec=420s", timer)
            self.assertTrue(
                (
                    stage
                    / "usr/local/lib/threexui-script-monitor/xui_discovery.py"
                ).exists()
            )

            reinstall = subprocess.run(
                [
                    "sh",
                    str(INSTALLER),
                    "--stage-root",
                    str(stage),
                    "--platform",
                    "linux",
                    "--xui-executable",
                    str(executable),
                    "--database",
                    str(database),
                ],
                cwd=ROOT,
                env={**os.environ, "PYTHON": sys.executable},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(reinstall.returncode, 0, reinstall.stderr)
            preserved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(preserved["runtime"]["interval_seconds"], 420)
            regenerated_timer = (
                stage
                / "etc/systemd/system/threexui-script-monitor.timer"
            ).read_text(encoding="utf-8")
            self.assertIn("OnUnitActiveSec=420s", regenerated_timer)

    def test_missing_xui_exits_without_creating_stage_root(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            stage = workspace / "stage"

            result = subprocess.run(
                [
                    "sh",
                    str(INSTALLER),
                    "--stage-root",
                    str(stage),
                    "--platform",
                    "linux",
                    "--xui-executable",
                    str(workspace / "missing-x-ui"),
                    "--database",
                    str(workspace / "missing.db"),
                ],
                cwd=ROOT,
                env={**os.environ, "PYTHON": sys.executable},
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("3x-ui", result.stderr)
            self.assertFalse(stage.exists())

    def test_stage_macos_install_generates_launchdaemon_and_process_check(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            executable, database = make_installation(workspace)
            stage = workspace / "stage"

            result = subprocess.run(
                [
                    "sh",
                    str(INSTALLER),
                    "--stage-root",
                    str(stage),
                    "--platform",
                    "macos",
                    "--xui-executable",
                    str(executable),
                    "--database",
                    str(database),
                    "--interval",
                    "600",
                ],
                cwd=ROOT,
                env={**os.environ, "PYTHON": sys.executable},
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            config = json.loads(
                (
                    stage / "etc/threexui-script-monitor/monitor.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(config["runtime"]["service_manager"], "process")
            self.assertFalse(config["notifications"]["enabled"])
            plist = (
                stage
                / "Library/LaunchDaemons/com.threexui.script-monitor.plist"
            ).read_text(encoding="utf-8")
            self.assertIn("<integer>600</integer>", plist)

    def test_invalid_interval_is_rejected_before_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            executable, database = make_installation(workspace)
            stage = workspace / "stage"

            result = subprocess.run(
                [
                    "sh",
                    str(INSTALLER),
                    "--stage-root",
                    str(stage),
                    "--xui-executable",
                    str(executable),
                    "--database",
                    str(database),
                    "--interval",
                    "30",
                ],
                cwd=ROOT,
                env={**os.environ, "PYTHON": sys.executable},
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("interval", result.stderr.lower())
            self.assertFalse(stage.exists())


if __name__ == "__main__":
    unittest.main()
