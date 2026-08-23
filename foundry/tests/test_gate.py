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


def signal(signal_id: str, source_url: str, source_type: str, metrics: dict) -> Signal:
    return Signal(signal_id, source_url, source_type, OBSERVED_AT, signal_id, metrics, "hash")


def paid_signal() -> Signal:
    return signal("paid", "https://gumroad.com/l/hermes", "paid_comparable", {"sales_count": 119})


def official_signal() -> Signal:
    return signal("official", "https://api.github.com/repos/example/project/releases/latest", "official_release", {})


def buyer_signal() -> Signal:
    return signal("buyer", "https://github.com/example/project/issues/99", "buyer_pain", {})


class GateTests(unittest.TestCase):
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
        )
        decision = evaluate(candidate(("official", "buyer")), [official_signal(), buyer_signal(), unrelated], set())
        self.assertEqual(
            decision.reasons,
            ("need_at_least_3_independent_signals", "need_paid_transactional_evidence"),
        )


if __name__ == "__main__":
    unittest.main()
