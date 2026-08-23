import json
from html.parser import HTMLParser
from pathlib import Path
import subprocess
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


def product_metadata() -> dict:
    return json.loads(
        (ROOT / "products" / "hermes-hybrid-operator-kit.json").read_text(encoding="utf-8")
    )


NODE_RUNNER = r"""
const fs = require('fs');
const vm = require('vm');
const [scriptPath, checkoutUrl, status, candidatesText] = process.argv.slice(1);
const source = fs.readFileSync(scriptPath, 'utf8')
  .replace("checkoutUrl: ''", `checkoutUrl: ${JSON.stringify(checkoutUrl)}`)
  .replace("status: 'pending'", `status: ${JSON.stringify(status)}`);
const ctas = [{ href: '', textContent: '' }];
const messages = [{ textContent: '' }];
const document = {
  querySelectorAll(selector) {
    if (selector === '[data-checkout-cta]') return ctas;
    if (selector === '[data-checkout-message]') return messages;
    return [];
  },
  querySelector() { return null; },
};
const sandbox = { URL, document, candidates: JSON.parse(candidatesText), ctas, messages };
vm.createContext(sandbox);
vm.runInContext(`${source}
globalThis.__result = {
  product: PRODUCT,
  accepted: Object.fromEntries(candidates.map(value => [value, isGumroadProductUrl(value)])),
  checkoutIsOpen,
  cta: ctas[0],
  message: messages[0],
};`, sandbox);
console.log(JSON.stringify(sandbox.__result));
"""


def run_checkout_script(checkout_url: str, status: str, candidates: list[str]) -> dict:
    result = subprocess.run(
        [
            "node",
            "-e",
            NODE_RUNNER,
            str(ROOT / "js" / "main.js"),
            checkout_url,
            status,
            json.dumps(candidates),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


class AssetCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.assets: list[str] = []

    def handle_starttag(self, tag: str, attrs):
        values = dict(attrs)
        if tag == "script" and values.get("src"):
            self.assets.append(values["src"])
        if tag == "link" and values.get("href"):
            rel = values.get("rel", "").split()
            if "stylesheet" in rel or "icon" in rel:
                self.assets.append(values["href"])


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
        self.assertIn("Nous Research", page)
        self.assertIn("OpenAI", page)
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
        self.assertIn("PRODUCT.status === 'ready'", script)

    def test_checkout_url_requires_exact_gumroad_product_path(self):
        valid = [
            "https://gumroad.com/l/hermes-hybrid-operator-kit",
            "https://creator.gumroad.com/l/operator-kit_2026",
        ]
        invalid = [
            "http://gumroad.com/l/operator-kit",
            "https://gumroad.com/",
            "https://gumroad.com/discover",
            "https://creator.gumroad.com/",
            "https://gumroad.com/l/",
            "https://gumroad.com/l/bad%20slug",
            "https://gumroad.com/l/operator/extra",
            "https://gumroad.com/p/operator-kit",
            "https://gumroad.com.evil/l/operator-kit",
            "https://seller:secret@gumroad.com/l/operator-kit",
            "https://gumroad.com/l/operator-kit?ref=example",
            "https://gumroad.com/l/operator-kit#details",
            "https://gumroad.com:444/l/operator-kit",
            " https://gumroad.com/l/operator-kit ",
        ]
        result = run_checkout_script("", "pending", valid + invalid)
        for url in valid:
            self.assertTrue(result["accepted"][url], url)
        for url in invalid:
            self.assertFalse(result["accepted"][url], url)

    def test_pending_status_keeps_a_valid_checkout_url_closed(self):
        url = "https://gumroad.com/l/hermes-hybrid-operator-kit"
        pending = run_checkout_script(url, "pending", [url])
        ready = run_checkout_script(url, "ready", [url])
        self.assertFalse(pending["checkoutIsOpen"])
        self.assertEqual(pending["cta"]["href"], "order.html")
        self.assertEqual(pending["cta"]["textContent"], "Checkout setup status")
        self.assertTrue(ready["checkoutIsOpen"])
        self.assertEqual(ready["cta"]["href"], url)
        self.assertEqual(ready["cta"]["textContent"], "Buy for $12")

    def test_metadata_agrees_with_public_storefront_state(self):
        metadata = product_metadata()
        index = read("index.html")
        order = read("order.html")
        proof = read("proof.html")
        script = read("js/main.js")

        self.assertEqual(metadata["slug"], "hermes-hybrid-operator-kit")
        self.assertIn(f'data-product-slug="{metadata["slug"]}"', index)
        self.assertIn(f"slug: '{metadata['slug']}'", script)
        self.assertEqual(metadata["price_usd"], 12)
        self.assertIn(f"price: {metadata['price_usd']}", script)
        self.assertIn(f"${metadata['price_usd']}", index)
        self.assertIn(f"${metadata['price_usd']}", order)
        self.assertEqual(metadata["checkout_status"], "pending_payout_onboarding")
        self.assertIn("Checkout status: pending.", index)
        self.assertEqual(metadata["publication_status"], "not_published")
        self.assertIn("Checkout is not open yet.", order)
        self.assertEqual(metadata["update_policy"], "Current release plus 30 days of corrections.")
        self.assertIn(metadata["update_policy"], index)
        self.assertEqual(metadata["verification_status"], "pending_release_verification")
        self.assertIn("No release manifest is published yet.", proof)
        self.assertIn(metadata["non_affiliation"], index)
        self.assertIn(metadata["non_affiliation"], order)
        self.assertIn(metadata["non_affiliation"], proof)

    def test_referenced_storefront_assets_are_local_and_present(self):
        for page_name in ("index.html", "order.html", "proof.html"):
            parser = AssetCollector()
            parser.feed(read(page_name))
            self.assertEqual(
                set(parser.assets),
                {"css/style.css", "js/main.js", "favicon.svg"},
                page_name,
            )
            for asset in parser.assets:
                self.assertTrue((ROOT / asset).is_file(), f"{page_name}: {asset}")

    def test_readme_links_the_free_architecture_repository(self):
        self.assertIn(
            "https://github.com/Dennis-Gireesh/hermes-hybrid-agent-router",
            read("README.md"),
        )

    def test_proof_page_describes_hashes_and_test_scope(self):
        proof_path = ROOT / "proof.html"
        self.assertTrue(proof_path.is_file())
        proof = proof_path.read_text(encoding="utf-8")
        self.assertIn("Verified hashes", proof)
        self.assertIn("Tested scope", proof)
        self.assertIn("archive hash will appear after the first packaged release", proof)


if __name__ == "__main__":
    unittest.main()
