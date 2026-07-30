#!/usr/bin/env python3
"""Discover an existing 3x-ui installation without changing the host."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sqlite3
import stat
import sys
from pathlib import Path
from typing import NamedTuple, Sequence


DEFAULT_EXECUTABLE_CANDIDATES = (
    Path("/usr/local/x-ui/x-ui"),
    Path("/usr/bin/x-ui"),
    Path("/opt/x-ui/x-ui"),
    Path("/usr/local/bin/x-ui"),
)
DEFAULT_DATABASE_CANDIDATES = (
    Path("/etc/x-ui/x-ui.db"),
    Path("/usr/local/x-ui/x-ui.db"),
    Path("/usr/local/etc/x-ui/x-ui.db"),
    Path("/opt/x-ui/x-ui.db"),
)
DEFAULT_ENVIRONMENT_CANDIDATES = (
    Path("/etc/default/x-ui"),
    Path("/etc/conf.d/x-ui"),
    Path("/etc/sysconfig/x-ui"),
)
MAX_DATABASE_BYTES = 16 * 1024**3


class DiscoveryError(RuntimeError):
    """A discovery error that is safe to display."""


class Discovery(NamedTuple):
    executable: str
    database: str
    install_dir: str
    service_manager: str
    service_name: str
    capabilities: tuple[str, ...]


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(f'PRAGMA table_info("{table}")')
    }


def inspect_database(path: Path) -> tuple[str, ...]:
    try:
        file_stat = path.stat()
    except OSError as exc:
        raise DiscoveryError("3x-ui database was not found") from exc
    if (
        not stat.S_ISREG(file_stat.st_mode)
        or file_stat.st_size > MAX_DATABASE_BYTES
    ):
        raise DiscoveryError("3x-ui database is invalid")
    try:
        connection = sqlite3.connect(
            f"file:{path}?mode=ro",
            uri=True,
            timeout=5,
        )
        try:
            marker_tables = {
                "settings": {"key", "value"},
                "inbounds": {"id"},
            }
            for table, required in marker_tables.items():
                if not required <= _table_columns(connection, table):
                    raise DiscoveryError(
                        "database is not a compatible 3x-ui SQLite database"
                    )
            capabilities = ["base"]
            optional = {
                "traffic": (
                    "client_traffics",
                    {"email", "up", "down"},
                ),
                "client_ips": (
                    "inbound_client_ips",
                    {"client_email", "ips"},
                ),
                "nodes": (
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
                ),
                "node_traffic": (
                    "node_client_traffics",
                    {"email", "up", "down"},
                ),
                "node_ips": (
                    "node_client_ips",
                    {"node_guid", "email", "ips"},
                ),
            }
            for name, (table, required) in optional.items():
                if required <= _table_columns(connection, table):
                    capabilities.append(name)
        finally:
            connection.close()
    except DiscoveryError:
        raise
    except sqlite3.Error as exc:
        raise DiscoveryError(
            "database is not a readable 3x-ui SQLite database"
        ) from exc
    return tuple(capabilities)


def _find_executable(candidates: Sequence[Path]) -> Path:
    for candidate in candidates:
        try:
            mode = candidate.stat().st_mode
        except OSError:
            continue
        if stat.S_ISREG(mode) and os.access(candidate, os.X_OK):
            return candidate.resolve()
    raise DiscoveryError("3x-ui executable was not found")


def _find_database(
    candidates: Sequence[Path],
) -> tuple[Path, tuple[str, ...]]:
    errors: list[DiscoveryError] = []
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            return candidate.resolve(), inspect_database(candidate)
        except DiscoveryError as exc:
            errors.append(exc)
    if errors:
        raise errors[0]
    raise DiscoveryError("3x-ui database was not found")


def reject_postgres_backend(candidates: Sequence[Path]) -> None:
    for candidate in candidates:
        try:
            file_stat = candidate.stat()
            if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > 64 * 1024:
                continue
            lines = candidate.read_text(
                encoding="utf-8",
                errors="replace",
            ).splitlines()
        except OSError:
            continue
        for line in lines:
            key, separator, value = line.partition("=")
            if (
                separator
                and key.strip() == "XUI_DB_TYPE"
                and value.strip().lower() in {"postgres", "postgresql", "pg"}
            ):
                raise DiscoveryError(
                    "3x-ui is configured for PostgreSQL, which is unsupported"
                )


def discover_xui(
    *,
    executable_candidates: Sequence[Path] = DEFAULT_EXECUTABLE_CANDIDATES,
    database_candidates: Sequence[Path] = DEFAULT_DATABASE_CANDIDATES,
    system_name: str | None = None,
    command_paths: dict[str, str] | None = None,
    environment_candidates: Sequence[Path] = DEFAULT_ENVIRONMENT_CANDIDATES,
) -> Discovery:
    reject_postgres_backend(environment_candidates)
    executable = _find_executable(executable_candidates)
    database, capabilities = _find_database(database_candidates)
    current_system = system_name or platform.system()
    commands = command_paths
    if commands is None:
        commands = {
            name: path
            for name in ("systemctl", "launchctl", "rc-service")
            if (path := shutil.which(name))
        }
    if current_system == "Darwin":
        # 3x-ui does not publish an official macOS launchd label. An existing
        # custom installation can still be checked safely by executable path.
        manager = "process"
        service_name = "x-ui"
    elif commands.get("systemctl"):
        manager = "systemd"
        service_name = "x-ui.service"
    elif commands.get("rc-service"):
        manager = "openrc"
        service_name = "x-ui"
    else:
        manager = "process"
        service_name = "x-ui"
    return Discovery(
        executable=str(executable),
        database=str(database),
        install_dir=str(executable.parent),
        service_manager=manager,
        service_name=service_name,
        capabilities=capabilities,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discover and validate a local 3x-ui installation",
    )
    parser.add_argument("--executable")
    parser.add_argument("--database")
    parser.add_argument(
        "--skip-backend-environment-check",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    executable_candidates = (
        [Path(args.executable)]
        if args.executable
        else DEFAULT_EXECUTABLE_CANDIDATES
    )
    database_candidates = (
        [Path(args.database)]
        if args.database
        else DEFAULT_DATABASE_CANDIDATES
    )
    try:
        result = discover_xui(
            executable_candidates=executable_candidates,
            database_candidates=database_candidates,
            environment_candidates=(
                ()
                if args.skip_backend_environment_check
                else DEFAULT_ENVIRONMENT_CANDIDATES
            ),
        )
    except DiscoveryError as exc:
        print(f"discovery_failed: {exc}", file=sys.stderr)
        return 1
    payload = result._asdict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(f"x-ui executable: {result.executable}")
        print(f"x-ui database: {result.database}")
        print(f"service manager: {result.service_manager}")
        print(f"capabilities: {', '.join(result.capabilities)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
