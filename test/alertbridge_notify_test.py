import hashlib
import hmac
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "monitor" / "alertbridge_notify.py"
SPEC = importlib.util.spec_from_file_location("alertbridge_notify", MODULE_PATH)
notify = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(notify)


class FakeResponse:
    def __init__(self, status=202, body=None):
        self.status = status
        self.body = body or b'{"event_id":"event-1","outcome":"queued","deliveries":1}'

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self.body


class AlertBridgeNotifyTest(unittest.TestCase):
    def setUp(self):
        self.config = {
            "version": 1,
            "mode": "events",
            "base_url": "https://notify.example.com",
            "client_id": "proxymonitor",
            "client_secret": "demo-secret",
            "routing_key": "base",
            "source": "threexui-monitor",
            "labels": {"server": "Panel-01", "service": "3x-ui"},
        }

    def test_load_config_rejects_unknown_keys(self):
        config = dict(self.config)
        config["client_secert"] = "typo"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alertbridge.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            os.chmod(path, 0o600)

            with self.assertRaisesRegex(notify.NotifyError, "unknown"):
                notify.load_config(str(path))

    def test_load_config_supports_bearer_notification_mode(self):
        config = {
            "version": 1,
            "mode": "notifications",
            "base_url": "https://notify.example.com",
            "token": "demo-token",
            "category": "3x-ui",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alertbridge.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            os.chmod(path, 0o600)

            loaded = notify.load_config(str(path))

        self.assertEqual(loaded["mode"], "notifications")
        self.assertEqual(loaded["token"], "demo-token")
        self.assertEqual(loaded["category"], "3x-ui")

    def test_signature_matches_openssl_example(self):
        body = b'{"event_id":"test"}'
        signature = notify.generate_signature(
            "demo-secret", "1700000000", "abcdef1234567890abcdef12", body
        )
        self.assertEqual(
            signature,
            "c0b1f0d651b55d1e182f52a8789c1ed81b1ba19434f9f3b3caf177df1f99d021",
        )

    def test_rejects_non_https_or_non_root_base_url(self):
        with self.assertRaises(notify.NotifyError):
            notify.validate_base_url("http://notify.example.com")
        with self.assertRaises(notify.NotifyError):
            notify.validate_base_url("https://notify.example.com/admin")

    def test_load_config_requires_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alertbridge.json"
            path.write_text(json.dumps(self.config), encoding="utf-8")
            os.chmod(path, 0o644)
            with self.assertRaises(notify.NotifyError):
                notify.load_config(str(path))

    def test_build_event_preserves_monitor_title_and_content(self):
        event = notify.build_event(
            self.config,
            event_id="event-1",
            status="firing",
            severity="critical",
            title="3x-ui Monitor：节点离线",
            message="监控对象：Panel-01\n详细信息：node-01 离线",
            dedupe_key="node:1:status",
            occurred_at="2026-07-16T01:02:03Z",
        )
        self.assertEqual(event["source"], "threexui-monitor")
        self.assertEqual(event["routing_key"], "base")
        self.assertEqual(event["status"], "firing")
        self.assertEqual(event["severity"], "critical")
        self.assertEqual(event["title"], "3x-ui Monitor：节点离线")
        self.assertIn("Panel-01", event["message"])
        self.assertEqual(event["dedupe_key"], "node:1:status")
        self.assertEqual(event["labels"]["service"], "3x-ui")

    @mock.patch.object(notify.urllib.request, "urlopen", return_value=FakeResponse())
    def test_send_event_signs_exact_body_and_accepts_queued_response(self, urlopen):
        event = notify.build_event(
            self.config,
            event_id="event-1",
            status="test",
            severity="info",
            title="3x-ui 通知链路测试",
            message="AlertBridge 集成测试。",
            dedupe_key="",
            occurred_at="2026-07-16T01:02:03Z",
        )
        with mock.patch.object(notify.time, "time", return_value=1_700_000_000), mock.patch.object(
            notify.secrets, "token_hex", return_value="abcdef1234567890abcdef12"
        ):
            result = notify.send_event(self.config, event)

        self.assertEqual(result["outcome"], "queued")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://notify.example.com/api/v1/events")
        self.assertEqual(request.headers["X-notify-client"], "proxymonitor")
        body_digest = hashlib.sha256(request.data).hexdigest()
        message = f"1700000000\nabcdef1234567890abcdef12\n{body_digest}".encode()
        expected = hmac.new(b"demo-secret", message, hashlib.sha256).hexdigest()
        self.assertEqual(request.headers["X-notify-signature"], expected)

    @mock.patch.object(
        notify.urllib.request,
        "urlopen",
        return_value=FakeResponse(
            200,
            b'{"notification_id":"notification-1","outcome":"queued"}',
        ),
    )
    def test_send_notification_uses_bearer_api(self, urlopen):
        config = {
            "mode": "notifications",
            "base_url": "https://notify.example.com",
            "token": "demo-token",
            "category": "3x-ui",
        }
        notification = notify.build_notification(
            config,
            title="Panel warning",
            message="A node is offline",
        )

        result = notify.send_notification(config, notification)

        self.assertEqual(result["outcome"], "queued")
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://notify.example.com/api/v1/notifications",
        )
        self.assertEqual(request.headers["Authorization"], "Bearer demo-token")
        self.assertEqual(json.loads(request.data)["category"], "3x-ui")


if __name__ == "__main__":
    unittest.main()
