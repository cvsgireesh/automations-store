import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from foundry.src.packager import PackagingError, build_release


class PackagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.source_dir = self.root / "source"
        self.source_dir.mkdir()
        self.write("README.md", "A tested product.\n")
        self.write("launch.sh", "#!/bin/sh\necho hello\n")

    def tearDown(self):
        self.temporary_directory.cleanup()

    def write(self, relative_path, text):
        target = self.source_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def metadata(self):
        return {
            "test_command": "python3 -m unittest",
            "tested_platforms": ["macOS", "Windows"],
            "source_revision": "abc123",
            "price": "19.00",
            "update_policy_end_date": "2027-01-01",
            "launchers": ["launch.sh"],
        }

    def test_same_input_produces_same_zip_hash(self):
        first = build_release(self.source_dir, self.root / "one.zip", self.metadata())
        second = build_release(self.source_dir, self.root / "two.zip", self.metadata())
        self.assertEqual(first.archive_sha256, second.archive_sha256)

    def test_failed_audit_does_not_create_zip(self):
        self.write(".env", "TOKEN=secretsecretsecret")
        output_zip = self.root / "bad.zip"
        with self.assertRaises(PackagingError):
            build_release(self.source_dir, output_zip, self.metadata())
        self.assertFalse(output_zip.exists())

    def test_manifest_lists_file_hashes_and_required_release_metadata(self):
        output_zip = self.root / "release.zip"
        manifest = build_release(self.source_dir, output_zip, self.metadata())
        self.assertEqual(manifest.archive_sha256, hashlib.sha256(output_zip.read_bytes()).hexdigest())
        with zipfile.ZipFile(output_zip) as archive:
            release_manifest = json.loads(archive.read("RELEASE-MANIFEST.json"))
        self.assertEqual(release_manifest["files"]["README.md"], hashlib.sha256(b"A tested product.\n").hexdigest())
        self.assertEqual(release_manifest["test_command"], "python3 -m unittest")
        self.assertEqual(release_manifest["tested_platforms"], ["macOS", "Windows"])
        self.assertEqual(release_manifest["source_revision"], "abc123")
        self.assertEqual(release_manifest["price"], "19.00")
        self.assertEqual(release_manifest["update_policy_end_date"], "2027-01-01")

    def test_archive_uses_stable_names_timestamps_and_modes(self):
        output_zip = self.root / "release.zip"
        build_release(self.source_dir, output_zip, self.metadata())
        with zipfile.ZipFile(output_zip) as archive:
            names = archive.namelist()
            readme = archive.getinfo("README.md")
            launcher = archive.getinfo("launch.sh")
        self.assertEqual(names, sorted(names))
        self.assertEqual(readme.date_time, (1980, 1, 1, 0, 0, 0))
        self.assertEqual(readme.external_attr >> 16, 0o100644)
        self.assertEqual(launcher.external_attr >> 16, 0o100755)

    def test_rejects_incomplete_release_metadata(self):
        metadata = self.metadata()
        del metadata["price"]
        with self.assertRaises(PackagingError):
            build_release(self.source_dir, self.root / "missing.zip", metadata)


if __name__ == "__main__":
    unittest.main()
