#!/usr/bin/env python3
"""Send events through either supported AlertBridge inbound API."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = "/etc/threexui-script-monitor/alertbridge.json"
MAX_CONFIG_BYTES = 16 * 1024
MAX_BODY_BYTES = 32 * 1024
MAX_NOTIFICATION_BODY_BYTES = 8 * 1024
EVENT_PATH = "/api/v1/events"
NOTIFICATION_PATH = "/api/v1/notifications"
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
COMMON_CONFIG_KEYS = {"version", "provider", "mode", "base_url"}
EVENT_CONFIG_KEYS = COMMON_CONFIG_KEYS | {
    "client_id",
    "client_secret",
    "routing_key",
    "source",
    "labels",
}
NOTIFICATION_CONFIG_KEYS = COMMON_CONFIG_KEYS | {"token", "category"}


class NotifyError(RuntimeError):
    """A safe-to-display notification error."""


def generate_signature(secret: str, timestamp: str, nonce: str, body: bytes) -> str:
    body_digest = hashlib.sha256(body).hexdigest()
    message = f"{timestamp}\n{nonce}\n{body_digest}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def validate_identifier(name: str, value: str, limit: int) -> str:
    if not value or len(value.encode("utf-8")) > limit or not IDENTIFIER_PATTERN.fullmatch(value):
        raise NotifyError(f"{name} is invalid")
    return value


def validate_base_url(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value.strip())
        port = parsed.port
    except ValueError as exc:
        raise NotifyError("base URL is invalid") from exc
    if parsed.scheme != "https" or not parsed.hostname or port not in (None, 443):
        raise NotifyError("base URL must use HTTPS on the standard port")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise NotifyError("base URL must not contain a path, credentials, query, or fragment")
    return urllib.parse.urlunsplit(("https", parsed.netloc, "", "", ""))


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
    return validate_config(data)


def validate_config(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise NotifyError("notification config must be an object")

    if data.get("version", 1) != 1:
        raise NotifyError("unsupported notification config version")
    if data.get("provider", "alertbridge") != "alertbridge":
        raise NotifyError("notification provider must be alertbridge")
    mode = data.get("mode", "events")
    if mode not in {"events", "notifications"}:
        raise NotifyError("notification mode is invalid")
    allowed_keys = (
        EVENT_CONFIG_KEYS if mode == "events" else NOTIFICATION_CONFIG_KEYS
    )
    unknown = sorted(set(data) - allowed_keys)
    if unknown:
        raise NotifyError(f"unknown notification config key: {unknown[0]}")
    base_url = validate_base_url(str(data.get("base_url", "")))
    if mode == "notifications":
        token = str(data.get("token", "")).strip()
        if not token or len(token) > 4_096:
            raise NotifyError("Bearer token is missing or invalid")
        category = str(data.get("category", "3x-ui")).strip()
        if (
            not category
            or len(category) > 120
            or "\n" in category
            or "\r" in category
        ):
            raise NotifyError("notification category is invalid")
        return {
            "version": 1,
            "provider": "alertbridge",
            "mode": mode,
            "base_url": base_url,
            "token": token,
            "category": category,
        }

    client_id = validate_identifier("client ID", str(data.get("client_id", "")).strip(), 64)
    routing_key = validate_identifier("routing key", str(data.get("routing_key", "")).strip(), 64)
    source = validate_identifier("source", str(data.get("source", "")).strip(), 64)
    client_secret = str(data.get("client_secret", "")).strip()
    if not client_secret or len(client_secret) > 1024:
        raise NotifyError("client secret is missing or invalid")
    labels = data.get("labels", {})
    if not isinstance(labels, dict) or len(labels) > 20:
        raise NotifyError("labels are invalid")
    normalized_labels: dict[str, str] = {}
    for key, value in labels.items():
        normalized_key = validate_identifier("label key", str(key), 64)
        normalized_value = str(value)
        if len(normalized_value) > 256:
            raise NotifyError("label value is too long")
        normalized_labels[normalized_key] = normalized_value
    return {
        "version": 1,
        "provider": "alertbridge",
        "mode": mode,
        "base_url": base_url,
        "client_id": client_id,
        "client_secret": client_secret,
        "routing_key": routing_key,
        "source": source,
        "labels": normalized_labels,
    }


def build_event(
    config: dict[str, Any],
    *,
    event_id: str,
    status: str,
    severity: str,
    title: str,
    message: str,
    dedupe_key: str,
    occurred_at: str,
) -> dict[str, Any]:
    validate_identifier("event ID", event_id, 128)
    if status not in {"firing", "resolved", "info", "test"}:
        raise NotifyError("status is invalid")
    if severity not in {"critical", "warning", "info"}:
        raise NotifyError("severity is invalid")
    clean_title = title.strip()
    clean_message = message.strip()
    if not clean_title or len(clean_title) > 200 or "\n" in clean_title or "\r" in clean_title:
        raise NotifyError("title is invalid")
    if not clean_message or len(clean_message) > 4000:
        raise NotifyError("message is invalid")
    if status in {"firing", "resolved"}:
        validate_identifier("dedupe key", dedupe_key, 128)
    elif dedupe_key:
        validate_identifier("dedupe key", dedupe_key, 128)
    if not occurred_at:
        raise NotifyError("occurred_at is missing")

    event: dict[str, Any] = {
        "event_id": event_id,
        "source": config["source"],
        "routing_key": config["routing_key"],
        "status": status,
        "severity": severity,
        "title": clean_title,
        "message": clean_message,
        "occurred_at": occurred_at,
    }
    if dedupe_key:
        event["dedupe_key"] = dedupe_key
    if config.get("labels"):
        event["labels"] = config["labels"]
    return event


def send_event(config: dict[str, Any], event: dict[str, Any], timeout: int = 10) -> dict[str, Any]:
    body = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_BODY_BYTES:
        raise NotifyError("notification body exceeds the API limit")
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(12)
    signature = generate_signature(config["client_secret"], timestamp, nonce, body)
    request = urllib.request.Request(
        config["base_url"] + EVENT_PATH,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Notify-Client": config["client_id"],
            "X-Notify-Timestamp": timestamp,
            "X-Notify-Nonce": nonce,
            "X-Notify-Signature": signature,
            "User-Agent": "ThreeXUIScriptMonitor/0.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status_code = response.status
            response_body = response.read(64 * 1024)
    except urllib.error.HTTPError as exc:
        error_code = "request_rejected"
        try:
            error_code = str(json.loads(exc.read(64 * 1024)).get("error", {}).get("code", error_code))
        except (AttributeError, UnicodeError, json.JSONDecodeError):
            pass
        raise NotifyError(f"AlertBridge returned HTTP {exc.code}: {error_code[:80]}") from exc
    except urllib.error.URLError as exc:
        raise NotifyError("unable to reach AlertBridge") from exc
    if status_code != 202:
        raise NotifyError(f"AlertBridge returned unexpected HTTP {status_code}")
    try:
        result = json.loads(response_body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise NotifyError("AlertBridge returned an invalid response") from exc
    if result.get("outcome") not in {"queued", "suppressed", "duplicate"}:
        raise NotifyError("AlertBridge returned an unknown outcome")
    return result


def build_notification(
    config: dict[str, Any],
    *,
    title: str,
    message: str,
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
    return {
        "title": clean_title,
        "message": clean_message,
        "category": config["category"],
    }


def send_notification(
    config: dict[str, Any],
    notification: dict[str, Any],
    timeout: int = 10,
) -> dict[str, Any]:
    body = json.dumps(
        notification,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(body) > MAX_NOTIFICATION_BODY_BYTES:
        raise NotifyError("notification body exceeds the API limit")
    request = urllib.request.Request(
        config["base_url"] + NOTIFICATION_PATH,
        data=body,
        headers={
            "Authorization": f"Bearer {config['token']}",
            "Content-Type": "application/json",
            "User-Agent": "ThreeXUIScriptMonitor/0.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status_code = response.status
            response_body = response.read(64 * 1024)
    except urllib.error.HTTPError as exc:
        raise NotifyError(
            f"AlertBridge returned HTTP {exc.code}: request_rejected"
        ) from exc
    except urllib.error.URLError as exc:
        raise NotifyError("unable to reach AlertBridge") from exc
    if status_code != 200:
        raise NotifyError(f"AlertBridge returned unexpected HTTP {status_code}")
    try:
        result = json.loads(response_body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise NotifyError("AlertBridge returned an invalid response") from exc
    if result.get("outcome") not in {"queued", "suppressed", "duplicate"}:
        raise NotifyError("AlertBridge returned an unknown outcome")
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send a 3x-ui event to AlertBridge")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--status", required=True, choices=("firing", "resolved", "info", "test"))
    parser.add_argument("--severity", required=True, choices=("critical", "warning", "info"))
    parser.add_argument("--title", required=True)
    parser.add_argument("--message", required=True)
    parser.add_argument("--dedupe-key", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = load_config(args.config)
        if config["mode"] == "events":
            now = (
                datetime.now(timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z")
            )
            event = build_event(
                config,
                event_id=(
                    f"threexui-{int(time.time())}-{secrets.token_hex(8)}"
                ),
                status=args.status,
                severity=args.severity,
                title=args.title,
                message=args.message,
                dedupe_key=args.dedupe_key,
                occurred_at=now,
            )
            result = send_event(config, event)
        else:
            notification = build_notification(
                config,
                title=args.title,
                message=args.message,
            )
            result = send_notification(config, notification)
    except NotifyError as exc:
        print(f"notification_failed: {exc}", file=sys.stderr)
        return 1
    print(f"notification_sent outcome={result['outcome']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
