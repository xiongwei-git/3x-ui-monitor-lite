import importlib.util
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "monitor" / "xui_discovery.py"
SPEC = importlib.util.spec_from_file_location("xui_discovery", MODULE_PATH)
discovery = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(discovery)


def make_xui_database(path: Path, *, compatible: bool = True) -> None:
    connection = sqlite3.connect(path)
    if compatible:
        connection.executescript(
            """
            CREATE TABLE settings (id INTEGER PRIMARY KEY, key TEXT, value TEXT);
            CREATE TABLE inbounds (id INTEGER PRIMARY KEY);
            CREATE TABLE client_traffics (
                email TEXT,
                up INTEGER,
                down INTEGER
            );
            """
        )
    else:
        connection.execute("CREATE TABLE unrelated (id INTEGER)")
    connection.close()


class XUIDiscoveryTest(unittest.TestCase):
    def test_discovers_executable_database_and_capabilities(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "x-ui"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            os.chmod(executable, 0o755)
            database = root / "x-ui.db"
            make_xui_database(database)

            result = discovery.discover_xui(
                executable_candidates=[executable],
                database_candidates=[database],
                system_name="Linux",
                command_paths={"systemctl": "/usr/bin/systemctl"},
            )

        self.assertEqual(result.executable, str(executable.resolve()))
        self.assertEqual(result.database, str(database.resolve()))
        self.assertEqual(result.service_manager, "systemd")
        self.assertIn("traffic", result.capabilities)
        self.assertNotIn("nodes", result.capabilities)

    def test_missing_executable_exits_before_accepting_database(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "x-ui.db"
            make_xui_database(database)

            with self.assertRaisesRegex(discovery.DiscoveryError, "executable"):
                discovery.discover_xui(
                    executable_candidates=[Path(directory) / "missing-x-ui"],
                    database_candidates=[database],
                    system_name="Linux",
                    command_paths={},
                )

    def test_incompatible_database_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "x-ui"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            os.chmod(executable, 0o755)
            database = root / "x-ui.db"
            make_xui_database(database, compatible=False)

            with self.assertRaisesRegex(discovery.DiscoveryError, "compatible"):
                discovery.discover_xui(
                    executable_candidates=[executable],
                    database_candidates=[database],
                    system_name="Linux",
                    command_paths={},
                )

    def test_postgres_environment_is_rejected_even_with_stale_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "x-ui"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            os.chmod(executable, 0o755)
            database = root / "x-ui.db"
            make_xui_database(database)
            environment = root / "x-ui.env"
            environment.write_text(
                "XUI_DB_TYPE=postgres\nXUI_DB_DSN=secret-value\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(discovery.DiscoveryError, "PostgreSQL"):
                discovery.discover_xui(
                    executable_candidates=[executable],
                    database_candidates=[database],
                    system_name="Linux",
                    command_paths={"systemctl": "/usr/bin/systemctl"},
                    environment_candidates=[environment],
                )

    def test_existing_macos_install_uses_process_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "x-ui"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            os.chmod(executable, 0o755)
            database = root / "x-ui.db"
            make_xui_database(database)

            result = discovery.discover_xui(
                executable_candidates=[executable],
                database_candidates=[database],
                system_name="Darwin",
                command_paths={"launchctl": "/bin/launchctl"},
            )

        self.assertEqual(result.service_manager, "process")
