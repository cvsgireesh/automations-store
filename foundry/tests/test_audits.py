from pathlib import Path
import tempfile
import unittest

from foundry.src.audits import audit_text, audit_tree


class AuditTextTests(unittest.TestCase):
    def test_flags_fabricated_social_proof(self):
        findings = audit_text("Join hundreds of developers. Best seller!", "index.html")
        self.assertEqual(
            {finding.code for finding in findings},
            {"unsupported_social_proof", "unsupported_bestseller"},
        )

    def test_flags_secret_like_content(self):
        findings = audit_text("OPENAI_API_KEY=sk-live-abcdefghijklmnopqrstuvwxyz", ".env")
        self.assertIn("secret_like_value", {finding.code for finding in findings})

    def test_flags_unresolved_placeholder(self):
        findings = audit_text("Set YOUR_API_KEY before launching.", "README.md")
        self.assertIn("unresolved_placeholder", {finding.code for finding in findings})


class AuditTreeTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def write(self, relative_path, text, mode=None):
        target = self.root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        if mode is not None:
            target.chmod(mode)
        return target

    def test_rejects_symlinks_and_sensitive_files(self):
        self.write(".env", "NAME=value")
        self.write("state.json", "{}")
        (self.root / "linked.txt").symlink_to(self.root / ".env")
        codes = {finding.code for finding in audit_tree(self.root)}
        self.assertEqual(codes, {"sensitive_file", "symlink"})

    def test_rejects_executable_binary(self):
        target = self.root / "tool"
        target.write_bytes(b"\x7fELF\x00binary")
        target.chmod(0o755)
        self.assertIn("executable_binary", {finding.code for finding in audit_tree(self.root)})

    def test_requires_notice_in_third_party_directory(self):
        self.write("third_party/library/LICENSE", "MIT")
        findings = audit_tree(self.root)
        self.assertIn("missing_third_party_notice", {finding.code for finding in findings})

    def test_accepts_notice_in_third_party_directory(self):
        self.write("third_party/library/NOTICE", "Library notices")
        self.assertEqual(audit_tree(self.root), [])


if __name__ == "__main__":
    unittest.main()
