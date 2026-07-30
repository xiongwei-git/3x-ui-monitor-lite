#!/usr/bin/env python3
"""Validate and materialize a complete ThreeXUIScriptMonitor deployment."""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any


MAX_CONFIG_BYTES = 128 * 1024
ROOT_KEYS = {
    "version",
    "scheduler",
    "xui",
    "instance",
    "notifications",
    "client_ip_details",
    "checks",
}
SCHEDULER_KEYS = {"enabled", "interval_seconds"}
XUI_KEYS = {"executable_path", "database_path", "service_name"}
INSTANCE_KEYS = {"name", "location"}
NOTIFICATION_KEYS = {
    "enabled",
    "title_template",
    "message_template",
    "delivery",
}
STATE_PATH = "/var/lib/threexui-script-monitor/state.json"
LOCK_PATH = "/var/lib/threexui-script-monitor/monitor.lock"
NOTIFICATION_PROVIDERS = {
    "alertbridge": {
        "module": "alertbridge_notify",
        "notifier_path": (
            "/usr/local/lib/threexui-script-monitor/alertbridge_notify.py"
        ),
        "config_path": "/etc/threexui-script-monitor/alertbridge.json",
    },
    "feishu_webhook": {
        "module": "feishu_notify",
        "notifier_path": (
            "/usr/local/lib/threexui-script-monitor/feishu_notify.py"
        ),
        "config_path": "/etc/threexui-script-monitor/feishu.json",
    },
}


class DeploymentConfigError(RuntimeError):
    """A safe-to-display deployment configuration error."""


def _load_sibling(name: str) -> Any:
    path = Path(__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(
        f"deployment_config_{name}",
        path,
    )
    if spec is None or spec.loader is None:
        raise DeploymentConfigError("deployment support module is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ANOMALY_MONITOR = _load_sibling("anomaly_monitor")
ALERTBRIDGE_NOTIFY = _load_sibling("alertbridge_notify")
FEISHU_NOTIFY = _load_sibling("feishu_notify")


def _reject_unknown_keys(
    data: dict[str, Any],
    allowed: set[str],
    prefix: str,
) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        name = f"{prefix}.{unknown[0]}" if prefix else unknown[0]
        raise DeploymentConfigError(f"unknown deployment config key: {name}")


def _require_object(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise DeploymentConfigError(f"{key} must be an object")
    return value


def _require_absolute_path(name: str, value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4_096
        or "\x00" in value
        or not Path(value).is_absolute()
    ):
        raise DeploymentConfigError(f"{name} must be an absolute path")
    return value


def _validate_delivery(delivery: Any) -> dict[str, Any] | None:
    if delivery is None:
        return None
    if not isinstance(delivery, dict):
        raise DeploymentConfigError(
            "notifications.delivery must be an object or null"
        )
    provider = delivery.get("provider", "alertbridge")
    try:
        if provider == "alertbridge":
            return ALERTBRIDGE_NOTIFY.validate_config(delivery)
        if provider == "feishu_webhook":
            return FEISHU_NOTIFY.validate_config(delivery)
    except (ALERTBRIDGE_NOTIFY.NotifyError, FEISHU_NOTIFY.NotifyError) as exc:
        raise DeploymentConfigError(str(exc)) from exc
    raise DeploymentConfigError("notification provider is unsupported")


def validate_config(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise DeploymentConfigError("deployment config must be an object")
    _reject_unknown_keys(data, ROOT_KEYS, "")
    if data.get("version") != 1:
        raise DeploymentConfigError("unsupported deployment config version")

    scheduler = _require_object(data, "scheduler")
    _reject_unknown_keys(scheduler, SCHEDULER_KEYS, "scheduler")
    if not isinstance(scheduler.get("enabled"), bool):
        raise DeploymentConfigError(
            "scheduler.enabled must be true or false"
        )
    interval = scheduler.get("interval_seconds")
    if (
        not isinstance(interval, int)
        or isinstance(interval, bool)
        or not 60 <= interval <= 86_400
    ):
        raise DeploymentConfigError(
            "scheduler.interval_seconds must be an integer from 60 to 86400"
        )

    xui = _require_object(data, "xui")
    _reject_unknown_keys(xui, XUI_KEYS, "xui")
    _require_absolute_path("xui.executable_path", xui.get("executable_path"))
    _require_absolute_path("xui.database_path", xui.get("database_path"))

    instance = _require_object(data, "instance")
    _reject_unknown_keys(instance, INSTANCE_KEYS, "instance")

    notifications = _require_object(data, "notifications")
    _reject_unknown_keys(
        notifications,
        NOTIFICATION_KEYS,
        "notifications",
    )
    if not isinstance(notifications.get("enabled"), bool):
        raise DeploymentConfigError(
            "notifications.enabled must be true or false"
        )
    delivery = _validate_delivery(notifications.get("delivery"))
    if notifications["enabled"] and delivery is None:
        raise DeploymentConfigError(
            "notifications.delivery is required when notifications are enabled"
        )

    client_ip_details = _require_object(data, "client_ip_details")
    checks = _require_object(data, "checks")

    normalized = copy.deepcopy(data)
    normalized["notifications"]["delivery"] = delivery
    try:
        monitor_config, _notification = _build_outputs(
            normalized,
            platform="linux",
            database=xui["database_path"],
            executable=xui["executable_path"],
        )
        ANOMALY_MONITOR.validate_config(monitor_config)
    except ANOMALY_MONITOR.MonitorError as exc:
        raise DeploymentConfigError(str(exc)) from exc
    return normalized


def load_config(
    path: str,
    *,
    require_owner: bool = True,
) -> dict[str, Any]:
    config_path = Path(path)
    try:
        file_stat = config_path.stat()
    except OSError as exc:
        raise DeploymentConfigError(
            "deployment config is unavailable"
        ) from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise DeploymentConfigError(
            "deployment config is not a regular file"
        )
    if file_stat.st_mode & 0o077:
        raise DeploymentConfigError(
            "deployment config permissions must be 0600"
        )
    if require_owner and file_stat.st_uid not in {0, os.geteuid()}:
        raise DeploymentConfigError("deployment config owner is invalid")
    if file_stat.st_size > MAX_CONFIG_BYTES:
        raise DeploymentConfigError(
            "deployment config is unexpectedly large"
        )
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeploymentConfigError("deployment config is invalid") from exc
    return validate_config(data)


def _provider_details(
    delivery: dict[str, Any] | None,
) -> tuple[str, dict[str, str]]:
    provider = "alertbridge"
    if delivery is not None:
        provider = delivery["provider"]
    return provider, NOTIFICATION_PROVIDERS[provider]


def _build_outputs(
    config: dict[str, Any],
    *,
    platform: str,
    database: str,
    executable: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if platform not in {"linux", "macos"}:
        raise DeploymentConfigError("platform must be linux or macos")
    _require_absolute_path("database", database)
    _require_absolute_path("executable", executable)

    delivery = config["notifications"]["delivery"]
    provider, provider_details = _provider_details(delivery)
    monitor_config = {
        "version": 1,
        "database_path": database,
        "state_path": STATE_PATH,
        "lock_path": LOCK_PATH,
        "instance": copy.deepcopy(config["instance"]),
        "runtime": {
            "interval_seconds": config["scheduler"]["interval_seconds"],
            "service_manager": (
                "systemd" if platform == "linux" else "process"
            ),
            "service_name": config["xui"]["service_name"],
            "service_executable": executable,
        },
        "notifications": {
            "enabled": config["notifications"]["enabled"],
            "provider": provider,
            "notifier_path": provider_details["notifier_path"],
            "config_path": provider_details["config_path"],
            "title_template": config["notifications"]["title_template"],
            "message_template": config["notifications"]["message_template"],
        },
        "client_ip_details": copy.deepcopy(config["client_ip_details"]),
        "checks": copy.deepcopy(config["checks"]),
    }
    return monitor_config, copy.deepcopy(delivery)


def build_outputs(
    data: Any,
    *,
    platform: str,
    database: str,
    executable: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    config = validate_config(data)
    monitor_config, notification = _build_outputs(
        config,
        platform=platform,
        database=database,
        executable=executable,
    )
    try:
        ANOMALY_MONITOR.validate_config(monitor_config)
    except ANOMALY_MONITOR.MonitorError as exc:
        raise DeploymentConfigError(str(exc)) from exc
    return monitor_config, notification


def inspect_config(data: Any) -> dict[str, Any]:
    config = validate_config(data)
    delivery = config["notifications"]["delivery"]
    provider, provider_details = _provider_details(delivery)
    return {
        "scheduler_enabled": config["scheduler"]["enabled"],
        "interval_seconds": config["scheduler"]["interval_seconds"],
        "xui_executable": config["xui"]["executable_path"],
        "database": config["xui"]["database_path"],
        "service_name": config["xui"]["service_name"],
        "notifications_enabled": config["notifications"]["enabled"],
        "notification_provider": provider,
        "notification_config_filename": Path(
            provider_details["config_path"]
        ).name,
    }


def _write_json_atomic(path_value: str, data: dict[str, Any]) -> None:
    path = Path(path_value)
    if not path.is_absolute():
        raise DeploymentConfigError("output paths must be absolute")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    descriptor = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except OSError as exc:
        raise DeploymentConfigError(
            "unable to write deployment output"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def materialize(
    data: Any,
    *,
    platform: str,
    database: str,
    executable: str,
    monitor_output: str,
    notification_output: str,
) -> None:
    monitor_config, notification = build_outputs(
        data,
        platform=platform,
        database=database,
        executable=executable,
    )
    if notification is not None:
        _write_json_atomic(notification_output, notification)
    _write_json_atomic(monitor_output, monitor_config)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate or install a complete monitor deployment config"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("--config", required=True)
    inspect_parser.add_argument(
        "--allow-non-root-owner",
        action="store_true",
        help=argparse.SUPPRESS,
    )

    materialize_parser = subparsers.add_parser("materialize")
    materialize_parser.add_argument("--config", required=True)
    materialize_parser.add_argument(
        "--platform",
        required=True,
        choices=("linux", "macos"),
    )
    materialize_parser.add_argument("--database", required=True)
    materialize_parser.add_argument("--executable", required=True)
    materialize_parser.add_argument("--monitor-output", required=True)
    materialize_parser.add_argument("--notification-output", required=True)
    materialize_parser.add_argument(
        "--allow-non-root-owner",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = load_config(
            args.config,
            require_owner=not args.allow_non_root_owner,
        )
        if args.command == "inspect":
            print(
                json.dumps(
                    inspect_config(config),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        else:
            materialize(
                config,
                platform=args.platform,
                database=args.database,
                executable=args.executable,
                monitor_output=args.monitor_output,
                notification_output=args.notification_output,
            )
    except DeploymentConfigError as exc:
        print(f"deployment_config_failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
