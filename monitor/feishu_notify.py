#!/usr/bin/env python3
"""Send notifications to a Feishu or Lark custom bot webhook."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = "/etc/threexui-script-monitor/feishu.json"
MAX_CONFIG_BYTES = 16 * 1024
MAX_BODY_BYTES = 19 * 1024
ALLOWED_HOSTS = {"open.feishu.cn", "open.larksuite.com"}
WEBHOOK_PATH_PATTERN = re.compile(
    r"^/open-apis/bot/v2/hook/[A-Za-z0-9_-]{8,256}$"
)
CONFIG_KEYS = {
    "version",
    "provider",
    "webhook_url",
    "signing_secret",
    "keyword",
}


class NotifyError(RuntimeError):
    """A safe-to-display notification error."""


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Prevent a webhook token from being forwarded through a redirect."""

    def redirect_request(
        self,
        _request: urllib.request.Request,
        _file_pointer: Any,
        _code: int,
        _message: str,
        _headers: Any,
        _new_url: str,
    ) -> None:
        return None


def generate_signature(secret: str, timestamp: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(string_to_sign, digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def validate_webhook_url(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value.strip())
        port = parsed.port
    except ValueError as exc:
        raise NotifyError("Feishu webhook URL is invalid") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname not in ALLOWED_HOSTS
        or port not in (None, 443)
    ):
        raise NotifyError(
            "Feishu webhook URL must use the official HTTPS endpoint"
        )
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not WEBHOOK_PATH_PATTERN.fullmatch(parsed.path)
    ):
        raise NotifyError("Feishu webhook URL is invalid")
    return urllib.parse.urlunsplit(
        ("https", parsed.netloc, parsed.path, "", "")
    )


def load_config(
    path: str,
    *,
    require_owner: bool = True,
) -> dict[str, Any]:
    config_path = Path(path)
    try:
        file_stat = config_path.stat()
    except OSError as exc:
        raise NotifyError("notification config is unavailable") from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise NotifyError("notification config is not a regular file")
    if file_stat.st_mode & 0o077:
        raise NotifyError("notification config permissions must be 0600")
    if require_owner and file_stat.st_uid not in {0, os.geteuid()}:
        raise NotifyError("notification config owner is invalid")
    if file_stat.st_size > MAX_CONFIG_BYTES:
        raise NotifyError("notification config is unexpectedly large")
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NotifyError("notification config is invalid") from exc
    if not isinstance(data, dict):
        raise NotifyError("notification config must be an object")

    unknown = sorted(set(data) - CONFIG_KEYS)
    if unknown:
        raise NotifyError(f"unknown notification config key: {unknown[0]}")
    if data.get("version", 1) != 1:
        raise NotifyError("unsupported notification config version")
    if data.get("provider") != "feishu_webhook":
        raise NotifyError("notification provider must be feishu_webhook")

    webhook_url = data.get("webhook_url")
    if not isinstance(webhook_url, str):
        raise NotifyError("Feishu webhook URL is missing")
    normalized_webhook_url = validate_webhook_url(webhook_url)

    signing_secret = data.get("signing_secret", "")
    if (
        not isinstance(signing_secret, str)
        or len(signing_secret) > 1_024
        or "\n" in signing_secret
        or "\r" in signing_secret
    ):
        raise NotifyError("Feishu signing secret is invalid")

    keyword = data.get("keyword", "")
    if (
        not isinstance(keyword, str)
        or len(keyword) > 64
        or "\n" in keyword
        or "\r" in keyword
    ):
        raise NotifyError("Feishu keyword is invalid")

    return {
        "version": 1,
        "provider": "feishu_webhook",
        "webhook_url": normalized_webhook_url,
        "signing_secret": signing_secret,
        "keyword": keyword.strip(),
    }


def build_payload(
    config: dict[str, Any],
    *,
    title: str,
    message: str,
    timestamp: str | None = None,
) -> dict[str, Any]:
    clean_title = title.strip()
    clean_message = message.strip()
    if (
        not clean_title
        or len(clean_title) > 200
        or "\n" in clean_title
        or "\r" in clean_title
    ):
        raise NotifyError("title is invalid")
    if not clean_message or len(clean_message) > 4_000:
        raise NotifyError("message is invalid")

    keyword = str(config.get("keyword", "")).strip()
    text = f"{clean_title}\n{clean_message}"
    if keyword and not text.startswith(f"【{keyword}】"):
        text = f"【{keyword}】{text}"
    payload: dict[str, Any] = {
        "msg_type": "text",
        "content": {"text": text},
    }

    signing_secret = str(config.get("signing_secret", ""))
    if signing_secret:
        signed_at = timestamp or str(int(time.time()))
        if not signed_at.isdigit():
            raise NotifyError("timestamp is invalid")
        payload["timestamp"] = signed_at
        payload["sign"] = generate_signature(signing_secret, signed_at)
    return payload


def send_webhook(
    config: dict[str, Any],
    payload: dict[str, Any],
    timeout: int = 10,
) -> dict[str, str]:
    body = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(body) > MAX_BODY_BYTES:
        raise NotifyError("notification body exceeds the Feishu limit")
    request = urllib.request.Request(
        config["webhook_url"],
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "ThreeXUIScriptMonitor/0.1",
        },
        method="POST",
    )
    opener = urllib.request.build_opener(NoRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            status_code = response.status
            response_body = response.read(64 * 1024)
    except urllib.error.HTTPError as exc:
        raise NotifyError(
            f"Feishu returned HTTP {exc.code}: request_rejected"
        ) from exc
    except urllib.error.URLError as exc:
        raise NotifyError("unable to reach Feishu") from exc
    if status_code != 200:
        raise NotifyError(f"Feishu returned unexpected HTTP {status_code}")
    try:
        result = json.loads(response_body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise NotifyError("Feishu returned an invalid response") from exc
    if not isinstance(result, dict):
        raise NotifyError("Feishu returned an invalid response")
    response_code = result.get("code", result.get("StatusCode"))
    if response_code != 0:
        raise NotifyError("Feishu rejected the notification")
    return {"outcome": "sent"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send a 3x-ui notification to a Feishu custom bot"
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument(
        "--status",
        required=True,
        choices=("firing", "resolved", "info", "test"),
    )
    parser.add_argument(
        "--severity",
        required=True,
        choices=("critical", "warning", "info"),
    )
    parser.add_argument("--title", required=True)
    parser.add_argument("--message", required=True)
    parser.add_argument("--dedupe-key", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = load_config(args.config)
        payload = build_payload(
            config,
            title=args.title,
            message=args.message,
        )
        result = send_webhook(config, payload)
    except NotifyError as exc:
        print(f"notification_failed: {exc}", file=sys.stderr)
        return 1
    print(f"notification_sent outcome={result['outcome']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
