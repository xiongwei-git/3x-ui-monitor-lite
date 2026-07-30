import base64
import hashlib
import hmac
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "monitor" / "feishu_notify.py"
SPEC = importlib.util.spec_from_file_location("feishu_notify", MODULE_PATH)
notify = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(notify)


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return b'{"code":0,"msg":"success"}'


class FakeOpener:
    def __init__(self):
        self.request = None
        self.timeout = None

    def open(self, request, timeout):
        self.request = request
        self.timeout = timeout
        return FakeResponse()


class FeishuNotifyTest(unittest.TestCase):
    def setUp(self):
        self.config = {
            "version": 1,
            "provider": "feishu_webhook",
            "webhook_url": (
                "https://open.feishu.cn/open-apis/bot/v2/hook/"
                "00000000-0000-0000-0000-000000000000"
            ),
            "signing_secret": "demo-signing-secret",
            "keyword": "3x-ui",
        }

    def test_signature_matches_feishu_algorithm(self):
        signature = notify.generate_signature(
            "demo-signing-secret",
            "1700000000",
        )
        key = b"1700000000\ndemo-signing-secret"
        expected = base64.b64encode(
            hmac.new(key, digestmod=hashlib.sha256).digest()
        ).decode("ascii")
        self.assertEqual(signature, expected)

    def test_rejects_non_official_or_malformed_webhook(self):
        with self.assertRaises(notify.NotifyError):
            notify.validate_webhook_url(
                "https://example.com/open-apis/bot/v2/hook/token"
            )
        with self.assertRaises(notify.NotifyError):
            notify.validate_webhook_url(
                "https://open.feishu.cn/open-apis/bot/v2/hook/token?next=x"
            )

    def test_load_config_is_strict_and_requires_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feishu.json"
            path.write_text(json.dumps(self.config), encoding="utf-8")
            os.chmod(path, 0o644)
            with self.assertRaisesRegex(notify.NotifyError, "0600"):
                notify.load_config(str(path))

            config = dict(self.config)
            config["webhok_url"] = config["webhook_url"]
            path.write_text(json.dumps(config), encoding="utf-8")
            os.chmod(path, 0o600)
            with self.assertRaisesRegex(notify.NotifyError, "unknown"):
                notify.load_config(str(path))

    def test_build_payload_includes_keyword_title_message_and_signature(self):
        payload = notify.build_payload(
            self.config,
            title="3x-ui Monitor：节点离线",
            message="监控对象：Panel-01",
            timestamp="1700000000",
        )

        self.assertEqual(payload["msg_type"], "text")
        self.assertEqual(payload["timestamp"], "1700000000")
        self.assertTrue(payload["sign"])
        text = payload["content"]["text"]
        self.assertTrue(text.startswith("【3x-ui】"))
        self.assertIn("节点离线", text)
        self.assertIn("Panel-01", text)

    def test_build_payload_allows_unsigned_bot_without_keyword(self):
        config = dict(self.config)
        config["signing_secret"] = ""
        config["keyword"] = ""

        payload = notify.build_payload(
            config,
            title="Panel warning",
            message="A node is offline",
        )

        self.assertNotIn("timestamp", payload)
        self.assertNotIn("sign", payload)
        self.assertEqual(
            payload["content"]["text"],
            "Panel warning\nA node is offline",
        )

    @mock.patch.object(notify.urllib.request, "build_opener")
    def test_send_webhook_posts_json_without_following_redirects(
        self,
        build_opener,
    ):
        opener = FakeOpener()
        build_opener.return_value = opener
        payload = notify.build_payload(
            self.config,
            title="Panel warning",
            message="A node is offline",
            timestamp="1700000000",
        )

        result = notify.send_webhook(self.config, payload)

        self.assertEqual(result["outcome"], "sent")
        build_opener.assert_called_once()
        self.assertEqual(
            opener.request.full_url,
            self.config["webhook_url"],
        )
        self.assertEqual(json.loads(opener.request.data)["msg_type"], "text")


if __name__ == "__main__":
    unittest.main()
