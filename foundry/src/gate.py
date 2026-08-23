"""Deterministic fail-closed evidence checks for product candidates."""

from __future__ import annotations

from collections.abc import Sequence, Set
from .models import Candidate, GateDecision, Signal


def _has_paid_transactional_evidence(signals: Sequence[Signal]) -> bool:
    for signal in signals:
        if signal.source_type != "paid_comparable":
            continue
        try:
            if int(signal.metrics.get("sales_count", 0)) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def evaluate(candidate: Candidate, signals: Sequence[Signal], ledger: Set[str]) -> GateDecision:
    """Return a pass only when a candidate has complete, independent evidence."""
    candidate_signal_ids = set(candidate.signal_ids)
    matched = [signal for signal in signals if signal.signal_id in candidate_signal_ids]
    independent = {
        signal.independence_key.casefold().strip()
        for signal in matched
        if isinstance(signal.independence_key, str) and signal.independence_key.strip()
    }
    reasons: list[str] = []
    if len(independent) < 3:
        reasons.append("need_at_least_3_independent_signals")
    if not _has_paid_transactional_evidence(matched):
        reasons.append("need_paid_transactional_evidence")
    if candidate.slug in ledger:
        reasons.append("duplicate_slug")
    if not candidate.buyer or not candidate.job_to_be_done or not candidate.product_delta:
        reasons.append("incomplete_buyer_outcome")
    return GateDecision(
        passed=not reasons,
        reasons=tuple(reasons),
        matched_signal_ids=tuple(signal.signal_id for signal in matched),
    )
