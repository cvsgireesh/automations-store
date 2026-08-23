from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from foundry.src.audits import audit_text, audit_tree


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
KIT_ROOT = REPOSITORY_ROOT / "private-products" / "hermes-hybrid-operator-kit"


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

    def test_rejects_testimonials_and_review_markup(self):
        findings = audit_text(
            'A buyer says this is excellent. <article class="testimonial">Excellent!</article>',
            "sales.html",
        )
        self.assertIn("unsupported_testimonial", {finding.code for finding in findings})

    def test_rejects_numeric_customer_counts(self):
        findings = audit_text("Trusted by 300 customers.", "sales.html")
        self.assertIn("unsupported_customer_count", {finding.code for finding in findings})

    def test_rejects_sales_revenue_and_earnings_figures(self):
        findings = audit_text("119 sales and $10,000 in revenue.", "sales.html")
        self.assertIn("unsupported_sales_figure", {finding.code for finding in findings})

    def test_rejects_star_and_rating_claims(self):
        findings = audit_text("Rated 5 stars by customers.", "sales.html")
        self.assertIn("unsupported_rating_claim", {finding.code for finding in findings})

    def test_rejects_discount_claims(self):
        findings = audit_text("Save 20% today.", "sales.html")
        self.assertIn("unsupported_discount_claim", {finding.code for finding in findings})

    def test_rejects_urgency_and_scarcity_copy(self):
        findings = audit_text("Limited-time offer — buy now before midnight!", "sales.html")
        self.assertIn("unsupported_urgency_scarcity", {finding.code for finding in findings})

    def test_rejects_markdown_html_and_css_crossed_out_pricing(self):
        examples = (
            "~~$29~~ $12",
            "<del>$29</del> $12",
            ".original-price { text-decoration: line-through; }",
            '<span style="text-decoration: line-through">$29</span> $12',
        )
        for example in examples:
            with self.subTest(example=example):
                findings = audit_text(example, "sales.html")
                self.assertIn("unsupported_discount_claim", {finding.code for finding in findings})

    def test_rejects_trusted_by_numeric_developer_counts(self):
        findings = audit_text("Trusted by 1,000 developers.", "sales.html")
        self.assertIn("unsupported_social_proof", {finding.code for finding in findings})

    def test_rejects_attributed_quoted_testimonials(self):
        findings = audit_text('“This is excellent.” — Jane Doe, Acme Corp', "sales.html")
        self.assertIn("unsupported_testimonial", {finding.code for finding in findings})

    def test_rejects_common_copy_pattern_bypasses(self):
        cases = {
            "best-selling": "unsupported_bestseller",
            "Join 1,000 developers": "unsupported_social_proof",
            "Today only": "unsupported_urgency_scarcity",
            "“Excellent.” — Jane": "unsupported_testimonial",
            ".price { text-decoration: line-through; }": "unsupported_discount_claim",
        }
        for text, code in cases.items():
            with self.subTest(text=text):
                self.assertIn(code, {finding.code for finding in audit_text(text, "sales.html")})

    def test_does_not_mistake_shell_comparison_for_attributed_testimonial(self):
        findings = audit_text('[ "$APPLY" -eq 0 ]', "scripts/install.sh")
        self.assertNotIn("unsupported_testimonial", {finding.code for finding in findings})

    def test_rejects_numeric_performance_and_cost_benchmarks(self):
        findings = audit_text("Processes jobs 2x faster for $0.01 per task.", "sales.html")
        self.assertIn("unsupported_benchmark", {finding.code for finding in findings})

    def test_rejects_percentage_and_throughput_benchmarks(self):
        findings = audit_text("99% accuracy and 500 requests per minute.", "sales.html")
        self.assertIn("unsupported_benchmark", {finding.code for finding in findings})

    def test_rejects_affiliation_claims_but_allows_non_affiliation_disclaimer(self):
        claims = audit_text("Officially partnered with Example Corp.", "sales.html")
        disclaimer = audit_text("Not affiliated with Example Corp.", "sales.html")
        self.assertIn("unsupported_affiliation_claim", {finding.code for finding in claims})
        self.assertNotIn("unsupported_affiliation_claim", {finding.code for finding in disclaimer})

    def test_reviewer_copy_examples_are_rejected(self):
        findings = audit_text("Rated 5 stars by 300 customers; 119 sales; save 20%", "sales.html")
        self.assertEqual(
            {finding.code for finding in findings},
            {"unsupported_customer_count", "unsupported_rating_claim", "unsupported_sales_figure", "unsupported_discount_claim"},
        )


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

    @unittest.skipIf(sys.platform.startswith("win"), "Windows does not expose POSIX executable mode bits")
    def test_rejects_executable_binary(self):
        target = self.root / "tool"
        target.write_bytes(b"\x7fELF\x00binary")
        target.chmod(0o755)
        self.assertIn("executable_binary", {finding.code for finding in audit_tree(self.root)})

    def test_requires_root_third_party_notices_file(self):
        self.write("third_party/library/NOTICE", "A local notice does not satisfy release licensing.")
        findings = audit_tree(self.root)
        self.assertIn("missing_third_party_notices", {finding.code for finding in findings})

    def test_requires_matching_compatible_root_notice_for_every_direct_component(self):
        self.write("third_party/library/module.py", "print('library')")
        self.write(
            "THIRD_PARTY_NOTICES.md",
            "## third_party/other-library\n- Source URL: https://example.com/other\n- License: MIT\n",
        )
        codes = {finding.code for finding in audit_tree(self.root)}
        self.assertEqual(codes, {"missing_third_party_component_notice", "unmatched_third_party_notice"})

    def test_rejects_incompatible_third_party_license(self):
        self.write("vendor/library/module.py", "print('library')")
        self.write(
            "THIRD_PARTY_NOTICES.md",
            "## vendor/library\n- Source URL: https://example.com/library\n- License: GPL-3.0\n",
        )
        self.assertIn("incompatible_third_party_license", {finding.code for finding in audit_tree(self.root)})

    def test_rejects_duplicate_notice_entries_for_one_component(self):
        self.write("vendor/library/module.py", "print('library')")
        self.write(
            "THIRD_PARTY_NOTICES.md",
            "## vendor/library\n- Source URL: https://example.com/library\n- License: MIT\n\n"
            "## vendor/library\n- Source URL: https://mirror.example.com/library\n- License: MIT\n",
        )
        self.assertIn("duplicate_third_party_notice", {finding.code for finding in audit_tree(self.root)})

    def test_accepts_compatible_root_notices_for_third_party_components(self):
        self.write("third_party/library/module.py", "print('library')")
        self.write("vendor/utility/module.py", "print('utility')")
        self.write(
            "THIRD_PARTY_NOTICES.md",
            "## third_party/library\n- Source URL: https://example.com/library\n- License: MIT\n\n"
            "## vendor/utility\n- Source URL: https://example.com/utility\n- License: Apache-2.0\n",
        )
        self.assertEqual(audit_tree(self.root), [])

    def test_discovers_all_supported_third_party_directory_names_recursively(self):
        self.write("third_party/core/module.py", "print('core')")
        self.write("src/vendor/gpl_lib/module.py", "print('gpl')")
        self.write("plugins/third-party/alpha/module.py", "print('alpha')")
        self.write("extensions/thirdparty/beta/module.py", "print('beta')")
        self.write(
            "THIRD_PARTY_NOTICES.md",
            "## third_party/core\n- Source URL: https://example.com/core\n- License: GPL-3.0\n\n"
            "## src/vendor/gpl_lib\n- Source URL: https://example.com/gpl\n- License: GPL-3.0\n\n"
            "## plugins/third-party/alpha\n- Source URL: https://example.com/alpha\n- License: GPL-3.0\n\n"
            "## extensions/thirdparty/beta\n- Source URL: https://example.com/beta\n- License: GPL-3.0\n",
        )
        findings = audit_tree(self.root)
        self.assertEqual(
            {finding.path for finding in findings if finding.code == "incompatible_third_party_license"},
            {"third_party/core", "src/vendor/gpl_lib", "plugins/third-party/alpha", "extensions/thirdparty/beta"},
        )

    def test_rejects_incompatible_license_in_nested_vendor_directory(self):
        self.write("src/vendor/gpl_lib/module.py", "print('gpl')")
        self.write(
            "THIRD_PARTY_NOTICES.md",
            "## src/vendor/gpl_lib\n- Source URL: https://example.com/gpl\n- License: GPL-3.0\n",
        )
        self.assertIn("incompatible_third_party_license", {finding.code for finding in audit_tree(self.root)})

    def test_requires_notices_for_nested_vendors_directory(self):
        self.write("src/vendors/gpl_lib/module.py", "print('gpl')")
        self.assertIn("missing_third_party_notices", {finding.code for finding in audit_tree(self.root)})

    def test_recognizes_nested_vendors_component_identity_for_gpl_rejection(self):
        self.write("src/vendors/gpl_lib/module.py", "print('gpl')")
        self.write(
            "THIRD_PARTY_NOTICES.md",
            "## src/vendors/gpl_lib\n- Source URL: https://example.com/gpl\n- License: GPL-3.0\n",
        )
        self.assertEqual(
            {finding.code for finding in audit_tree(self.root)},
            {"incompatible_third_party_license"},
        )

    def test_validates_present_root_notice_without_detected_components(self):
        self.write(
            "THIRD_PARTY_NOTICES.md",
            "## src/vendor/gpl_lib\n- Source URL: https://example.com/gpl\n- License: GPL-3.0\n",
        )
        codes = {finding.code for finding in audit_tree(self.root)}
        self.assertEqual(codes, {"unmatched_third_party_notice", "incompatible_third_party_license"})

    def test_rejects_malformed_no_component_notice(self):
        self.write("THIRD_PARTY_NOTICES.md", "No vendor directories are present.\n")
        self.assertIn("invalid_third_party_notices", {finding.code for finding in audit_tree(self.root)})

    def test_accepts_explicit_no_third_party_code_notice_when_vendor_directories_are_absent(self):
        self.write("THIRD_PARTY_NOTICES.md", "No third-party code is included in this release.\n")
        self.assertEqual(audit_tree(self.root), [])

    @unittest.skipUnless(KIT_ROOT.is_dir(), "ignored private kit is not part of this test canary")
    def test_does_not_flag_the_kit_shell_installer_as_a_testimonial(self):
        installer = KIT_ROOT / "scripts" / "install.sh"
        self.assertTrue(installer.is_file())
        findings = audit_tree(KIT_ROOT)
        self.assertNotIn(
            "unsupported_testimonial",
            {finding.code for finding in findings if finding.path == "scripts/install.sh"},
        )


class AuditCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def run_cli(self, *paths, stdin=None):
        return subprocess.run(
            [sys.executable, "-m", "foundry.src.audits", *map(str, paths)],
            cwd=REPOSITORY_ROOT,
            input=stdin,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_cli_reports_zero_for_a_clean_tree(self):
        clean_tree = self.root / "clean"
        clean_tree.mkdir()
        (clean_tree / "README.md").write_text("A straightforward product description.\n", encoding="utf-8")
        result = self.run_cli(clean_tree)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "0 findings\n")

    def test_cli_reports_claim_and_secret_findings_for_multiple_files(self):
        claim = self.root / "claim.txt"
        secret = self.root / "secret.txt"
        claim.write_text("Best seller!\n", encoding="utf-8")
        secret.write_text("TOKEN=secretsecretsecret\n", encoding="utf-8")
        result = self.run_cli(secret, claim)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"{claim.as_posix()}: unsupported_bestseller\n", result.stdout)
        self.assertIn(f"{secret.as_posix()}: secret_like_value\n", result.stdout)
        self.assertTrue(result.stdout.endswith("2 findings\n"))

    def test_cli_reports_missing_paths_as_invalid_inputs(self):
        missing = self.root / "missing"
        result = self.run_cli(missing)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"{missing.as_posix()}: invalid_input\n", result.stdout)
        self.assertTrue(result.stdout.endswith("1 findings\n"))

    def test_cli_audits_piped_stdin(self):
        result = self.run_cli("--stdin", stdin="A buyer says this is excellent.")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("<stdin>: unsupported_testimonial\n", result.stdout)
        self.assertTrue(result.stdout.endswith("1 findings\n"))

    def test_cli_individual_files_reject_sensitive_names(self):
        for name in (".env", "auth.json", "session-data.txt", "state.yml"):
            with self.subTest(name=name):
                path = self.root / name
                path.write_text("SAFE=value\n", encoding="utf-8")
                result = self.run_cli(path)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"{path.as_posix()}: sensitive_file\n", result.stdout)

    def test_cli_individual_file_rejects_symlink(self):
        target = self.root / "target.txt"
        link = self.root / "link.txt"
        target.write_text("plain text\n", encoding="utf-8")
        link.symlink_to(target)
        result = self.run_cli(link)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"{link.as_posix()}: symlink\n", result.stdout)

    @unittest.skipIf(sys.platform.startswith("win"), "Windows does not expose POSIX executable mode bits")
    def test_cli_individual_file_rejects_executable_binary(self):
        binary = self.root / "tool"
        binary.write_bytes(b"\x7fELF\x00binary")
        binary.chmod(0o755)
        result = self.run_cli(binary)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"{binary.as_posix()}: executable_binary\n", result.stdout)

    @unittest.skipIf(sys.platform.startswith("win"), "Windows chmod does not deny the current process read access")
    def test_cli_individual_file_reports_unreadable_input(self):
        unreadable = self.root / "unreadable.txt"
        unreadable.write_text("plain text\n", encoding="utf-8")
        unreadable.chmod(0)
        try:
            result = self.run_cli(unreadable)
        finally:
            unreadable.chmod(0o600)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"{unreadable.as_posix()}: unreadable_file\n", result.stdout)


if __name__ == "__main__":
    unittest.main()
