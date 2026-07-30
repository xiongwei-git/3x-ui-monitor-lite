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


def make_deployment_config(
    path: Path,
    executable: Path,
    database: Path,
) -> None:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "scheduler": {
                    "enabled": False,
                    "interval_seconds": 480,
                },
                "xui": {
                    "executable_path": str(executable),
                    "database_path": str(database),
                    "service_name": "x-ui.service",
                },
                "instance": {
                    "name": "Config-Panel-01",
                    "location": "Singapore",
                },
                "notifications": {
                    "enabled": True,
                    "title_template": "{instance_name}：{event_title}",
                    "message_template": (
                        "监控对象：{instance_name}\n"
                        "事件状态：{event_status}\n"
                        "详细信息：{event_message}\n"
                        "{event_details}"
                    ),
                    "delivery": {
                        "version": 1,
                        "provider": "alertbridge",
                        "mode": "notifications",
                        "base_url": "https://notify.example.com",
                        "token": "deployment-config-token",
                        "category": "3x-ui",
                    },
                },
                "client_ip_details": {
                    "exposure": "full",
                    "max_entries": 5,
                    "database_path": (
                        "/usr/local/share/threexui-script-monitor/"
                        "ip2region_v4.xdb"
                    ),
                },
                "checks": {
                    "node_heartbeat_max_age_seconds": 210,
                    "node_latency_warn_ms": 1900,
                    "node_cpu_warn_percent": 94,
                    "node_memory_warn_percent": 89,
                    "ip_window_seconds": 2100,
                    "ip_warn_count": 5,
                    "ip_critical_count": 9,
                    "traffic_interval_seconds": 480,
                    "traffic_min_elapsed_seconds": 60,
                    "traffic_max_elapsed_seconds": 1800,
                    "traffic_warn_bytes_per_interval": 5368709120,
                    "traffic_critical_bytes_per_interval": 16106127360,
                    "repeat_alert_seconds": 18000,
                },
            }
        ),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


class InstallerTest(unittest.TestCase):
    def test_stage_install_accepts_single_standard_config_file(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            executable, database = make_installation(workspace)
            deployment = workspace / "deployment.json"
            make_deployment_config(deployment, executable, database)
            stage = workspace / "stage"

            result = subprocess.run(
                [
                    "sh",
                    str(INSTALLER),
                    "--config",
                    str(deployment),
                    "--stage-root",
                    str(stage),
                    "--platform",
                    "linux",
                ],
                cwd=ROOT,
                env={**os.environ, "PYTHON": sys.executable},
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            monitor_path = stage / "etc/threexui-script-monitor/monitor.json"
            monitor = json.loads(monitor_path.read_text(encoding="utf-8"))
            self.assertEqual(monitor["instance"]["name"], "Config-Panel-01")
            self.assertEqual(monitor["runtime"]["interval_seconds"], 480)
            self.assertEqual(monitor["checks"]["ip_warn_count"], 5)
            self.assertEqual(monitor["client_ip_details"]["exposure"], "full")
            self.assertTrue(monitor["notifications"]["enabled"])
            self.assertNotIn(
                "deployment-config-token",
                monitor_path.read_text(encoding="utf-8"),
            )
            installed_notification = (
                stage / "etc/threexui-script-monitor/alertbridge.json"
            )
            self.assertIn(
                "deployment-config-token",
                installed_notification.read_text(encoding="utf-8"),
            )
            timer = (
                stage
                / "etc/systemd/system/threexui-script-monitor.timer"
            ).read_text(encoding="utf-8")
            self.assertIn("OnUnitActiveSec=480s", timer)

    def test_standard_config_rejects_conflicting_cli_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            executable, database = make_installation(workspace)
            deployment = workspace / "deployment.json"
            make_deployment_config(deployment, executable, database)
            stage = workspace / "stage"

            result = subprocess.run(
                [
                    "sh",
                    str(INSTALLER),
                    "--config",
                    str(deployment),
                    "--interval",
                    "600",
                    "--stage-root",
                    str(stage),
                    "--platform",
                    "linux",
                ],
                cwd=ROOT,
                env={**os.environ, "PYTHON": sys.executable},
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("cannot be combined", result.stderr)
            self.assertFalse(stage.exists())

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

    def test_stage_install_configures_direct_feishu_webhook(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            executable, database = make_installation(workspace)
            stage = workspace / "stage"
            notification = workspace / "feishu.json"
            notification.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "provider": "feishu_webhook",
                        "webhook_url": (
                            "https://open.feishu.cn/open-apis/bot/v2/hook/"
                            "00000000-0000-0000-0000-000000000000"
                        ),
                        "signing_secret": "demo-signing-secret",
                        "keyword": "3x-ui",
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
            config = json.loads(
                (
                    stage / "etc/threexui-script-monitor/monitor.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                config["notifications"]["provider"],
                "feishu_webhook",
            )
            self.assertEqual(
                config["notifications"]["notifier_path"],
                "/usr/local/lib/threexui-script-monitor/feishu_notify.py",
            )
            self.assertEqual(
                config["notifications"]["config_path"],
                "/etc/threexui-script-monitor/feishu.json",
            )
            self.assertTrue(
                (
                    stage
                    / "etc/threexui-script-monitor/feishu.json"
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
            preserved = json.loads(
                (
                    stage / "etc/threexui-script-monitor/monitor.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                preserved["notifications"]["provider"],
                "feishu_webhook",
            )

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
