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
            "## other-library\n- Source URL: https://example.com/other\n- License: MIT\n",
        )
        codes = {finding.code for finding in audit_tree(self.root)}
        self.assertEqual(codes, {"missing_third_party_component_notice", "unmatched_third_party_notice"})

    def test_rejects_incompatible_third_party_license(self):
        self.write("vendor/library/module.py", "print('library')")
        self.write(
            "THIRD_PARTY_NOTICES.md",
            "## library\n- Source URL: https://example.com/library\n- License: GPL-3.0\n",
        )
        self.assertIn("incompatible_third_party_license", {finding.code for finding in audit_tree(self.root)})

    def test_rejects_duplicate_notice_entries_for_one_component(self):
        self.write("vendor/library/module.py", "print('library')")
        self.write(
            "THIRD_PARTY_NOTICES.md",
            "## library\n- Source URL: https://example.com/library\n- License: MIT\n\n"
            "## library\n- Source URL: https://mirror.example.com/library\n- License: MIT\n",
        )
        self.assertIn("duplicate_third_party_notice", {finding.code for finding in audit_tree(self.root)})

    def test_accepts_compatible_root_notices_for_third_party_components(self):
        self.write("third_party/library/module.py", "print('library')")
        self.write("vendor/utility/module.py", "print('utility')")
        self.write(
            "THIRD_PARTY_NOTICES.md",
            "## library\n- Source URL: https://example.com/library\n- License: MIT\n\n"
            "## utility\n- Source URL: https://example.com/utility\n- License: Apache-2.0\n",
        )
        self.assertEqual(audit_tree(self.root), [])

    def test_accepts_explicit_no_third_party_code_notice_when_vendor_directories_are_absent(self):
        self.write("THIRD_PARTY_NOTICES.md", "No third-party code is included in this release.\n")
        self.assertEqual(audit_tree(self.root), [])


if __name__ == "__main__":
    unittest.main()
