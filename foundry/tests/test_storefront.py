from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN = [
    "Best Seller",
    "hundreds of developers",
    "Lifetime Updates",
    "enterprise-quality",
    "★★★★★",
    "Testimonials",
    "Save $",
]


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


class StorefrontTests(unittest.TestCase):
    def test_storefront_contains_one_truthful_product(self):
        page = read("index.html")
        self.assertEqual(page.count('data-product-card="true"'), 1)
        self.assertIn(
            "Run Hermes locally for routine work. Escalate the work that matters.",
            page,
        )
        self.assertIn("$12", page)
        self.assertIn("30-day update window", page)
        self.assertIn("not affiliated", page)
        for inclusion in (
            "Generator",
            "redacted preflight",
            "safe installers",
            "routing checklist",
            "rollback checklist",
            "four failure drills",
        ):
            self.assertIn(inclusion, page)
        self.assertIn("fresh canaries", page)
        for phrase in FORBIDDEN:
            self.assertNotIn(phrase, page)

    def test_pending_checkout_is_truthful(self):
        page = read("order.html")
        self.assertIn("Checkout is not open yet", page)
        self.assertNotIn("Request invoice", page)
        self.assertNotIn("GitHub issue", page)

    def test_checkout_state_has_one_pending_product(self):
        script = read("js/main.js")
        self.assertIn("const PRODUCT = Object.freeze({", script)
        self.assertIn("slug: 'hermes-hybrid-operator-kit'", script)
        self.assertIn("price: 12", script)
        self.assertIn("checkoutUrl: ''", script)
        self.assertIn("status: 'pending'", script)
        self.assertNotIn("PRODUCT.status === 'ready'", script)

    def test_proof_page_describes_hashes_and_test_scope(self):
        proof_path = ROOT / "proof.html"
        self.assertTrue(proof_path.is_file())
        proof = proof_path.read_text(encoding="utf-8")
        self.assertIn("Verified hashes", proof)
        self.assertIn("Tested scope", proof)
        self.assertIn("archive hash will appear after the first packaged release", proof)


if __name__ == "__main__":
    unittest.main()
