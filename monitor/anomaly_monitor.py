#!/usr/bin/env python3
"""Read-only 3x-ui anomaly checks with deduplicated notifications."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import shutil
import sqlite3
import stat
import string
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, NamedTuple


DEFAULT_CONFIG_PATH = "/etc/threexui-script-monitor/monitor.json"
MAX_CONFIG_BYTES = 64 * 1024
MAX_STATE_BYTES = 2 * 1024 * 1024
MAX_IP_RECORD_BYTES = 2 * 1024 * 1024
MAX_IP_RECORDS = 20_000
MAX_IP_DETAIL_ENTRIES = 20
MAX_IP_DATABASE_BYTES = 256 * 1024 * 1024
MAX_NOTIFICATION_DETAILS_CHARS = 3_000
SERVICE_PATTERN = re.compile(r"^[A-Za-z0-9_.@-]+$")
CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x1f\x7f]+")
ROOT_CONFIG_KEYS = {
    "version",
    "database_path",
    "state_path",
    "lock_path",
    "instance",
    "runtime",
    "notifications",
    "client_ip_details",
    "checks",
}
INSTANCE_CONFIG_KEYS = {"name", "location"}
RUNTIME_CONFIG_KEYS = {
    "interval_seconds",
    "service_manager",
    "service_name",
    "service_executable",
}
NOTIFICATION_CONFIG_KEYS = {
    "enabled",
    "provider",
    "notifier_path",
    "config_path",
    "title_template",
    "message_template",
}
CLIENT_IP_CONFIG_KEYS = {"exposure", "max_entries", "database_path"}
CHECK_CONFIG_KEYS = {
    "node_heartbeat_max_age_seconds",
    "node_latency_warn_ms",
    "node_cpu_warn_percent",
    "node_memory_warn_percent",
    "ip_window_seconds",
    "ip_warn_count",
    "ip_critical_count",
    "traffic_interval_seconds",
    "traffic_min_elapsed_seconds",
    "traffic_max_elapsed_seconds",
    "traffic_warn_bytes_per_interval",
    "traffic_critical_bytes_per_interval",
    "repeat_alert_seconds",
}
TITLE_TEMPLATE_FIELDS = {
    "instance_name",
    "instance_location",
    "event_title",
    "severity",
}
MESSAGE_TEMPLATE_FIELDS = TITLE_TEMPLATE_FIELDS | {
    "event_status",
    "event_message",
    "event_details",
}


class MonitorError(RuntimeError):
    """A monitor error that is safe to write to the system journal."""


class Issue(NamedTuple):
    key: str
    level: str
    title: str
    message: str
    notification_details: str = ""


class NotificationAction(NamedTuple):
    kind: str
    issue: Issue


class IPMetadata(NamedTuple):
    country: str
    region: str
    city: str
    isp: str
    country_code: str


def default_config() -> dict[str, Any]:
    return {
        "version": 1,
        "database_path": "/etc/x-ui/x-ui.db",
        "state_path": "/var/lib/threexui-script-monitor/state.json",
        "lock_path": "/var/lib/threexui-script-monitor/monitor.lock",
        "instance": {
            "name": "3x-ui Monitor",
            "location": "",
        },
        "runtime": {
            "interval_seconds": 300,
            "service_manager": "auto",
            "service_name": "x-ui.service",
            "service_executable": "/usr/local/x-ui/x-ui",
        },
        "notifications": {
            "enabled": True,
            "provider": "alertbridge",
            "notifier_path": (
                "/usr/local/lib/threexui-script-monitor/alertbridge_notify.py"
            ),
            "config_path": "/etc/threexui-script-monitor/alertbridge.json",
            "title_template": "{instance_name}：{event_title}",
            "message_template": (
                "监控对象：{instance_name}\n"
                "事件状态：{event_status}\n"
                "详细信息：{event_message}\n"
                "{event_details}"
            ),
        },
        "client_ip_details": {
            "exposure": "none",
            "max_entries": 8,
            "database_path": (
                "/usr/local/share/threexui-script-monitor/ip2region_v4.xdb"
            ),
        },
        "checks": {
            "node_heartbeat_max_age_seconds": 180,
            "node_latency_warn_ms": 2_000,
            "node_cpu_warn_percent": 95.0,
            "node_memory_warn_percent": 90.0,
            "ip_window_seconds": 1_800,
            "ip_warn_count": 4,
            "ip_critical_count": 8,
            "traffic_interval_seconds": 300,
            "traffic_min_elapsed_seconds": 60,
            "traffic_max_elapsed_seconds": 1_800,
            "traffic_warn_bytes_per_interval": 5 * 1024**3,
            "traffic_critical_bytes_per_interval": 15 * 1024**3,
            "repeat_alert_seconds": 6 * 3_600,
        },
    }


def merge_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_config(result[key], value)
        else:
            result[key] = value
    return result


def require_absolute_path(config: dict[str, Any], key: str) -> None:
    value = config.get(key)
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise MonitorError(f"{key} must be an absolute path")


def reject_unknown_keys(
    config: dict[str, Any],
    allowed: set[str],
    prefix: str = "",
) -> None:
    for key in sorted(set(config) - allowed):
        name = f"{prefix}.{key}" if prefix else key
        raise MonitorError(f"unknown config key: {name}")


def normalize_legacy_config(override: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(override)
    if "runtime" in normalized and not isinstance(normalized["runtime"], dict):
        raise MonitorError("runtime must be an object")
    if "notifications" in normalized and not isinstance(
        normalized["notifications"],
        dict,
    ):
        raise MonitorError("notifications must be an object")
    runtime = dict(normalized.get("runtime", {}))
    notifications = dict(normalized.get("notifications", {}))
    aliases = (
        ("local_service_name", runtime, "service_name"),
        ("notifier_path", notifications, "notifier_path"),
        ("notifier_config_path", notifications, "config_path"),
    )
    for legacy_key, group, current_key in aliases:
        if legacy_key not in normalized:
            continue
        legacy_value = normalized.pop(legacy_key)
        if current_key in group and group[current_key] != legacy_value:
            raise MonitorError(
                f"{legacy_key} conflicts with its current config field"
            )
        group[current_key] = legacy_value
    if runtime:
        normalized["runtime"] = runtime
    if notifications:
        normalized["notifications"] = notifications
    return normalized


def validate_text(name: str, value: Any, limit: int, *, required: bool) -> str:
    if not isinstance(value, str):
        raise MonitorError(f"{name} must be text")
    if required and not value.strip():
        raise MonitorError(f"{name} must not be empty")
    if len(value) > limit or CONTROL_CHARACTER_PATTERN.search(value):
        raise MonitorError(f"{name} is invalid")
    return value


def validate_template(name: str, value: Any, allowed_fields: set[str]) -> str:
    if not isinstance(value, str) or not value or len(value) > 4_000:
        raise MonitorError(f"{name} is invalid")
    try:
        parsed = string.Formatter().parse(value)
    except ValueError as exc:
        raise MonitorError(f"{name} is invalid") from exc
    for _literal, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if field_name not in allowed_fields:
            raise MonitorError(f"{name} contains an unsupported placeholder")
        if format_spec:
            raise MonitorError(f"{name} format specifications are not allowed")
        if conversion:
            raise MonitorError(f"{name} conversion is not allowed")
    return value


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    reject_unknown_keys(config, ROOT_CONFIG_KEYS)
    if config.get("version") != 1:
        raise MonitorError("unsupported monitor config version")
    for key in ("database_path", "state_path", "lock_path"):
        require_absolute_path(config, key)

    instance = config.get("instance")
    if not isinstance(instance, dict):
        raise MonitorError("instance must be an object")
    reject_unknown_keys(instance, INSTANCE_CONFIG_KEYS, "instance")
    validate_text("instance.name", instance.get("name"), 120, required=True)
    validate_text(
        "instance.location",
        instance.get("location"),
        120,
        required=False,
    )

    runtime = config.get("runtime")
    if not isinstance(runtime, dict):
        raise MonitorError("runtime must be an object")
    reject_unknown_keys(runtime, RUNTIME_CONFIG_KEYS, "runtime")
    interval = runtime.get("interval_seconds")
    if (
        not isinstance(interval, int)
        or isinstance(interval, bool)
        or not 60 <= interval <= 86_400
    ):
        raise MonitorError(
            "runtime.interval_seconds must be an integer from 60 to 86400"
        )
    if runtime.get("service_manager") not in {
        "auto",
        "systemd",
        "launchd",
        "openrc",
        "process",
    }:
        raise MonitorError("runtime.service_manager is invalid")
    service = runtime.get("service_name")
    if not isinstance(service, str) or not SERVICE_PATTERN.fullmatch(service):
        raise MonitorError("runtime.service_name is invalid")
    require_absolute_path(runtime, "service_executable")

    notifications = config.get("notifications")
    if not isinstance(notifications, dict):
        raise MonitorError("notifications must be an object")
    reject_unknown_keys(
        notifications,
        NOTIFICATION_CONFIG_KEYS,
        "notifications",
    )
    if not isinstance(notifications.get("enabled"), bool):
        raise MonitorError("notifications.enabled must be true or false")
    if notifications.get("provider") not in {
        "alertbridge",
        "feishu_webhook",
    }:
        raise MonitorError("notifications.provider is unsupported")
    require_absolute_path(notifications, "notifier_path")
    require_absolute_path(notifications, "config_path")
    validate_template(
        "notifications.title_template",
        notifications.get("title_template"),
        TITLE_TEMPLATE_FIELDS,
    )
    validate_template(
        "notifications.message_template",
        notifications.get("message_template"),
        MESSAGE_TEMPLATE_FIELDS,
    )

    client_ip_details = config.get("client_ip_details")
    if not isinstance(client_ip_details, dict):
        raise MonitorError("client_ip_details must be an object")
    reject_unknown_keys(
        client_ip_details,
        CLIENT_IP_CONFIG_KEYS,
        "client_ip_details",
    )
    if client_ip_details.get("exposure") not in ("none", "full"):
        raise MonitorError("client_ip_details.exposure must be none or full")
    max_entries = client_ip_details.get("max_entries")
    if (
        not isinstance(max_entries, int)
        or isinstance(max_entries, bool)
        or not 1 <= max_entries <= MAX_IP_DETAIL_ENTRIES
    ):
        raise MonitorError(
            f"client_ip_details.max_entries must be an integer from 1 to {MAX_IP_DETAIL_ENTRIES}"
        )
    require_absolute_path(client_ip_details, "database_path")

    checks = config.get("checks")
    if not isinstance(checks, dict):
        raise MonitorError("checks must be an object")
    reject_unknown_keys(checks, CHECK_CONFIG_KEYS, "checks")
    for key in CHECK_CONFIG_KEYS:
        value = checks.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise MonitorError(f"checks.{key} must be a positive number")
    if checks["ip_warn_count"] > checks["ip_critical_count"]:
        raise MonitorError("IP warning threshold cannot exceed critical threshold")
    if checks["traffic_warn_bytes_per_interval"] > checks["traffic_critical_bytes_per_interval"]:
        raise MonitorError("traffic warning threshold cannot exceed critical threshold")
    if checks["traffic_min_elapsed_seconds"] > checks["traffic_max_elapsed_seconds"]:
        raise MonitorError("traffic elapsed range is invalid")
    return config


def load_config(path: str) -> dict[str, Any]:
    config_path = Path(path)
    try:
        file_stat = config_path.stat()
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > MAX_CONFIG_BYTES:
            raise MonitorError("monitor config is invalid")
        if file_stat.st_mode & 0o022:
            raise MonitorError(
                "monitor config permissions must not allow group or other writes"
            )
        allowed_owners = {0, os.geteuid()}
        if file_stat.st_uid not in allowed_owners:
            raise MonitorError("monitor config owner is invalid")
        override = json.loads(config_path.read_text(encoding="utf-8"))
    except MonitorError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MonitorError("monitor config is unavailable or invalid") from exc
    if not isinstance(override, dict):
        raise MonitorError("monitor config must be an object")
    normalized = normalize_legacy_config(override)
    return validate_config(merge_config(default_config(), normalized))


def load_state(path: str) -> dict[str, Any]:
    state_path = Path(path)
    try:
        file_stat = state_path.stat()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise MonitorError("monitor state is unavailable") from exc
    if (
        not stat.S_ISREG(file_stat.st_mode)
        or file_stat.st_mode & 0o077
        or file_stat.st_size > MAX_STATE_BYTES
    ):
        raise MonitorError("monitor state is invalid")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MonitorError("monitor state is invalid") from exc
    return state if isinstance(state, dict) and state.get("version") == 1 else {}


def save_state(path: str, state: dict[str, Any]) -> None:
    state_path = Path(path)
    state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state_path.parent, 0o700)
    payload = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(payload.encode("utf-8")) > MAX_STATE_BYTES:
        raise MonitorError("monitor state is unexpectedly large")
    descriptor, temporary = tempfile.mkstemp(prefix=".state-", dir=state_path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, state_path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def stable_client_key(prefix: str, email: str) -> str:
    digest = hashlib.sha256(email.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:{digest}"


def safe_display(value: Any, limit: int = 120) -> str:
    text = CONTROL_CHARACTER_PATTERN.sub(" ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 3)] + "..."


def table_has_columns(
    connection: sqlite3.Connection,
    table: str,
    required: set[str],
) -> bool:
    if not re.fullmatch(r"[a-z_]+", table):
        return False
    try:
        columns = {
            str(row["name"])
            for row in connection.execute(f'PRAGMA table_info("{table}")')
        }
    except sqlite3.Error:
        return False
    return required <= columns


def normalized_timestamp(value: Any) -> int | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    timestamp = int(value)
    if timestamp > 10_000_000_000:
        timestamp //= 1_000
    return timestamp if timestamp > 0 else None


def human_bytes(value: float) -> str:
    size = float(max(0, value))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def canonical_address(value: str) -> str | None:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return str(address.ipv4_mapped)
    if isinstance(address, ipaddress.IPv6Address):
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


def collect_node_issues(connection: sqlite3.Connection, config: dict[str, Any], now: int) -> list[Issue]:
    if not table_has_columns(
        connection,
        "nodes",
        {
            "id",
            "name",
            "enable",
            "status",
            "last_heartbeat",
            "latency_ms",
            "cpu_pct",
            "mem_pct",
            "xray_state",
            "config_dirty",
        },
    ):
        return []
    checks = config["checks"]
    issues: list[Issue] = []
    query = """
        SELECT id, name, status, last_heartbeat, latency_ms, cpu_pct, mem_pct,
               xray_state, config_dirty
        FROM nodes WHERE enable = 1 ORDER BY id
    """
    for row in connection.execute(query):
        node_id = int(row["id"])
        name = safe_display(row["name"] or f"node-{node_id}", 80)
        status = safe_display(row["status"] or "unknown", 40).lower()
        if status != "online":
            issues.append(Issue(f"node:{node_id}:status", "critical", "节点离线", f"{name} 状态为 {status}"))
        else:
            heartbeat = normalized_timestamp(row["last_heartbeat"])
            age = now - heartbeat if heartbeat else now
            if age > checks["node_heartbeat_max_age_seconds"]:
                issues.append(
                    Issue(
                        f"node:{node_id}:heartbeat",
                        "critical",
                        "节点心跳超时",
                        f"{name} 已 {age} 秒没有有效心跳",
                    )
                )
        xray_state = safe_display(row["xray_state"] or "unknown", 40).lower()
        if xray_state != "running":
            issues.append(Issue(f"node:{node_id}:xray", "critical", "Xray 状态异常", f"{name} 的 Xray 状态为 {xray_state}"))
        if bool(row["config_dirty"]):
            issues.append(Issue(f"node:{node_id}:config", "warning", "节点配置未同步", f"{name} 存在待同步配置"))
        latency = float(row["latency_ms"] or 0)
        if latency > checks["node_latency_warn_ms"]:
            issues.append(Issue(f"node:{node_id}:latency", "warning", "节点延迟过高", f"{name} 控制链路延迟为 {latency:.0f} ms"))
        cpu = float(row["cpu_pct"] or 0)
        if cpu >= checks["node_cpu_warn_percent"]:
            issues.append(Issue(f"node:{node_id}:cpu", "warning", "节点 CPU 使用率过高", f"{name} CPU 使用率为 {cpu:.1f}%"))
        memory = float(row["mem_pct"] or 0)
        if memory >= checks["node_memory_warn_percent"]:
            issues.append(Issue(f"node:{node_id}:memory", "warning", "节点内存使用率过高", f"{name} 内存使用率为 {memory:.1f}%"))
    return issues


def parse_recent_ip_records(raw: Any, now: int, window: int) -> list[tuple[str, int]]:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_IP_RECORD_BYTES:
        return []
    try:
        records = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(records, list):
        return []
    result: list[tuple[str, int]] = []
    for record in records[:MAX_IP_RECORDS]:
        if not isinstance(record, dict):
            continue
        address = record.get("ip")
        timestamp = normalized_timestamp(record.get("timestamp"))
        if not isinstance(address, str) or not address or timestamp is None:
            continue
        age = now - timestamp
        normalized = canonical_address(address)
        if normalized and -300 <= age <= window:
            result.append((normalized, timestamp))
    return result


def parse_recent_ips(raw: Any, now: int, window: int) -> set[str]:
    return {address for address, _ in parse_recent_ip_records(raw, now, window)}


def _node_names_by_guid(connection: sqlite3.Connection) -> dict[str, str]:
    if not table_has_columns(connection, "nodes", {"guid", "name"}):
        return {}
    try:
        rows = connection.execute("SELECT guid, name FROM nodes")
    except sqlite3.OperationalError:
        return {}
    names: dict[str, str] = {}
    for row in rows:
        guid = str(row["guid"] or "").strip()
        if guid:
            names[guid] = safe_display(row["name"] or "远程节点", 80)
    return names


def _record_client_ip(
    clients: dict[str, dict[str, dict[str, Any]]],
    email: str,
    address: str,
    timestamp: int,
    source: str,
) -> None:
    observation = clients.setdefault(email, {}).setdefault(
        address,
        {
            "first_seen": timestamp,
            "last_seen": timestamp,
            "sources": set(),
        },
    )
    observation["first_seen"] = min(int(observation["first_seen"]), timestamp)
    observation["last_seen"] = max(int(observation["last_seen"]), timestamp)
    observation["sources"].add(source)


def collect_recent_client_ips(
    connection: sqlite3.Connection,
    now: int,
    window: int,
) -> dict[str, dict[str, dict[str, Any]]]:
    clients: dict[str, dict[str, dict[str, Any]]] = {}
    if table_has_columns(
        connection,
        "inbound_client_ips",
        {"client_email", "ips"},
    ):
        for row in connection.execute(
            "SELECT client_email AS email, ips FROM inbound_client_ips"
        ):
            email = str(row["email"] or "").strip()
            if not email:
                continue
            for address, timestamp in parse_recent_ip_records(
                row["ips"],
                now,
                window,
            ):
                _record_client_ip(clients, email, address, timestamp, "主节点")

    node_names = _node_names_by_guid(connection)
    if table_has_columns(
        connection,
        "node_client_ips",
        {"node_guid", "email", "ips"},
    ):
        for row in connection.execute(
            "SELECT node_guid, email, ips FROM node_client_ips"
        ):
            email = str(row["email"] or "").strip()
            if not email:
                continue
            guid = str(row["node_guid"] or "").strip()
            source = node_names.get(guid, "远程节点")
            for address, timestamp in parse_recent_ip_records(
                row["ips"],
                now,
                window,
            ):
                _record_client_ip(clients, email, address, timestamp, source)
    return clients


def _metadata_field(value: Any) -> str:
    text = safe_display(value, 80)
    return "" if text in ("", "0") else text


def resolve_ip_metadata(addresses: list[str], database_path: str) -> dict[str, IPMetadata]:
    database = Path(database_path)
    try:
        file_stat = database.stat()
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_mode & 0o022
            or not 1_000_000 <= file_stat.st_size <= MAX_IP_DATABASE_BYTES
        ):
            return {}
        import ip2region.searcher as xdb
        import ip2region.util as util

        searcher = xdb.new_with_file_only(util.IPv4, str(database))
    except (ImportError, OSError, ValueError):
        return {}

    metadata: dict[str, IPMetadata] = {}
    try:
        for address in addresses:
            try:
                parsed = ipaddress.ip_address(address)
                if not isinstance(parsed, ipaddress.IPv4Address):
                    continue
                value = searcher.search(str(parsed))
                fields = value.split("|") if value else []
                if len(fields) < 5:
                    continue
                metadata[address] = IPMetadata(
                    _metadata_field(fields[0]),
                    _metadata_field(fields[1]),
                    _metadata_field(fields[2]),
                    _metadata_field(fields[3]),
                    _metadata_field(fields[4]).upper(),
                )
            except (IndexError, OSError, UnicodeError, ValueError):
                continue
    finally:
        searcher.close()
    return metadata


def _age_text(now: int, timestamp: int) -> str:
    minutes = max(0, now - timestamp) // 60
    return "不到 1 分钟前" if minutes == 0 else f"{minutes} 分钟前"


def _source_sort_key(source: str) -> tuple[int, str]:
    return (0 if source == "主节点" else 1, source)


def render_client_ip_details(
    observations: dict[str, dict[str, Any]],
    config: dict[str, Any],
    now: int,
) -> str:
    detail_config = config["client_ip_details"]
    if detail_config["exposure"] != "full":
        return ""
    limit = int(detail_config["max_entries"])
    ordered = sorted(
        observations.items(),
        key=lambda item: (-int(item[1]["last_seen"]), item[0]),
    )
    candidates = ordered[:limit]
    metadata = resolve_ip_metadata(
        [address for address, _ in candidates],
        detail_config["database_path"],
    )
    lines = ["IP 明细（管理员私密通知；归属由离线数据库提供）："]
    displayed = 0
    for address, observation in candidates:
        sources = "、".join(
            sorted(
                (safe_display(source, 80) for source in observation["sources"]),
                key=_source_sort_key,
            )
        )
        item = metadata.get(address)
        if item is None:
            ownership = "归属：暂不可用"
        else:
            location = " / ".join(
                value for value in (item.country, item.region, item.city) if value
            )
            ownership = f"归属：{location or item.country_code or '未知'}"
            ownership += f"｜运营商：{item.isp or '未知'}"
        line = (
            f"- {address}｜来源：{sources or '未知'}"
            f"｜首次：{_age_text(now, int(observation['first_seen']))}"
            f"｜最近：{_age_text(now, int(observation['last_seen']))}"
            f"｜{ownership}"
        )
        candidate = "\n".join(lines + [line])
        if len(candidate) > MAX_NOTIFICATION_DETAILS_CHARS - 80:
            break
        lines.append(line)
        displayed += 1
    hidden = len(ordered) - displayed
    if hidden > 0:
        lines.append(f"- 另有 {hidden} 个 IP 未展示（受 max_entries 限制）")
    return "\n".join(lines)


def collect_client_ip_issues(connection: sqlite3.Connection, config: dict[str, Any], now: int) -> list[Issue]:
    checks = config["checks"]
    clients = collect_recent_client_ips(
        connection,
        now,
        int(checks["ip_window_seconds"]),
    )
    issues: list[Issue] = []
    for email, observations in clients.items():
        count = len(observations)
        if count < checks["ip_warn_count"]:
            continue
        level = "critical" if count >= checks["ip_critical_count"] else "warning"
        minutes = int(checks["ip_window_seconds"] // 60)
        display_email = safe_display(email)
        issues.append(
            Issue(
                stable_client_key("client-ip", email),
                level,
                "客户端 IP 数异常",
                f"客户端 {display_email} 在最近 {minutes} 分钟出现 {count} 个不同公网 IP",
                render_client_ip_details(observations, config, now),
            )
        )
    return issues


def collect_traffic_totals(connection: sqlite3.Connection) -> dict[str, int]:
    totals: dict[str, int] = {}
    sources = (
        ("client_traffics", {"email", "up", "down"}),
        ("node_client_traffics", {"email", "up", "down"}),
    )
    for table, columns in sources:
        if not table_has_columns(connection, table, columns):
            continue
        query = f'SELECT email, up, down FROM "{table}"'
        for row in connection.execute(query):
            email = str(row["email"] or "").strip()
            if not email:
                continue
            amount = max(0, int(row["up"] or 0)) + max(0, int(row["down"] or 0))
            totals[email] = totals.get(email, 0) + amount
    return totals


def collect_traffic_issues(
    connection: sqlite3.Connection,
    config: dict[str, Any],
    state: dict[str, Any],
    now: int,
) -> list[Issue]:
    checks = config["checks"]
    previous = state.get("traffic_totals")
    last_run = state.get("last_run_at")
    if not isinstance(previous, dict) or not isinstance(last_run, int):
        return []
    elapsed = now - last_run
    if not checks["traffic_min_elapsed_seconds"] <= elapsed <= checks["traffic_max_elapsed_seconds"]:
        return []
    interval = checks["traffic_interval_seconds"]
    issues: list[Issue] = []
    for email, current in collect_traffic_totals(connection).items():
        old = previous.get(email)
        if not isinstance(old, int) or current < old:
            continue
        delta = current - old
        normalized = delta * interval / elapsed
        if normalized < checks["traffic_warn_bytes_per_interval"]:
            continue
        level = "critical" if normalized >= checks["traffic_critical_bytes_per_interval"] else "warning"
        display_email = safe_display(email)
        issues.append(
            Issue(
                stable_client_key("client-traffic", email),
                level,
                "客户端流量突增",
                f"客户端 {display_email} 本周期新增 {human_bytes(delta)}，折算每 {int(interval // 60)} 分钟 {human_bytes(normalized)}",
            )
        )
    return issues


def collect_database_issues(
    connection: sqlite3.Connection,
    config: dict[str, Any],
    state: dict[str, Any],
    now: int,
) -> list[Issue]:
    issues = collect_node_issues(connection, config, now)
    issues.extend(collect_client_ip_issues(connection, config, now))
    issues.extend(collect_traffic_issues(connection, config, state, now))
    return sorted(issues, key=lambda issue: (issue.level != "critical", issue.key))


def check_local_service(runtime: dict[str, Any]) -> Issue | None:
    manager = runtime["service_manager"]
    if manager == "auto":
        if shutil.which("systemctl"):
            manager = "systemd"
        elif shutil.which("launchctl"):
            manager = "launchd"
        elif shutil.which("rc-service"):
            manager = "openrc"
        else:
            manager = "process"
    service_name = runtime["service_name"]
    executable = runtime["service_executable"]
    if manager == "systemd":
        command = [
            shutil.which("systemctl") or "/usr/bin/systemctl",
            "is-active",
            service_name,
        ]
        expected_state = "active"
    elif manager == "launchd":
        label = service_name.removesuffix(".service")
        command = [
            shutil.which("launchctl") or "/bin/launchctl",
            "print",
            f"system/{label}",
        ]
        expected_state = ""
    elif manager == "openrc":
        command = [
            shutil.which("rc-service") or "/sbin/rc-service",
            service_name,
            "status",
        ]
        expected_state = ""
    else:
        command = [
            shutil.which("pgrep") or "/usr/bin/pgrep",
            "-f",
            executable,
        ]
        expected_state = ""
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MonitorError("unable to inspect the local service") from exc
    state = result.stdout.strip() or "unknown"
    if result.returncode == 0 and (not expected_state or state == expected_state):
        return None
    return Issue(
        "local-service:x-ui",
        "critical",
        "主节点服务异常",
        f"{service_name} 状态为 {state}",
    )


def plan_notifications(
    issues: list[Issue],
    previous_active: dict[str, Any],
    now: int,
    repeat_seconds: int,
    notifications_enabled: bool = True,
) -> tuple[list[NotificationAction], dict[str, Any]]:
    previous = previous_active if isinstance(previous_active, dict) else {}
    actions: list[NotificationAction] = []
    active: dict[str, Any] = {}
    for issue in issues:
        old = previous.get(issue.key)
        old = old if isinstance(old, dict) else {}
        first_seen = int(old.get("first_seen", now))
        last_notified = int(old.get("last_notified", 0))
        changed = old.get("level") != issue.level
        should_notify = not old or changed or now - last_notified >= repeat_seconds
        if should_notify and notifications_enabled:
            actions.append(NotificationAction("alert", issue))
            last_notified = now
        active[issue.key] = {
            "level": issue.level,
            "title": issue.title,
            "message": issue.message,
            "first_seen": first_seen,
            "last_seen": now,
            "last_notified": last_notified,
        }
    for key, old in previous.items():
        if key in active or not isinstance(old, dict):
            continue
        title = str(old.get("title", "异常"))
        message = str(old.get("message", "此前异常已消失"))
        recovery = Issue(key, "recovery", f"{title}已恢复", f"此前状态：{message}")
        if notifications_enabled:
            actions.append(NotificationAction("recovery", recovery))
    return actions, active


def render_notification(
    action: NotificationAction,
    config: dict[str, Any],
) -> tuple[str, str, str, str]:
    is_recovery = action.kind == "recovery"
    status = "resolved" if is_recovery else "firing"
    severity = "info" if is_recovery else action.issue.level
    values = {
        "instance_name": config["instance"]["name"],
        "instance_location": config["instance"]["location"],
        "event_title": action.issue.title,
        "event_status": "已恢复" if is_recovery else "检测到异常",
        "event_message": action.issue.message,
        "event_details": (
            "" if is_recovery else action.issue.notification_details
        ),
        "severity": severity,
    }
    notifications = config["notifications"]
    title = notifications["title_template"].format_map(values).strip()
    message = notifications["message_template"].format_map(values).strip()
    return status, severity, title, message


def send_actions(actions: list[NotificationAction], config: dict[str, Any]) -> None:
    notifications = config["notifications"]
    if not actions or not notifications["enabled"]:
        return
    for action in actions:
        status, severity, title, message = render_notification(action, config)
        command = [
            notifications["notifier_path"],
            "--config",
            notifications["config_path"],
            "--status",
            status,
            "--severity",
            severity,
            "--dedupe-key",
            action.issue.key,
            "--title",
            title,
            "--message",
            message,
        ]
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise MonitorError("notification command could not run") from exc
        if result.returncode != 0:
            error = (result.stderr or "notification failed").strip()[:240]
            raise MonitorError(error)


def connect_database(path: str) -> sqlite3.Connection:
    database = Path(path)
    try:
        file_stat = database.stat()
    except OSError as exc:
        raise MonitorError("3x-ui database is unavailable") from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise MonitorError("3x-ui database is invalid")
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection
    except sqlite3.Error as exc:
        raise MonitorError("unable to open the 3x-ui database") from exc


def acquire_lock(path: str):
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor
    except BlockingIOError as exc:
        raise MonitorError("another monitor run is still active") from exc
    except OSError as exc:
        raise MonitorError("monitor lock is unavailable") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check 3x-ui nodes and client usage anomalies")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--dry-run", action="store_true", help="check without notifying or changing state")
    parser.add_argument("--json", action="store_true", help="print check results as JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    lock_descriptor = None
    connection = None
    try:
        config = load_config(args.config)
        lock_descriptor = acquire_lock(config["lock_path"])
        state = load_state(config["state_path"])
        now = int(time.time())
        connection = connect_database(config["database_path"])
        issues = collect_database_issues(connection, config, state, now)
        service_issue = check_local_service(config["runtime"])
        if service_issue:
            issues.insert(0, service_issue)
        actions, active = plan_notifications(
            issues,
            state.get("active_alerts", {}),
            now,
            int(config["checks"]["repeat_alert_seconds"]),
            config["notifications"]["enabled"],
        )
        if args.json or args.dry_run:
            print(
                json.dumps(
                    {
                        "checked_at": now,
                        "dry_run": args.dry_run,
                        "issues": [issue._asdict() for issue in issues],
                        "notification_count": len(actions),
                    },
                    ensure_ascii=False,
                )
            )
        if args.dry_run:
            return 0
        send_actions(actions, config)
        next_state = {
            "version": 1,
            "last_run_at": now,
            "traffic_totals": collect_traffic_totals(connection),
            "active_alerts": active,
        }
        save_state(config["state_path"], next_state)
        if not args.json:
            print(f"monitor_ok issues={len(issues)} notifications={len(actions)}")
        return 0
    except MonitorError as exc:
        print(f"monitor_failed: {exc}", file=sys.stderr)
        return 1
    except sqlite3.Error:
        print("monitor_failed: unable to read 3x-ui monitoring data", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()
        if lock_descriptor is not None:
            os.close(lock_descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
