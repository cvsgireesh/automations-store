"""Deterministic, fail-closed orchestration for the Hermes product foundry.

State and release output deliberately live in ignored ``foundry/state``,
``foundry/runs``, and ``dist`` directories.  This module never publishes a
listing, contacts a buyer, or handles credentials; it only prepares evidence
and a locally verified release bundle.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from typing import Any, Iterator, Mapping, Sequence
from zoneinfo import ZoneInfo

import fcntl

from .collector import collect
from .gate import evaluate
from .models import Candidate, Signal
from .packager import PackagingError, build_release


CHICAGO = ZoneInfo("America/Chicago")
STATE_NAME = "state"
RUNS_NAME = "runs"
SIGNALS_FILE = "latest-signals.json"
CANDIDATE_FILE = "current-candidate.json"
SCOUT_PROPOSAL_FILE = "scout-candidate.json"
PLATFORM_VERIFICATION_FILE = "platform-verification.json"
GATE_FILE = "gate.json"
LEDGER_FILE = "release-ledger.json"
SAFE_SLUG = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")
SAFE_VERSION = re.compile(r"^[0-9]+(?:[.][0-9A-Za-z-]+)*$")
NEW_SKU = "new_sku"
UPDATE = "update"
MAC_PRODUCT_SUITE = "Mac product suite"
WINDOWS_COMPATIBILITY_CANARY = "Windows native compatibility canary (no Windows live Hermes readiness)"
NO_WINDOWS_EVIDENCE = "no Windows native compatibility evidence for this source tree"
NO_WINDOWS_LIVE_READINESS = "no Windows live Hermes readiness"


class FoundryError(RuntimeError):
    """Base error for a safe, user-actionable Foundry failure."""


class CollectionError(FoundryError):
    """A bounded collector failure that left the last good state intact."""


class CandidateError(FoundryError):
    """A malformed or conflicting candidate was refused."""


class ReleaseError(FoundryError):
    """Packaging cannot proceed without fresh verified evidence."""


def _repo_root(repo_root: str | Path | None = None) -> Path:
    root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[2]
    root = root.resolve()
    if not root.is_dir():
        raise FoundryError(f"repository root is not a directory: {root}")
    return root


def _foundry_root(repo_root: Path) -> Path:
    return repo_root / "foundry"


def _state_root(repo_root: Path) -> Path:
    return _foundry_root(repo_root) / STATE_NAME


def _run_root(repo_root: Path, observed_date: str) -> Path:
    return _foundry_root(repo_root) / RUNS_NAME / observed_date


def chicago_date(now: datetime | None = None) -> str:
    """Return the current calendar date in the Foundry's canonical timezone."""
    if now is None:
        now = datetime.now(CHICAGO)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=CHICAGO)
    else:
        now = now.astimezone(CHICAGO)
    return now.date().isoformat()


def _validated_date(value: str | None) -> str:
    value = value or chicago_date()
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise FoundryError("date must be YYYY-MM-DD") from error
    return value


def _observed_at(observed_date: str) -> str:
    """Anchor an observation to midnight in America/Chicago deterministically."""
    return datetime.combine(date.fromisoformat(observed_date), time.min, tzinfo=CHICAGO).isoformat()


@contextmanager
def state_lock(repo_root: str | Path) -> Iterator[None]:
    """Serialize mutable Foundry state using a POSIX advisory file lock."""
    root = _repo_root(repo_root)
    state_root = _state_root(root)
    state_root.mkdir(parents=True, exist_ok=True)
    lock_path = state_root / ".lock"
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _canonical_json(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def atomic_write_json(destination: str | Path, payload: Any) -> None:
    """Persist canonical JSON with a sibling temporary file and ``os.replace``."""
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(_canonical_json(payload))
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, target)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _read_json(path: Path, *, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FoundryError(f"invalid Foundry state file: {path}") from error


def _signal_mapping(signal: Signal) -> dict[str, Any]:
    return {
        "signal_id": signal.signal_id,
        "source_url": signal.source_url,
        "source_type": signal.source_type,
        "observed_at": signal.observed_at,
        "title": signal.title,
        "metrics": signal.metrics,
        "content_sha256": signal.content_sha256,
        "independence_key": signal.independence_key,
    }


def _signal_from_mapping(item: Mapping[str, Any]) -> Signal:
    required_text = (
        "signal_id",
        "source_url",
        "source_type",
        "observed_at",
        "title",
        "content_sha256",
        "independence_key",
    )
    if not all(isinstance(item.get(field), str) and item[field] for field in required_text):
        raise FoundryError("normalized signal is missing a required text field")
    metrics = item.get("metrics")
    if not isinstance(metrics, dict):
        raise FoundryError("normalized signal metrics must be an object")
    return Signal(
        signal_id=item["signal_id"],
        source_url=item["source_url"],
        source_type=item["source_type"],
        observed_at=item["observed_at"],
        title=item["title"],
        metrics=dict(metrics),
        content_sha256=item["content_sha256"],
        independence_key=item["independence_key"],
    )


def _signals_from_payload(payload: Any) -> list[Signal]:
    if not isinstance(payload, Mapping):
        raise FoundryError("normalized signal state must be an object")
    raw_signals = payload.get("signals")
    if not isinstance(raw_signals, list):
        raise FoundryError("normalized signal state must contain a signals list")
    signals: list[Signal] = []
    for item in raw_signals:
        if not isinstance(item, Mapping):
            raise FoundryError("normalized signal entries must be objects")
        signals.append(_signal_from_mapping(item))
    return signals


def _latest_signals_unlocked(repo_root: Path) -> list[Signal]:
    payload = _read_json(_state_root(repo_root) / SIGNALS_FILE)
    return [] if payload is None else _signals_from_payload(payload)


def latest_signals(repo_root: str | Path) -> list[Signal]:
    """Read only the last normalized collector record, never source pages."""
    root = _repo_root(repo_root)
    with state_lock(root):
        return _latest_signals_unlocked(root)


def monitor_payload(signals: Sequence[Signal] | Mapping[str, Any]) -> bytes:
    """Return stable monitor bytes containing only meaningful normalized fields.

    Observation dates, per-fetch content hashes, and date-derived signal IDs are
    deliberately excluded.  Hermes hashes this exact output to suppress a
    local-model run when metrics and meaningful source facts did not change.
    """
    if isinstance(signals, Mapping):
        signals = _signals_from_payload(signals)
    normalized = [
        {
            "independence_key": item.independence_key,
            "metrics": item.metrics,
            "source_type": item.source_type,
            "source_url": item.source_url,
            "title": item.title,
        }
        for item in signals
    ]
    normalized.sort(key=lambda item: (item["source_url"], item["source_type"], item["title"]))
    return _canonical_json({"signals": normalized})


def _write_run_report(repo_root: Path, observed_date: str, name: str, payload: Mapping[str, Any]) -> None:
    """Write an ignored deterministic run report only for meaningful activity."""
    atomic_write_json(_run_root(repo_root, observed_date) / name, dict(payload))


def collect_signals(repo_root: str | Path, observed_date: str | None = None, *,
                    config: Mapping[str, Any] | str | Path | None = None,
                    fixture_dir: str | Path | None = None) -> dict[str, Any]:
    """Boundedly collect signals while preserving the previous good record on error."""
    root = _repo_root(repo_root)
    observation_date = _validated_date(observed_date)
    source_config = config if config is not None else _foundry_root(root) / "config.json"
    try:
        collected = collect(source_config, _observed_at(observation_date), fixture_dir)
    except Exception as error:
        with state_lock(root):
            preserved = (_state_root(root) / SIGNALS_FILE).exists()
        suffix = "last good signals preserved" if preserved else "no good signal record exists"
        raise CollectionError(f"collection failed; {suffix}: {error}") from error

    with state_lock(root):
        previous = _latest_signals_unlocked(root)
        changed = monitor_payload(previous) != monitor_payload(collected)
        if changed:
            payload = {
                "observed_date": observation_date,
                "schema_version": 1,
                "signals": [_signal_mapping(signal) for signal in collected],
            }
            atomic_write_json(_state_root(root) / SIGNALS_FILE, payload)
            _write_run_report(
                root,
                observation_date,
                "collect.json",
                {"signal_count": len(collected), "status": "collected"},
            )
    return {
        "changed": changed,
        "signal_count": len(collected),
        "status": "collected" if changed else "noop",
    }


def _required_candidate_text(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise CandidateError(f"candidate requires non-empty {field}")
    return value.strip()


def validate_candidate(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the small candidate schema shared by Scout, gate, and Builder."""
    if not isinstance(payload, Mapping):
        raise CandidateError("candidate must be a JSON object")
    candidate_type = payload.get("candidate_type", NEW_SKU)
    if candidate_type not in {NEW_SKU, UPDATE}:
        raise CandidateError("candidate_type must be new_sku or update")
    slug = _required_candidate_text(payload, "slug")
    if not SAFE_SLUG.fullmatch(slug):
        raise CandidateError("candidate slug must be a safe lowercase product slug")
    raw_signal_ids = payload.get("signal_ids")
    if not isinstance(raw_signal_ids, list) or not raw_signal_ids:
        raise CandidateError("candidate requires at least one cited signal ID")
    signal_ids = []
    for signal_id in raw_signal_ids:
        if not isinstance(signal_id, str) or not signal_id.strip():
            raise CandidateError("candidate signal IDs must be non-empty strings")
        normalized_id = signal_id.strip()
        if normalized_id not in signal_ids:
            signal_ids.append(normalized_id)
    candidate = {
        "candidate_type": candidate_type,
        "slug": slug,
        "signal_ids": signal_ids,
        "buyer": _required_candidate_text(payload, "buyer"),
        "job_to_be_done": _required_candidate_text(payload, "job_to_be_done"),
        "product_delta": _required_candidate_text(payload, "product_delta"),
    }
    for optional in ("upstream_change", "reproducible_defect", "repeated_buyer_pain"):
        value = payload.get(optional)
        if isinstance(value, str) and value.strip():
            candidate[optional] = value.strip()
    return candidate


def candidate_sha256(candidate: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(dict(candidate))).hexdigest()


def _candidate_path(repo_root: Path) -> Path:
    return _state_root(repo_root) / CANDIDATE_FILE


def stage_candidate(repo_root: str | Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Stage exactly one distinct Scout candidate per America/Chicago day."""
    root = _repo_root(repo_root)
    candidate = validate_candidate(payload)
    digest = candidate_sha256(candidate)
    today = chicago_date()
    with state_lock(root):
        existing = _read_json(_candidate_path(root))
        if isinstance(existing, Mapping):
            previous_digest = existing.get("candidate_sha256")
            previous_date = existing.get("staged_date")
            if previous_digest == digest:
                return {"candidate_sha256": digest, "status": "noop"}
            if previous_date == today:
                raise CandidateError("a distinct candidate is already staged for today")
        atomic_write_json(
            _candidate_path(root),
            {
                "candidate": candidate,
                "candidate_sha256": digest,
                "staged_date": today,
            },
        )
    return {"candidate_sha256": digest, "status": "staged"}


def _lexical_absolute(path: str | Path) -> Path:
    """Normalize ``.``/``..`` without resolving a final symlink component."""
    absolute = Path(os.path.abspath(os.fspath(Path(path).expanduser())))
    # Resolve only the parent so platform aliases such as /var -> /private/var
    # compare consistently. Keeping the final name lexical lets lstat/open
    # reject a proposal symlink instead of resolving through it.
    return absolute.parent.resolve() / absolute.name


def _load_regular_candidate_file(candidate_path: Path) -> tuple[dict[str, Any], tuple[int, int]]:
    """Read one regular proposal without following a symlink or path swap."""
    try:
        before = candidate_path.lstat()
    except OSError as error:
        raise CandidateError(f"cannot read candidate JSON: {candidate_path}") from error
    if not stat.S_ISREG(before.st_mode):
        raise CandidateError("Scout proposal must be a regular file, not a symlink or special file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate_path, flags)
    except OSError as error:
        raise CandidateError("Scout proposal could not be opened without following links") from error
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8") as candidate_file:
            opened = os.fstat(candidate_file.fileno())
            identity = (before.st_dev, before.st_ino)
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != identity:
                raise CandidateError("Scout proposal changed while it was being opened")
            raw = candidate_file.read()
    except OSError as error:
        raise CandidateError(f"cannot read candidate JSON: {candidate_path}") from error
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise CandidateError(f"cannot read candidate JSON: {candidate_path}") from error
    if not isinstance(payload, Mapping):
        raise CandidateError("candidate JSON must be an object")
    return dict(payload), identity


def stage_candidate_file(repo_root: str | Path, candidate_path: str | Path, *, consume: bool = False) -> dict[str, Any]:
    """Stage a JSON candidate and consume only the one approved Scout proposal.

    ``--consume`` cannot remove an arbitrary path.  It is deliberately limited
    to the ignored ``foundry/state/scout-candidate.json`` handoff file, and it
    removes that file only after the stage operation succeeds or is a safe
    same-candidate no-op.
    """
    root = _repo_root(repo_root)
    proposal = _lexical_absolute(candidate_path)
    approved_proposal = _lexical_absolute(_state_root(root) / SCOUT_PROPOSAL_FILE)
    if consume and proposal != approved_proposal:
        raise CandidateError("only the approved ignored Scout proposal path may be consumed")
    if consume:
        candidate, identity = _load_regular_candidate_file(proposal)
    else:
        candidate = _load_candidate_file(proposal)
        identity = None
    result = stage_candidate(root, candidate)
    if consume:
        try:
            current = proposal.lstat()
            if (
                identity is None
                or not stat.S_ISREG(current.st_mode)
                or (current.st_dev, current.st_ino) != identity
            ):
                raise CandidateError("Scout proposal changed before it could be consumed")
            proposal.unlink()
        except FileNotFoundError as error:
            raise CandidateError("Scout proposal disappeared before it could be consumed") from error
    return result


def _ledger_unlocked(repo_root: Path) -> list[dict[str, Any]]:
    payload = _read_json(_state_root(repo_root) / LEDGER_FILE, default={"releases": []})
    if not isinstance(payload, Mapping) or not isinstance(payload.get("releases"), list):
        raise FoundryError("release ledger must contain a releases list")
    releases: list[dict[str, Any]] = []
    for release in payload["releases"]:
        if not isinstance(release, Mapping):
            raise FoundryError("release ledger entries must be objects")
        releases.append(dict(release))
    return releases


def _released_slugs_unlocked(repo_root: Path) -> set[str]:
    return {
        release["slug"]
        for release in _ledger_unlocked(repo_root)
        if release.get("status") == "success" and isinstance(release.get("slug"), str)
    }


def _has_successful_release_unlocked(repo_root: Path, candidate_digest: str, version: str) -> bool:
    return any(
        release.get("status") == "success"
        and release.get("candidate_sha256") == candidate_digest
        and release.get("version") == version
        for release in _ledger_unlocked(repo_root)
    )


def _append_success_unlocked(repo_root: Path, candidate_digest: str, slug: str, version: str,
                             archive_sha256: str | None = None) -> None:
    releases = _ledger_unlocked(repo_root)
    if _has_successful_release_unlocked(repo_root, candidate_digest, version):
        return
    release = {
        "candidate_sha256": candidate_digest,
        "date": chicago_date(),
        "slug": slug,
        "status": "success",
        "version": version,
    }
    if archive_sha256:
        release["archive_sha256"] = archive_sha256
    releases.append(release)
    atomic_write_json(_state_root(repo_root) / LEDGER_FILE, {"releases": releases})


def mark_successful_release(repo_root: str | Path, candidate_digest: str, slug: str,
                            version: str, archive_sha256: str | None = None) -> None:
    """Record a successful release for duplicate-release prevention."""
    root = _repo_root(repo_root)
    if not candidate_digest or not SAFE_SLUG.fullmatch(slug) or not SAFE_VERSION.fullmatch(version):
        raise ReleaseError("release ledger entry has unsafe identifiers")
    with state_lock(root):
        _append_success_unlocked(root, candidate_digest, slug, version, archive_sha256)


def _new_sku_gate(candidate: Mapping[str, Any], signals: Sequence[Signal], ledger: set[str]) -> tuple[bool, list[str], list[str]]:
    decision = evaluate(
        Candidate(
            slug=candidate["slug"],
            signal_ids=tuple(candidate["signal_ids"]),
            buyer=candidate["buyer"],
            job_to_be_done=candidate["job_to_be_done"],
            product_delta=candidate["product_delta"],
        ),
        signals,
        ledger,
    )
    return decision.passed, list(decision.reasons), list(decision.matched_signal_ids)


def _update_gate(candidate: Mapping[str, Any], signals: Sequence[Signal]) -> tuple[bool, list[str], list[str]]:
    cited = {signal_id for signal_id in candidate["signal_ids"]}
    matched = [signal for signal in signals if signal.signal_id in cited]
    matched_ids = [signal.signal_id for signal in matched]
    source_types = [signal.source_type for signal in matched]
    has_upstream_change = bool(candidate.get("upstream_change")) and any(
        source_type in {"official_release", "upstream_change"} for source_type in source_types
    )
    has_reproducible_defect = bool(candidate.get("reproducible_defect")) and any(
        source_type == "reproducible_defect" for source_type in source_types
    )
    buyer_pain_ids = {
        signal.signal_id for signal in matched if signal.source_type == "buyer_pain"
    }
    has_repeated_buyer_pain = bool(candidate.get("repeated_buyer_pain")) and len(buyer_pain_ids) >= 2
    reasons: list[str] = []
    if not matched:
        reasons.append("missing_signal_evidence")
    if not (has_upstream_change or has_reproducible_defect or has_repeated_buyer_pain):
        reasons.append("need_update_trigger")
    return not reasons, reasons, matched_ids


def _remove_stale_gate_unlocked(repo_root: Path) -> None:
    try:
        (_state_root(repo_root) / GATE_FILE).unlink()
    except FileNotFoundError:
        pass


def gate_candidate(repo_root: str | Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate a candidate and persist a gate artifact only when it passes."""
    root = _repo_root(repo_root)
    candidate = validate_candidate(payload)
    digest = candidate_sha256(candidate)
    with state_lock(root):
        signals = _latest_signals_unlocked(root)
        if candidate["candidate_type"] == NEW_SKU:
            passed, reasons, matched_ids = _new_sku_gate(candidate, signals, _released_slugs_unlocked(root))
        else:
            passed, reasons, matched_ids = _update_gate(candidate, signals)
        result = {
            "candidate": candidate,
            "candidate_sha256": digest,
            "candidate_type": candidate["candidate_type"],
            "generated_date": chicago_date(),
            "matched_signal_ids": matched_ids,
            "passed": passed,
            "reasons": reasons,
        }
        if passed:
            atomic_write_json(_state_root(root) / GATE_FILE, result)
        else:
            _remove_stale_gate_unlocked(root)
    return result


def gate_current_candidate(repo_root: str | Path) -> dict[str, Any] | None:
    """Gate the single staged candidate, returning ``None`` for a normal no-op."""
    root = _repo_root(repo_root)
    with state_lock(root):
        staged = _read_json(_candidate_path(root))
        if staged is None:
            return None
        if not isinstance(staged, Mapping) or not isinstance(staged.get("candidate"), Mapping):
            raise CandidateError("staged candidate state is invalid")
        candidate = dict(staged["candidate"])
    return gate_candidate(root, candidate)


def _read_public_metadata(repo_root: Path, slug: str, version: str) -> dict[str, Any]:
    metadata_path = repo_root / "products" / f"{slug}.json"
    metadata = _read_json(metadata_path)
    if not isinstance(metadata, Mapping):
        raise ReleaseError(f"public product metadata is missing: {metadata_path}")
    required = (
        "slug",
        "name",
        "version",
        "price_usd",
        "checkout_status",
        "publication_status",
        "verified_revenue_usd",
        "update_policy",
        "verification_status",
        "non_affiliation",
    )
    if any(field not in metadata for field in required):
        raise ReleaseError("public product metadata is incomplete")
    if metadata["slug"] != slug or metadata["version"] != version:
        raise ReleaseError("product metadata does not match requested slug and version")
    if metadata["verified_revenue_usd"] != 0:
        raise ReleaseError("verified revenue must remain $0 until a non-owner sale is observed")
    if metadata["publication_status"] != "not_published":
        raise ReleaseError("Foundry may only prepare a not-published release")
    if metadata["checkout_status"] != "pending_payout_onboarding":
        raise ReleaseError("checkout must remain pending payout onboarding")
    non_affiliation = metadata["non_affiliation"]
    if not isinstance(non_affiliation, str) or "Nous Research" not in non_affiliation or "OpenAI" not in non_affiliation:
        raise ReleaseError("public metadata must disclose non-affiliation with Nous Research and OpenAI")
    return dict(metadata)


def source_tree_revision(source: str | Path) -> str:
    """Return the release source identity, including ignored paid-source bytes.

    Public Git history intentionally does not contain paid source, so a Git
    commit is never used as ``source_revision``.  The deterministic tree
    digest is the source identity that goes into the archive manifest.
    """
    source_root = Path(source).resolve()
    if not source_root.is_dir() or source_root.is_symlink():
        raise ReleaseError("source tree for revision hashing is unavailable")
    digest = hashlib.sha256()
    for path in sorted(item for item in source_root.rglob("*") if item.is_file() and not item.is_symlink()):
        digest.update(path.relative_to(source_root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"tree-sha256:{digest.hexdigest()}"


def _build_control_revision(repo_root: Path) -> str | None:
    """Return public Git control-plane revision separately from paid source."""
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )
    revision = completed.stdout.strip()
    return revision if completed.returncode == 0 and revision else None


def _tested_platform_scope_unlocked(repo_root: Path, source_revision: str) -> tuple[list[str], str]:
    """Include Windows only when its sanitized evidence matches paid source."""
    platforms = [MAC_PRODUCT_SUITE]
    payload = _read_json(_state_root(repo_root) / PLATFORM_VERIFICATION_FILE, default=None)
    windows_evidence = payload.get("windows_native_compatibility") if isinstance(payload, Mapping) else None
    windows_verified = (
        isinstance(payload, Mapping)
        and payload.get("source_revision") == source_revision
        and isinstance(windows_evidence, Mapping)
        and windows_evidence.get("status") == "passed"
        and isinstance(windows_evidence.get("failures"), int)
        and not isinstance(windows_evidence.get("failures"), bool)
        and windows_evidence.get("failures") == 0
    )
    if windows_verified:
        platforms.append(WINDOWS_COMPATIBILITY_CANARY)
        return platforms, f"{MAC_PRODUCT_SUITE}; Windows native compatibility canary; {NO_WINDOWS_LIVE_READINESS}."
    return platforms, f"{MAC_PRODUCT_SUITE}; {NO_WINDOWS_EVIDENCE}; {NO_WINDOWS_LIVE_READINESS}."


def _run_product_tests(repo_root: Path, slug: str) -> tuple[str, str]:
    command = f"python3 -B private-products/{slug}/tests/test_kit.py -v"
    test_path = repo_root / "private-products" / slug / "tests" / "test_kit.py"
    if not test_path.is_file():
        raise ReleaseError(f"required product test file is missing: {test_path}")
    completed = subprocess.run(
        [sys.executable, "-B", str(test_path), "-v"],
        cwd=repo_root,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    if completed.returncode != 0:
        raise ReleaseError(f"product tests failed for {slug} (exit {completed.returncode})")
    output = (completed.stdout + completed.stderr).encode("utf-8", errors="replace")
    return command, hashlib.sha256(output).hexdigest()


def _release_metadata(repo_root: Path, source: Path, public_metadata: Mapping[str, Any], slug: str) -> dict[str, Any]:
    version_file = source / "VERSION"
    if not version_file.is_file():
        raise ReleaseError("private product VERSION file is required")
    source_version = version_file.read_text(encoding="utf-8").strip()
    if source_version != public_metadata["version"]:
        raise ReleaseError("private VERSION does not match public product metadata")
    update_end = date.fromisoformat(chicago_date()) + timedelta(days=30)
    test_command, output_sha256 = _run_product_tests(repo_root, slug)
    source_revision = source_tree_revision(source)
    tested_platforms, tested_scope = _tested_platform_scope_unlocked(repo_root, source_revision)
    return {
        "build_control_revision": _build_control_revision(repo_root),
        "launchers": ["scripts/install.sh"],
        "price": public_metadata["price_usd"],
        "source_revision": source_revision,
        "test_command": test_command,
        "test_output_sha256": output_sha256,
        "tested_platforms": tested_platforms,
        "tested_scope": tested_scope,
        "update_policy_end_date": update_end.isoformat(),
    }


def _fresh_gate_unlocked(repo_root: Path, slug: str) -> dict[str, Any]:
    gate = _read_json(_state_root(repo_root) / GATE_FILE)
    if not isinstance(gate, Mapping) or gate.get("passed") is not True:
        raise ReleaseError("a fresh passed gate.json is required before packaging")
    if gate.get("generated_date") != chicago_date():
        raise ReleaseError("gate.json is not fresh for the current America/Chicago date")
    candidate = gate.get("candidate")
    digest = gate.get("candidate_sha256")
    if not isinstance(candidate, Mapping) or not isinstance(digest, str) or not digest:
        raise ReleaseError("gate.json lacks a verified candidate identity")
    if candidate.get("slug") != slug:
        raise ReleaseError("gate.json is for a different product")
    if candidate_sha256(candidate) != digest:
        raise ReleaseError("gate.json candidate hash does not match its contents")
    return dict(gate)


def package_release(repo_root: str | Path, slug: str, version: str) -> dict[str, Any]:
    """Test, audit, package, and locally verify a fresh gated release.

    No publication action is present here.  A repeated successful
    candidate/version pair returns a silent-safe ``noop`` before touching the
    ignored release directory.
    """
    root = _repo_root(repo_root)
    if not SAFE_SLUG.fullmatch(slug) or not SAFE_VERSION.fullmatch(version):
        raise ReleaseError("product slug or version is unsafe")
    with state_lock(root):
        gate = _fresh_gate_unlocked(root, slug)
        candidate_digest = gate["candidate_sha256"]
        if _has_successful_release_unlocked(root, candidate_digest, version):
            return {"status": "noop"}

        public_metadata = _read_public_metadata(root, slug, version)
        source = root / "private-products" / slug
        if not source.is_dir() or source.is_symlink():
            raise ReleaseError("private product source is unavailable")
        release_metadata = _release_metadata(root, source, public_metadata, slug)
        archive_path = root / "dist" / "hermespacks" / f"{slug}-{version}.zip"
        try:
            manifest = build_release(source, archive_path, release_metadata, repo_root=root)
        except PackagingError as error:
            raise ReleaseError(f"package verification failed: {error}") from error

        manifest_path = archive_path.with_suffix(".manifest.json")
        listing_path = archive_path.with_suffix(".listing.json")
        release_report = {
            "archive": archive_path.name,
            "archive_sha256": manifest.archive_sha256,
            "build_control_revision": release_metadata["build_control_revision"],
            "candidate_sha256": candidate_digest,
            "checkout_status": public_metadata["checkout_status"],
            "files": manifest.files,
            "generated_date": chicago_date(),
            "price_usd": public_metadata["price_usd"],
            "publication_status": public_metadata["publication_status"],
            "slug": slug,
            "source_revision": manifest.source_revision,
            "test_command": manifest.test_command,
            "test_output_sha256": release_metadata["test_output_sha256"],
            "tested_platforms": list(manifest.tested_platforms),
            "tested_scope": release_metadata["tested_scope"],
            "update_policy_end_date": manifest.update_policy_end_date,
            "verified_revenue_usd": 0,
            "version": version,
            "verification_status": "verified_release_manifest",
        }
        listing_copy = {
            "checkout_status": public_metadata["checkout_status"],
            "name": public_metadata["name"],
            "non_affiliation": public_metadata["non_affiliation"],
            "price_usd": public_metadata["price_usd"],
            "publication_status": public_metadata["publication_status"],
            "slug": slug,
            "tested_scope": release_metadata["tested_scope"],
            "update_policy": public_metadata["update_policy"],
            "verification_status": "verified_release_manifest",
            "verified_revenue_usd": 0,
            "version": version,
        }
        atomic_write_json(manifest_path, release_report)
        atomic_write_json(listing_path, listing_copy)
        _append_success_unlocked(root, candidate_digest, slug, version, manifest.archive_sha256)
        _write_run_report(
            root,
            chicago_date(),
            f"package-{slug}-{version}.json",
            {"archive_sha256": manifest.archive_sha256, "status": "verified"},
        )
    return {
        "archive_path": str(archive_path),
        "archive_sha256": manifest.archive_sha256,
        "listing_path": str(listing_path),
        "manifest_path": str(manifest_path),
        "status": "verified",
    }


def verify_current_release(repo_root: str | Path) -> dict[str, Any]:
    """Package the current fresh gated product, or silently no-op without one."""
    root = _repo_root(repo_root)
    with state_lock(root):
        gate = _read_json(_state_root(root) / GATE_FILE)
        if not isinstance(gate, Mapping) or gate.get("passed") is not True:
            return {"status": "noop"}
        if gate.get("generated_date") != chicago_date():
            return {"status": "noop"}
        candidate = gate.get("candidate")
        if not isinstance(candidate, Mapping) or not isinstance(candidate.get("slug"), str):
            raise ReleaseError("gate.json lacks a packageable candidate")
        slug = candidate["slug"]
    metadata = _read_public_metadata(root, slug, _read_version_for_verify(root, slug))
    return package_release(root, slug, metadata["version"])


def _read_version_for_verify(repo_root: Path, slug: str) -> str:
    metadata = _read_json(repo_root / "products" / f"{slug}.json")
    if not isinstance(metadata, Mapping) or not isinstance(metadata.get("version"), str):
        raise ReleaseError("public product metadata lacks a version")
    return metadata["version"]


def _load_candidate_file(path: str | Path) -> dict[str, Any]:
    candidate_path = Path(path)
    try:
        payload = json.loads(candidate_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CandidateError(f"cannot read candidate JSON: {candidate_path}") from error
    if not isinstance(payload, Mapping):
        raise CandidateError("candidate JSON must be an object")
    return dict(payload)


def _print_json(payload: Mapping[str, Any]) -> None:
    sys.stdout.buffer.write(_canonical_json(dict(payload)))


def main(argv: list[str] | None = None) -> int:
    """Expose deterministic primitives for wrappers and Task 6 dry runs."""
    parser = argparse.ArgumentParser(description="Hermes product Foundry orchestration")
    parser.add_argument("--repo-root", default=None, help="repository root (defaults to this module's repository)")
    subcommands = parser.add_subparsers(dest="command", required=True)

    collect_parser = subcommands.add_parser("collect", help="collect bounded normalized public signals")
    collect_parser.add_argument("--date", default=None, help="America/Chicago date (YYYY-MM-DD)")
    collect_parser.add_argument("--fixture-dir", default=None, help="fixture directory for bounded deterministic tests")

    subcommands.add_parser("monitor", help="emit stable meaningful signal bytes for Hermes monitor mode")

    stage_parser = subcommands.add_parser("stage-candidate", help="stage one Scout candidate")
    stage_parser.add_argument("--candidate", required=True, help="candidate JSON file")
    stage_parser.add_argument(
        "--consume",
        action="store_true",
        help="remove only foundry/state/scout-candidate.json after a successful stage",
    )

    gate_parser = subcommands.add_parser("gate", help="evaluate one candidate deterministically")
    gate_parser.add_argument("--candidate", required=True, help="candidate JSON file")

    subcommands.add_parser("candidate-monitor", help="emit only a passed current gate")

    package_parser = subcommands.add_parser("package", help="test and package a fresh passed candidate")
    package_parser.add_argument("--product", required=True, help="safe product slug")
    package_parser.add_argument("--version", required=True, help="product version")

    subcommands.add_parser("verify", help="package the current gated candidate or silently no-op")

    arguments = parser.parse_args(argv)
    try:
        root = _repo_root(arguments.repo_root)
        if arguments.command == "collect":
            result = collect_signals(root, arguments.date, fixture_dir=arguments.fixture_dir)
            if result["changed"]:
                _print_json(result)
            return 0
        if arguments.command == "monitor":
            signals = latest_signals(root)
            if signals:
                sys.stdout.buffer.write(monitor_payload(signals))
            return 0
        if arguments.command == "stage-candidate":
            result = stage_candidate_file(root, arguments.candidate, consume=arguments.consume)
            if result["status"] != "noop":
                _print_json(result)
            return 0
        if arguments.command == "gate":
            result = gate_candidate(root, _load_candidate_file(arguments.candidate))
            _print_json(result)
            return 0 if result["passed"] else 1
        if arguments.command == "candidate-monitor":
            result = gate_current_candidate(root)
            if result is not None and result["passed"]:
                _print_json(result)
            return 0
        if arguments.command == "package":
            result = package_release(root, arguments.product, arguments.version)
            if result["status"] != "noop":
                _print_json(result)
            return 0
        if arguments.command == "verify":
            result = verify_current_release(root)
            if result["status"] != "noop":
                _print_json(result)
            return 0
        raise FoundryError(f"unsupported Foundry command: {arguments.command}")
    except (FoundryError, subprocess.TimeoutExpired) as error:
        print(f"foundry: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
