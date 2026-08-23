import json
from pathlib import Path
import unittest

from foundry.src.collector import (
    collect,
    fetch_text,
    parse_github_release,
    parse_gumroad_product,
    parse_gumroad_search,
)


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class CollectorTests(unittest.TestCase):
    def test_parses_gumroad_transactional_metrics(self):
        signal = parse_gumroad_product(
            fixture("gumroad-product.html"),
            "https://example.gumroad.com/l/item",
            "2026-08-23T00:00:00Z",
        )
        self.assertEqual(signal.metrics["sales_count"], 119)
        self.assertEqual(signal.metrics["price"], 14.67)
        self.assertEqual(signal.metrics["rating_count"], 1)

    def test_rejects_non_allowlisted_url(self):
        with self.assertRaises(ValueError):
            fetch_text("https://example.invalid/private", {"gumroad.com"})

    def test_rejects_non_https_url(self):
        with self.assertRaises(ValueError):
            fetch_text("http://gumroad.com/private", {"gumroad.com"})

    def test_parses_bounded_search_count(self):
        signal = parse_gumroad_search(
            fixture("gumroad-search.html"),
            "https://gumroad.com/discover?query=Hermes%20Agent",
            "2026-08-23T00:00:00Z",
        )
        self.assertEqual(signal.metrics["result_count"], 22)

    def test_parses_official_release(self):
        signal = parse_github_release(
            fixture("github-release.json"),
            "https://api.github.com/repos/NousResearch/hermes-agent/releases/latest",
            "2026-08-23T00:00:00Z",
        )
        self.assertEqual(signal.title, "Hermes 0.20.5")
        self.assertEqual(signal.metrics["tag_name"], "v0.20.5")

    def test_collect_uses_bounded_fixture_responses(self):
        config = json.loads((FIXTURES.parent.parent / "config.json").read_text(encoding="utf-8"))
        signals = collect(config, "2026-08-23T00:00:00Z", FIXTURES)
        self.assertEqual([signal.source_type for signal in signals], [
            "market_search", "paid_comparable", "official_release"
        ])


if __name__ == "__main__":
    unittest.main()
