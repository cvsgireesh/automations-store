"""Immutable records exchanged by the evidence-gated product foundry."""

from dataclasses import dataclass
from typing import Union


MetricValue = Union[int, float, str]


@dataclass(frozen=True)
class Signal:
    signal_id: str
    source_url: str
    source_type: str
    observed_at: str
    title: str
    metrics: dict[str, MetricValue]
    content_sha256: str
    independence_key: str


@dataclass(frozen=True)
class Candidate:
    slug: str
    signal_ids: tuple[str, ...]
    buyer: str
    job_to_be_done: str
    product_delta: str


@dataclass(frozen=True)
class GateDecision:
    passed: bool
    reasons: tuple[str, ...]
    matched_signal_ids: tuple[str, ...]
