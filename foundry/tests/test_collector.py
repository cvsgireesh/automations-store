import json
from pathlib import Path
import unittest

from foundry.src.collector import (
    USER_AGENT,
    collect,
    fetch_text,
    parse_github_release,
    parse_gumroad_product,
    parse_gumroad_search,
    parse_pypistats_recent,
)


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class CollectorTests(unittest.TestCase):
    def test_collector_identifies_itself_with_a_compatible_contact_user_agent(self):
        self.assertTrue(USER_AGENT.startswith("Mozilla/5.0 (compatible; HermesProductFoundry/1.0;"))
        self.assertIn("Dennis-Gireesh/automations-store", USER_AGENT)

    def test_parses_gumroad_transactional_metrics(self):
        signal = parse_gumroad_product(
            fixture("gumroad-product.html"),
            "https://example.gumroad.com/l/item",
            "2026-08-23T00:00:00Z",
        )
        self.assertEqual(signal.metrics["sales_count"], 119)
        self.assertEqual(signal.metrics["price"], 14.67)
        self.assertEqual(signal.metrics["rating_count"], 1)

    def test_signal_identity_is_stable_across_observation_dates(self):
        first = parse_gumroad_product(
            fixture("gumroad-product.html"),
            "https://example.gumroad.com/l/item",
            "2026-08-23T00:00:00Z",
        )
        refreshed = parse_gumroad_product(
            fixture("gumroad-product.html"),
            "https://example.gumroad.com/l/item",
            "2026-08-24T00:00:00Z",
        )
        self.assertEqual(first.signal_id, refreshed.signal_id)
        self.assertNotEqual(first.observed_at, refreshed.observed_at)

    def test_signal_identity_includes_source_type_and_provider(self):
        shared_source = (
            '<html><head><title>Shared source</title>'
            '<meta property="product:price:amount" content="12.00"></head>'
            '<body>"sales_count": 1, "ratings": {"count": 1}'
            '<div data-results-count="1"></div></body></html>'
        )
        product = parse_gumroad_product(
            shared_source, "https://gumroad.com/l/shared", "2026-08-23T00:00:00Z", "gumroad"
        )
        search = parse_gumroad_search(
            shared_source, "https://gumroad.com/l/shared", "2026-08-23T00:00:00Z", "gumroad"
        )
        alternate_provider = parse_gumroad_product(
            shared_source, "https://gumroad.com/l/shared", "2026-08-23T00:00:00Z", "another-provider"
        )
        self.assertNotEqual(product.signal_id, search.signal_id)
        self.assertNotEqual(product.signal_id, alternate_provider.signal_id)

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

    def test_parses_live_gumroad_inertia_total(self):
        signal = parse_gumroad_search(
            '<html><head><title>Discover | Gumroad</title></head>'
            '<body><div data-page="{&quot;props&quot;:{&quot;total&quot;:22}}"></div></body></html>',
            "https://gumroad.com/discover?query=hermes%20agent",
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

    def test_parses_pypistats_adoption_metrics(self):
        signal = parse_pypistats_recent(
            fixture("pypistats-recent.json"),
            "https://pypistats.org/api/packages/hermes-agent/recent",
            "2026-08-23T00:00:00Z",
            independence_key="pypistats",
        )
        self.assertEqual(signal.source_type, "adoption_signal")
        self.assertEqual(signal.metrics["last_month"], 1098)
        self.assertEqual(signal.independence_key, "pypistats")

    def test_collect_uses_bounded_fixture_responses(self):
        config = json.loads((FIXTURES.parent.parent / "config.json").read_text(encoding="utf-8"))
        signals = collect(config, "2026-08-23T00:00:00Z", FIXTURES)
        self.assertEqual([signal.source_type for signal in signals], [
            "paid_comparable", "market_search", "adoption_signal", "official_release"
        ])
        self.assertEqual(
            [signal.independence_key for signal in signals],
            ["gumroad", "gumroad", "pypistats", "github"],
        )

    def test_collect_rejects_provider_key_conflict(self):
        config = json.loads((FIXTURES.parent.parent / "config.json").read_text(encoding="utf-8"))
        config["sources"][0]["independence_key"] = "github"
        with self.assertRaises(ValueError):
            collect(config, "2026-08-23T00:00:00Z", FIXTURES)


if __name__ == "__main__":
    unittest.main()
