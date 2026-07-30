import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "monitor" / "anomaly_monitor.py"
SPEC = importlib.util.spec_from_file_location("anomaly_monitor", MODULE_PATH)
monitor = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(monitor)


def make_database():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY,
            guid TEXT,
            name TEXT,
            enable NUMERIC,
            status TEXT,
            last_heartbeat INTEGER,
            latency_ms INTEGER,
            cpu_pct REAL,
            mem_pct REAL,
            xray_state TEXT,
            config_dirty NUMERIC,
            last_error TEXT,
            xray_error TEXT
        );
        CREATE TABLE inbound_client_ips (client_email TEXT, ips TEXT);
        CREATE TABLE node_client_ips (node_guid TEXT, email TEXT, ips TEXT);
        CREATE TABLE client_traffics (email TEXT, up INTEGER, down INTEGER);
        CREATE TABLE node_client_traffics (node_id INTEGER, email TEXT, up INTEGER, down INTEGER);
        """
    )
    return connection


class AnomalyMonitorTest(unittest.TestCase):
    def setUp(self):
        self.now = 1_700_000_000
        self.config = monitor.default_config()

    def test_default_config_uses_standalone_product_paths_and_groups(self):
        self.assertEqual(self.config["runtime"]["interval_seconds"], 300)
        self.assertEqual(self.config["runtime"]["service_manager"], "auto")
        self.assertEqual(self.config["instance"]["name"], "3x-ui Monitor")
        self.assertIn(
            "/threexui-script-monitor/",
            self.config["notifications"]["notifier_path"],
        )
        self.assertNotIn("vpsops", json.dumps(self.config).lower())

    def test_config_rejects_unknown_root_and_nested_keys(self):
        config = monitor.default_config()
        config["unknown"] = True
        with self.assertRaisesRegex(monitor.MonitorError, "unknown config key"):
            monitor.validate_config(config)

        config = monitor.default_config()
        config["checks"]["ip_warn_counts"] = 3
        with self.assertRaisesRegex(monitor.MonitorError, "checks.ip_warn_counts"):
            monitor.validate_config(config)

    def test_config_rejects_unsafe_notification_templates_and_interval(self):
        config = monitor.default_config()
        config["notifications"]["title_template"] = "{unknown}"
        with self.assertRaisesRegex(monitor.MonitorError, "placeholder"):
            monitor.validate_config(config)

        config = monitor.default_config()
        config["notifications"]["title_template"] = "{event_title!r}"
        with self.assertRaisesRegex(monitor.MonitorError, "conversion"):
            monitor.validate_config(config)

        config = monitor.default_config()
        config["runtime"]["interval_seconds"] = 59
        with self.assertRaisesRegex(monitor.MonitorError, "interval_seconds"):
            monitor.validate_config(config)

    def test_load_config_maps_legacy_notification_and_service_fields(self):
        legacy = {
            "version": 1,
            "notifier_path": "/opt/legacy/notify.py",
            "notifier_config_path": "/etc/legacy/notify.json",
            "local_service_name": "custom-x-ui.service",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitor.json"
            path.write_text(json.dumps(legacy), encoding="utf-8")
            os.chmod(path, 0o600)

            config = monitor.load_config(str(path))

        self.assertEqual(config["notifications"]["notifier_path"], "/opt/legacy/notify.py")
        self.assertEqual(config["notifications"]["config_path"], "/etc/legacy/notify.json")
        self.assertEqual(config["runtime"]["service_name"], "custom-x-ui.service")
        self.assertNotIn("notifier_path", config)

    def test_load_config_rejects_group_or_other_writable_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitor.json"
            path.write_text('{"version":1}', encoding="utf-8")
            os.chmod(path, 0o666)

            with self.assertRaisesRegex(monitor.MonitorError, "permissions"):
                monitor.load_config(str(path))

    def test_database_checks_degrade_when_optional_multinode_tables_are_absent(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE settings (id INTEGER PRIMARY KEY, key TEXT, value TEXT);
            CREATE TABLE inbounds (id INTEGER PRIMARY KEY);
            CREATE TABLE client_traffics (email TEXT, up INTEGER, down INTEGER);
            CREATE TABLE inbound_client_ips (client_email TEXT, ips TEXT);
            """
        )

        issues = monitor.collect_database_issues(connection, self.config, {}, self.now)

        self.assertEqual(issues, [])

    def test_client_ip_check_deduplicates_nodes_and_ignores_stale_records(self):
        connection = make_database()
        self.addCleanup(connection.close)
        connection.execute(
            "INSERT INTO inbound_client_ips VALUES (?, ?)",
            (
                "primary@example",
                '[{"ip":"1.1.1.1","timestamp":1699999990},'
                '{"ip":"2.2.2.2","timestamp":1699999980}]',
            ),
        )
        connection.execute(
            "INSERT INTO node_client_ips VALUES (?, ?, ?)",
            (
                "node-a",
                "primary@example",
                '[{"ip":"1.1.1.1","timestamp":1699999970},'
                '{"ip":"3.3.3.3","timestamp":1699999960},'
                '{"ip":"9.9.9.9","timestamp":1699990000}]',
            ),
        )
        self.config["checks"]["ip_warn_count"] = 3
        self.config["checks"]["ip_critical_count"] = 5

        issues = monitor.collect_database_issues(
            connection, self.config, {}, self.now
        )

        issue = next(item for item in issues if item.key.startswith("client-ip:"))
        self.assertEqual(issue.level, "warning")
        self.assertIn("3 个不同公网 IP", issue.message)
        self.assertNotIn("1.1.1.1", issue.message)
        self.assertEqual(issue.notification_details, "")

    @mock.patch.object(monitor, "resolve_ip_metadata")
    def test_client_ip_full_details_include_source_time_region_and_isp(self, resolve_ip_metadata):
        connection = make_database()
        self.addCleanup(connection.close)
        connection.execute(
            "INSERT INTO nodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (1, "guid-a", "LISA-US-01", 1, "online", self.now, 20, 3, 5, "running", 0, "", ""),
        )
        connection.execute(
            "INSERT INTO inbound_client_ips VALUES (?, ?)",
            (
                "primary@example",
                '[{"ip":"1.1.1.1","timestamp":1699999400},'
                '{"ip":"2.2.2.2","timestamp":1699999500},'
                '{"ip":"3.3.3.3","timestamp":1699999600}]',
            ),
        )
        connection.execute(
            "INSERT INTO node_client_ips VALUES (?, ?, ?)",
            (
                "guid-a",
                "primary@example",
                '[{"ip":"1.1.1.1","timestamp":1699999900},'
                '{"ip":"4.4.4.4","timestamp":1699999700}]',
            ),
        )
        self.config["checks"]["ip_warn_count"] = 3
        self.config["checks"]["ip_critical_count"] = 8
        self.config["client_ip_details"]["exposure"] = "full"
        self.config["client_ip_details"]["max_entries"] = 3
        resolve_ip_metadata.return_value = {
            "1.1.1.1": monitor.IPMetadata("澳大利亚", "昆士兰", "布里斯班", "Cloudflare", "AU"),
            "2.2.2.2": monitor.IPMetadata("法国", "法兰西岛", "巴黎", "Orange", "FR"),
        }

        issues = monitor.collect_database_issues(connection, self.config, {}, self.now)

        issue = next(item for item in issues if item.key.startswith("client-ip:"))
        self.assertIn("1.1.1.1", issue.notification_details)
        self.assertIn("主节点、LISA-US-01", issue.notification_details)
        self.assertIn("澳大利亚 / 昆士兰 / 布里斯班", issue.notification_details)
        self.assertIn("运营商：Cloudflare", issue.notification_details)
        self.assertIn("首次：10 分钟前", issue.notification_details)
        self.assertIn("最近：1 分钟前", issue.notification_details)
        self.assertIn("归属：暂不可用", issue.notification_details)
        self.assertIn("另有 1 个 IP 未展示", issue.notification_details)
        self.assertNotIn("1.1.1.1", issue.message)

    def test_full_ip_details_are_not_persisted_in_alert_state(self):
        issue = monitor.Issue(
            "client-ip:abc",
            "warning",
            "客户端 IP 数异常",
            "客户端 primary@example 在最近 30 分钟出现 4 个不同公网 IP",
            "IP 明细：1.1.1.1",
        )

        actions, active = monitor.plan_notifications([issue], {}, self.now, 3600)

        self.assertEqual(len(actions), 1)
        self.assertNotIn("notification_details", active[issue.key])
        self.assertNotIn("1.1.1.1", str(active))

    def test_client_ip_detail_config_is_opt_in_and_bounded(self):
        config = monitor.default_config()
        self.assertEqual(config["client_ip_details"]["exposure"], "none")

        config["client_ip_details"]["exposure"] = "masked"
        with self.assertRaises(monitor.MonitorError):
            monitor.validate_config(config)

        config = monitor.default_config()
        config["client_ip_details"]["max_entries"] = 21
        with self.assertRaises(monitor.MonitorError):
            monitor.validate_config(config)

    def test_client_ip_details_stay_within_notification_size_budget(self):
        self.config["client_ip_details"]["exposure"] = "full"
        self.config["client_ip_details"]["max_entries"] = 20
        observations = {
            f"198.51.100.{index}": {
                "first_seen": self.now - 120,
                "last_seen": self.now - 60,
                "sources": {f"node-{source}-" + "x" * 70 for source in range(5)},
            }
            for index in range(1, 21)
        }

        details = monitor.render_client_ip_details(observations, self.config, self.now)

        self.assertLessEqual(len(details), monitor.MAX_NOTIFICATION_DETAILS_CHARS)
        self.assertIn("个 IP 未展示", details)

    def test_traffic_check_uses_first_run_as_baseline(self):
        connection = make_database()
        self.addCleanup(connection.close)
        connection.execute(
            "INSERT INTO client_traffics VALUES (?, ?, ?)",
            ("primary@example", 10_000, 20_000),
        )
        first_state = {}

        first_issues = monitor.collect_database_issues(
            connection, self.config, first_state, self.now
        )
        self.assertFalse(any(item.key.startswith("client-traffic:") for item in first_issues))

        state = {
            "last_run_at": self.now - 300,
            "traffic_totals": {"primary@example": 0},
        }
        self.config["checks"]["traffic_warn_bytes_per_interval"] = 20_000
        self.config["checks"]["traffic_critical_bytes_per_interval"] = 100_000

        second_issues = monitor.collect_database_issues(
            connection, self.config, state, self.now
        )
        issue = next(item for item in second_issues if item.key.startswith("client-traffic:"))
        self.assertEqual(issue.level, "warning")

    def test_recent_ips_groups_ipv6_privacy_addresses_by_prefix(self):
        raw = (
            '[{"ip":"2001:db8:1:2::10","timestamp":1699999990},'
            '{"ip":"2001:db8:1:2::20","timestamp":1699999980},'
            '{"ip":"2001:db8:1:3::10","timestamp":1699999970}]'
        )

        addresses = monitor.parse_recent_ips(raw, self.now, 1800)

        self.assertEqual(addresses, {"2001:db8:1:2::/64", "2001:db8:1:3::/64"})

    def test_notifications_are_deduplicated_and_recovery_is_emitted(self):
        issue = monitor.Issue("node:1:offline", "critical", "节点离线", "node-a")

        first_actions, first_active = monitor.plan_notifications(
            [issue], {}, self.now, 3600
        )
        self.assertEqual([item.kind for item in first_actions], ["alert"])

        second_actions, second_active = monitor.plan_notifications(
            [issue], first_active, self.now + 300, 3600
        )
        self.assertEqual(second_actions, [])

        changed_detail = issue._replace(message="node-a remains offline")
        detail_actions, detail_active = monitor.plan_notifications(
            [changed_detail], second_active, self.now + 400, 3600
        )
        self.assertEqual(detail_actions, [])

        recovery_actions, recovery_active = monitor.plan_notifications(
            [], detail_active, self.now + 600, 3600
        )
        self.assertEqual([item.kind for item in recovery_actions], ["recovery"])
        self.assertEqual(recovery_active, {})

    def test_disabled_notifications_do_not_mark_anomaly_as_notified(self):
        issue = monitor.Issue("node:1:offline", "critical", "节点离线", "node-a")

        actions, active = monitor.plan_notifications(
            [issue],
            {},
            self.now,
            3600,
            notifications_enabled=False,
        )

        self.assertEqual(actions, [])
        self.assertEqual(active[issue.key]["last_notified"], 0)

        enabled_actions, _ = monitor.plan_notifications(
            [issue],
            active,
            self.now + 60,
            3600,
            notifications_enabled=True,
        )
        self.assertEqual([item.kind for item in enabled_actions], ["alert"])

    def test_stale_node_heartbeat_is_critical(self):
        connection = make_database()
        self.addCleanup(connection.close)
        connection.execute(
            "INSERT INTO nodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (1, "guid-a", "node-a", 1, "online", self.now - 600, 50, 5, 10, "running", 0, "", ""),
        )

        issues = monitor.collect_database_issues(
            connection, self.config, {}, self.now
        )

        issue = next(item for item in issues if item.key == "node:1:heartbeat")
        self.assertEqual(issue.level, "critical")

    @mock.patch.object(monitor.subprocess, "run")
    @mock.patch.object(monitor.shutil, "which")
    def test_service_check_auto_selects_systemd(self, which, run):
        which.side_effect = lambda name: (
            "/usr/bin/systemctl" if name == "systemctl" else None
        )
        run.return_value = mock.Mock(
            returncode=0,
            stdout="active\n",
        )

        issue = monitor.check_local_service(self.config["runtime"])

        self.assertIsNone(issue)
        self.assertEqual(
            run.call_args.args[0],
            ["/usr/bin/systemctl", "is-active", "x-ui.service"],
        )

    def test_display_text_removes_control_characters_and_limits_length(self):
        value = "client\nname\x00" + "x" * 200

        display = monitor.safe_display(value, 20)

        self.assertNotIn("\n", display)
        self.assertNotIn("\x00", display)
        self.assertEqual(len(display), 20)

    @mock.patch.object(monitor.subprocess, "run")
    def test_alertbridge_actions_use_structured_titles_and_stable_dedupe_keys(self, run):
        run.return_value = mock.Mock(returncode=0, stdout="notification_sent", stderr="")
        actions = [
            monitor.NotificationAction(
                "alert",
                monitor.Issue(
                    "node:1:status",
                    "critical",
                    "节点离线",
                    "US-01 状态为 offline",
                    "IP 明细：1.1.1.1",
                ),
            ),
            monitor.NotificationAction(
                "recovery",
                monitor.Issue("node:1:status", "recovery", "节点离线已恢复", "此前状态：US-01 状态为 offline"),
            ),
        ]

        monitor.send_actions(actions, self.config)

        self.assertEqual(run.call_count, 2)
        alert_command = run.call_args_list[0].args[0]
        recovery_command = run.call_args_list[1].args[0]
        self.assertIn(
            "/usr/local/lib/threexui-script-monitor/alertbridge_notify.py",
            alert_command,
        )
        self.assertEqual(alert_command[alert_command.index("--status") + 1], "firing")
        self.assertEqual(alert_command[alert_command.index("--severity") + 1], "critical")
        self.assertEqual(alert_command[alert_command.index("--dedupe-key") + 1], "node:1:status")
        self.assertEqual(
            alert_command[alert_command.index("--title") + 1],
            "3x-ui Monitor：节点离线",
        )
        self.assertIn(
            "监控对象：3x-ui Monitor",
            alert_command[alert_command.index("--message") + 1],
        )
        self.assertIn("IP 明细：1.1.1.1", alert_command[alert_command.index("--message") + 1])
        self.assertEqual(recovery_command[recovery_command.index("--status") + 1], "resolved")
        self.assertEqual(recovery_command[recovery_command.index("--severity") + 1], "info")
        self.assertEqual(recovery_command[recovery_command.index("--dedupe-key") + 1], "node:1:status")

    @mock.patch.object(monitor.subprocess, "run")
    def test_notification_templates_and_disabled_switch_are_honored(self, run):
        run.return_value = mock.Mock(returncode=0, stdout="notification_sent", stderr="")
        self.config["instance"] = {"name": "SG-Panel-01", "location": "Singapore"}
        self.config["notifications"]["title_template"] = (
            "[{severity}] {instance_name} / {event_title}"
        )
        self.config["notifications"]["message_template"] = (
            "{instance_location}\n{event_status}\n{event_message}\n{event_details}"
        )
        actions = [
            monitor.NotificationAction(
                "alert",
                monitor.Issue("node:1:status", "critical", "节点离线", "US-01 offline"),
            )
        ]

        monitor.send_actions(actions, self.config)

        command = run.call_args.args[0]
        self.assertEqual(
            command[command.index("--title") + 1],
            "[critical] SG-Panel-01 / 节点离线",
        )
        self.assertEqual(
            command[command.index("--message") + 1],
            "Singapore\n检测到异常\nUS-01 offline",
        )

        run.reset_mock()
        self.config["notifications"]["enabled"] = False
        monitor.send_actions(actions, self.config)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
