import hashlib
import io
import subprocess
import tarfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class PackageTest(unittest.TestCase):
    def test_single_file_installer_is_versioned_self_verifying_and_runnable(self):
        result = subprocess.run(
            ["sh", "scripts/build-installer.sh"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        output = Path(result.stdout.strip())
        self.assertTrue(output.is_file())
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        self.assertIn(version, output.name)
        checksum_path = Path(str(output) + ".sha256")
        expected = checksum_path.read_text(encoding="utf-8").split()[0]
        actual = hashlib.sha256(output.read_bytes()).hexdigest()
        self.assertEqual(actual, expected)
        marker = b"__THREEXUI_PAYLOAD_BELOW__\n"
        archive = output.read_bytes().split(marker, 1)[1]
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
            extended_attributes = {
                key
                for member in bundle.getmembers()
                for key in member.pax_headers
                if "xattr" in key.lower() or "mac" in key.lower()
            }
        self.assertEqual(extended_attributes, set())

        help_result = subprocess.run(
            ["sh", str(output), "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("--config", help_result.stdout)
        self.assertIn("--interval", help_result.stdout)
        self.assertNotIn(
            "extended header keyword",
            help_result.stderr,
        )
        self.assertIn("payload verified", help_result.stderr)


if __name__ == "__main__":
    unittest.main()
