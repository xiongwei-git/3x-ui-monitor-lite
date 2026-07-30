import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
MODULE_PATH = ROOT / "monitor" / "deployment_config.py"
SPEC = importlib.util.spec_from_file_location("deployment_config", MODULE_PATH)
deployment = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(deployment)


def deployment_config() -> dict:
    return {
        "version": 1,
        "scheduler": {
            "enabled": True,
            "interval_seconds": 420,
        },
        "xui": {
            "executable_path": "/usr/local/x-ui/x-ui",
            "database_path": "/etc/x-ui/x-ui.db",
            "service_name": "x-ui.service",
        },
        "instance": {
            "name": "SG-Panel-01",
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
                "mode": "events",
                "base_url": "https://notify.example.com",
                "client_id": "threexui-monitor",
                "client_secret": "demo-client-secret",
                "routing_key": "base",
                "source": "threexui-script-monitor",
                "labels": {
                    "server": "SG-Panel-01",
                    "service": "3x-ui",
                },
            },
        },
        "client_ip_details": {
            "exposure": "full",
            "max_entries": 6,
            "database_path": (
                "/usr/local/share/threexui-script-monitor/ip2region_v4.xdb"
            ),
        },
        "checks": {
            "node_heartbeat_max_age_seconds": 240,
            "node_latency_warn_ms": 1800,
            "node_cpu_warn_percent": 92,
            "node_memory_warn_percent": 88,
            "ip_window_seconds": 2400,
            "ip_warn_count": 5,
            "ip_critical_count": 9,
            "traffic_interval_seconds": 420,
            "traffic_min_elapsed_seconds": 60,
            "traffic_max_elapsed_seconds": 1800,
            "traffic_warn_bytes_per_interval": 6442450944,
            "traffic_critical_bytes_per_interval": 17179869184,
            "repeat_alert_seconds": 14400,
        },
    }


class DeploymentConfigTest(unittest.TestCase):
    def test_load_config_is_strict_and_requires_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "deployment.json"
            path.write_text(json.dumps(deployment_config()), encoding="utf-8")
            os.chmod(path, 0o644)

            with self.assertRaisesRegex(deployment.DeploymentConfigError, "0600"):
                deployment.load_config(str(path))

            config = deployment_config()
            config["instnace"] = config["instance"]
            path.write_text(json.dumps(config), encoding="utf-8")
            os.chmod(path, 0o600)
            with self.assertRaisesRegex(deployment.DeploymentConfigError, "unknown"):
                deployment.load_config(str(path))

    def test_inspect_returns_only_non_secret_install_fields(self):
        inspected = deployment.inspect_config(deployment_config())

        self.assertEqual(inspected["interval_seconds"], 420)
        self.assertEqual(inspected["xui_executable"], "/usr/local/x-ui/x-ui")
        self.assertEqual(inspected["database"], "/etc/x-ui/x-ui.db")
        self.assertEqual(inspected["notification_provider"], "alertbridge")
        self.assertTrue(inspected["scheduler_enabled"])
        self.assertNotIn("secret", json.dumps(inspected).lower())

    def test_build_monitor_config_preserves_settings_and_uses_product_paths(self):
        config = deployment_config()

        monitor_config, notification = deployment.build_outputs(
            config,
            platform="linux",
            database="/etc/x-ui/x-ui.db",
            executable="/usr/local/x-ui/x-ui",
        )

        self.assertEqual(monitor_config["checks"]["ip_warn_count"], 5)
        self.assertEqual(monitor_config["client_ip_details"]["exposure"], "full")
        self.assertEqual(
            monitor_config["state_path"],
            "/var/lib/threexui-script-monitor/state.json",
        )
        self.assertEqual(
            monitor_config["notifications"]["notifier_path"],
            "/usr/local/lib/threexui-script-monitor/alertbridge_notify.py",
        )
        self.assertNotIn(
            "demo-client-secret",
            json.dumps(monitor_config),
        )
        self.assertEqual(notification["client_secret"], "demo-client-secret")

    def test_enabled_notifications_require_delivery_settings(self):
        config = deployment_config()
        config["notifications"]["delivery"] = None

        with self.assertRaisesRegex(
            deployment.DeploymentConfigError,
            "delivery",
        ):
            deployment.validate_config(config)

    def test_standard_config_supports_direct_feishu_delivery(self):
        config = deployment_config()
        config["notifications"]["delivery"] = {
            "version": 1,
            "provider": "feishu_webhook",
            "webhook_url": (
                "https://open.feishu.cn/open-apis/bot/v2/hook/"
                "00000000-0000-0000-0000-000000000000"
            ),
            "signing_secret": "demo-signing-secret",
            "keyword": "3x-ui",
        }

        monitor_config, notification = deployment.build_outputs(
            config,
            platform="linux",
            database="/etc/x-ui/x-ui.db",
            executable="/usr/local/x-ui/x-ui",
        )

        self.assertEqual(
            monitor_config["notifications"]["provider"],
            "feishu_webhook",
        )
        self.assertEqual(
            monitor_config["notifications"]["config_path"],
            "/etc/threexui-script-monitor/feishu.json",
        )
        self.assertEqual(
            notification["signing_secret"],
            "demo-signing-secret",
        )

    def test_materialize_writes_split_private_configs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            monitor_path = root / "monitor.json"
            notification_path = root / "alertbridge.json"

            deployment.materialize(
                deployment_config(),
                platform="linux",
                database="/etc/x-ui/x-ui.db",
                executable="/usr/local/x-ui/x-ui",
                monitor_output=str(monitor_path),
                notification_output=str(notification_path),
            )

            self.assertEqual(monitor_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(notification_path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(
                "demo-client-secret",
                monitor_path.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "demo-client-secret",
                notification_path.read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
