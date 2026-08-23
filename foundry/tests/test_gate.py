import unittest

from foundry.src.gate import evaluate
from foundry.src.models import Candidate, Signal


OBSERVED_AT = "2026-08-23T00:00:00Z"


def candidate(signal_ids: tuple[str, ...] = ("paid", "official", "buyer")) -> Candidate:
    return Candidate(
        slug="operator-kit",
        signal_ids=signal_ids,
        buyer="Hermes operator",
        job_to_be_done="route low-risk work to a local model safely",
        product_delta="tested diagnostics and routing policy",
    )


def signal(signal_id: str, source_url: str, source_type: str, metrics: dict,
           independence_key: str) -> Signal:
    return Signal(
        signal_id,
        source_url,
        source_type,
        OBSERVED_AT,
        signal_id,
        metrics,
        "hash",
        independence_key=independence_key,
    )


def paid_signal() -> Signal:
    return signal("paid", "https://gumroad.com/l/hermes", "paid_comparable", {"sales_count": 119}, "gumroad")


def official_signal() -> Signal:
    return signal("official", "https://api.github.com/repos/example/project/releases/latest", "official_release", {}, "github")


def buyer_signal() -> Signal:
    return signal(
        "buyer",
        "https://pypistats.org/api/packages/hermes-agent/recent",
        "adoption_signal",
        {},
        "pypistats",
    )


class GateTests(unittest.TestCase):
    def test_signal_supports_original_seven_argument_constructor(self):
        legacy_signal = Signal(
            "legacy",
            "https://gumroad.com/l/hermes",
            "paid_comparable",
            OBSERVED_AT,
            "Legacy signal",
            {"sales_count": 1},
            "hash",
        )
        self.assertEqual(legacy_signal.independence_key, "")

    def test_new_sku_requires_three_independent_signals_and_paid_comparable(self):
        decision = evaluate(candidate(), [paid_signal(), official_signal(), buyer_signal()], set())
        self.assertTrue(decision.passed)
        self.assertEqual(decision.reasons, ())

    def test_two_signals_fail_closed(self):
        decision = evaluate(candidate(), [paid_signal(), official_signal()], set())
        self.assertEqual(decision.reasons, ("need_at_least_3_independent_signals",))

    def test_duplicate_product_slug_fails(self):
        decision = evaluate(candidate(), [paid_signal(), official_signal(), buyer_signal()], {"operator-kit"})
        self.assertIn("duplicate_slug", decision.reasons)
        self.assertFalse(decision.passed)

    def test_unrelated_metrics_do_not_count_as_evidence(self):
        unrelated = signal(
            "unrelated",
            "https://example.org/competitor",
            "paid_comparable",
            {"sales_count": 999},
            "competitor",
        )
        decision = evaluate(candidate(("official", "buyer")), [official_signal(), buyer_signal(), unrelated], set())
        self.assertEqual(
            decision.reasons,
            ("need_at_least_3_independent_signals", "need_paid_transactional_evidence"),
        )

    def test_github_hostnames_with_one_independence_key_count_once(self):
        github_issue = signal(
            "github-issue",
            "https://github.com/example/project/issues/99",
            "buyer_pain",
            {},
            "github",
        )
        decision = evaluate(
            candidate(("paid", "official", "github-issue")),
            [paid_signal(), official_signal(), github_issue],
            set(),
        )
        self.assertEqual(decision.reasons, ("need_at_least_3_independent_signals",))

    def test_conflicting_github_key_does_not_create_independent_evidence(self):
        mislabeled_github_issue = signal(
            "github-issue",
            "https://github.com/example/project/issues/99",
            "buyer_pain",
            {},
            "buyer_forum",
        )
        decision = evaluate(
            candidate(("paid", "official", "github-issue")),
            [paid_signal(), official_signal(), mislabeled_github_issue],
            set(),
        )
        self.assertEqual(decision.reasons, ("need_at_least_3_independent_signals",))

    def test_unknown_provider_does_not_create_independent_evidence(self):
        unknown_provider = signal(
            "unknown",
            "https://example.org/research",
            "buyer_pain",
            {},
            "buyer_forum",
        )
        decision = evaluate(
            candidate(("paid", "official", "unknown")),
            [paid_signal(), official_signal(), unknown_provider],
            set(),
        )
        self.assertEqual(decision.reasons, ("need_at_least_3_independent_signals",))


if __name__ == "__main__":
    unittest.main()
