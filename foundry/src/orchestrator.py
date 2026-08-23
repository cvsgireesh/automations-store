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
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import sys
import tempfile
import time as clock
from typing import Any, Iterator, Mapping, Sequence
import uuid
from zoneinfo import ZoneInfo

try:  # POSIX hosts use flock; Windows uses msvcrt byte locks below.
    import fcntl
except ImportError:  # pragma: no cover - exercised by native Windows canary
    fcntl = None  # type: ignore[assignment]

try:  # pragma: no cover - unavailable on POSIX hosts
    import msvcrt
except ImportError:  # pragma: no cover - exercised on POSIX hosts
    msvcrt = None  # type: ignore[assignment]

from .collector import collect
from .audits import audit_text, audit_tree
from .gate import evaluate
from .models import Candidate, Signal
from .packager import PackagingError, build_release


CHICAGO = ZoneInfo("America/Chicago")
STATE_NAME = "state"
RUNS_NAME = "runs"
SIGNALS_FILE = "latest-signals.json"
COLLECTION_STATUS_FILE = "collection-status.json"
CANDIDATE_FILE = "current-candidate.json"
SCOUT_PROPOSAL_FILE = "scout-candidate.json"
SCOUT_RECEIPTS_DIRECTORY = "scout-receipts"
SCOUT_RECEIPT_LIMIT = 32
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
# A successful collection refreshes its timestamps daily.  A failed collection
# leaves those timestamps untouched, and candidates may use evidence at most
# this many Chicago calendar days old.
MAX_SIGNAL_AGE_DAYS = 14
LOCK_TIMEOUT_SECONDS = 30.0
LOCK_RETRY_SECONDS = 0.05
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


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


def _lock_contention(error: OSError) -> bool:
    """Recognize only ordinary advisory-lock contention across supported hosts."""
    return error.errno in (errno.EACCES, errno.EAGAIN) or getattr(error, "winerror", None) in (32, 33)


def _acquire_state_lock(descriptor: int) -> None:
    """Take a bounded one-byte state lock through the host's native backend."""
    if fcntl is not None:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            return
        except OSError as error:
            raise FoundryError("cannot acquire Foundry state lock") from error
    if msvcrt is None:  # pragma: no cover - every supported host has one backend
        raise FoundryError("no supported Foundry state-lock backend")
    deadline = clock.monotonic() + LOCK_TIMEOUT_SECONDS
    while True:  # pragma: no cover - exercised by native Windows canary
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            return
        except OSError as error:
            if not _lock_contention(error):
                raise FoundryError("cannot acquire Foundry state lock") from error
            if clock.monotonic() >= deadline:
                raise FoundryError("timed out waiting for Foundry state lock") from error
            clock.sleep(LOCK_RETRY_SECONDS)


def _release_state_lock(descriptor: int) -> None:
    if fcntl is not None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return
    if msvcrt is not None:  # pragma: no cover - exercised by native Windows canary
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)


@contextmanager
def state_lock(repo_root: str | Path) -> Iterator[None]:
    """Serialize mutable Foundry state with a native advisory one-byte lock."""
    root = _repo_root(repo_root)
    state_root = _state_root(root)
    state_root.mkdir(parents=True, exist_ok=True)
    lock_path = state_root / ".lock"
    with lock_path.open("a+b") as lock_file:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
            os.fsync(lock_file.fileno())
        _acquire_state_lock(lock_file.fileno())
        try:
            yield
        finally:
            _release_state_lock(lock_file.fileno())


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


def atomic_write_bytes(destination: str | Path, content: bytes) -> None:
    """Persist bytes with a sibling temporary file and one atomic replace."""
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
            temporary.write(content)
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


def _signal_state_sha256_unlocked(repo_root: Path) -> str | None:
    """Hash exact persisted last-good bytes without parsing source evidence."""
    try:
        content = (_state_root(repo_root) / SIGNALS_FILE).read_bytes()
    except OSError:
        return None
    return hashlib.sha256(content).hexdigest()


def _signal_state_observed_date_unlocked(repo_root: Path) -> str | None:
    """Read the canonical collection date from the hash-bound signal record."""
    payload = _read_json(_state_root(repo_root) / SIGNALS_FILE)
    if not isinstance(payload, Mapping):
        return None
    observed_date = payload.get("observed_date")
    if not isinstance(observed_date, str):
        return None
    try:
        return observed_date if date.fromisoformat(observed_date).isoformat() == observed_date else None
    except ValueError:
        return None


def _ready_signal_state_sha256_unlocked(repo_root: Path) -> str | None:
    """Return the exact current ready signal digest, or ``None`` fail-closed.

    The collection marker, its canonical date, and the raw persisted signals
    bytes must agree.  A passed gate records this value, making a same-day
    meaningful refresh invalidate the earlier decision even when citations
    retain stable IDs.
    """
    try:
        status = _read_json(_state_root(repo_root) / COLLECTION_STATUS_FILE)
    except FoundryError:
        return None
    if not isinstance(status, Mapping) or status.get("status") != "ready":
        return None
    expected_digest = status.get("signals_sha256")
    observed_date = status.get("observed_date")
    try:
        marker_date = observed_date if isinstance(observed_date, str) and date.fromisoformat(observed_date).isoformat() == observed_date else None
    except ValueError:
        marker_date = None
    actual_digest = _signal_state_sha256_unlocked(repo_root)
    if not (
        isinstance(expected_digest, str)
        and SHA256_HEX.fullmatch(expected_digest)
        and marker_date is not None
        and marker_date == _signal_state_observed_date_unlocked(repo_root)
        and actual_digest is not None
        and expected_digest == actual_digest
    ):
        return None
    return actual_digest


def _collection_is_stale_unlocked(repo_root: Path) -> bool:
    """Return whether the last bounded collection has no valid ready marker."""
    return _ready_signal_state_sha256_unlocked(repo_root) is None


def latest_signals(repo_root: str | Path) -> list[Signal]:
    """Read only the last normalized collector record, never source pages."""
    root = _repo_root(repo_root)
    with state_lock(root):
        return _latest_signals_unlocked(root)


def monitor_payload(signals: Sequence[Signal] | Mapping[str, Any]) -> bytes:
    """Return stable monitor bytes containing only meaningful normalized fields.

    Observation dates, per-fetch content hashes, and stable citation IDs are
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
            # Do not persist an exception string: it can contain remote or
            # local details.  The date/status are sufficient to fail the gate
            # closed while preserving the last good normalized evidence.
            atomic_write_json(
                _state_root(root) / COLLECTION_STATUS_FILE,
                {
                    "failed_date": observation_date,
                    "last_good_signals_sha256": _signal_state_sha256_unlocked(root),
                    "status": "failed",
                },
            )
            _remove_stale_gate_unlocked(root)
        suffix = "last good signals preserved" if preserved else "no good signal record exists"
        raise CollectionError(f"collection failed; {suffix}: {error}") from error

    with state_lock(root):
        previous = _latest_signals_unlocked(root)
        changed = monitor_payload(previous) != monitor_payload(collected)
        # The monitor's meaningful bytes deliberately exclude fetch dates, but
        # a successful bounded collection still refreshes last-good freshness.
        # This permits daily no-op runs to remain silent without eventually
        # treating verified, unchanged signals as stale.
        payload = {
            "observed_date": observation_date,
            "schema_version": 1,
            "signals": [_signal_mapping(signal) for signal in collected],
        }
        signal_digest = hashlib.sha256(_canonical_json(payload)).hexdigest()
        if changed:
            atomic_write_json(_state_root(root) / SIGNALS_FILE, payload)
            _write_run_report(
                root,
                observation_date,
                "collect.json",
                {"signal_count": len(collected), "status": "collected"},
            )
        else:
            atomic_write_json(_state_root(root) / SIGNALS_FILE, payload)
        # Persist health after the signal record.  A stop between these writes
        # leaves a missing/mismatched marker, which gate evaluation rejects.
        atomic_write_json(
            _state_root(root) / COLLECTION_STATUS_FILE,
            {
                "observed_date": observation_date,
                "signals_sha256": signal_digest,
                "status": "ready",
            },
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
    """Stage one candidate, retaining unbuilt work until it is retired."""
    root = _repo_root(repo_root)
    candidate = validate_candidate(payload)
    digest = candidate_sha256(candidate)
    today = chicago_date()
    with state_lock(root):
        existing = _read_json(_candidate_path(root))
        if isinstance(existing, Mapping):
            previous_digest = existing.get("candidate_sha256")
            if previous_digest == digest:
                return {"candidate_sha256": digest, "status": "noop"}
            if not isinstance(previous_digest, str) or not previous_digest:
                raise CandidateError("staged candidate state lacks a safe identity")
            if not _has_successful_candidate_unlocked(root, previous_digest):
                raise CandidateError("a distinct candidate is already staged and unbuilt")
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


ProposalIdentity = tuple[int, int, int, int, str]


def _read_regular_candidate_bytes(candidate_path: Path) -> tuple[bytes, ProposalIdentity]:
    """Read one regular proposal and bind its exact bytes to its inode."""
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
        with os.fdopen(descriptor, "rb") as candidate_file:
            opened = os.fstat(candidate_file.fileno())
            opened_inode = (opened.st_dev, opened.st_ino)
            before_inode = (before.st_dev, before.st_ino)
            if not stat.S_ISREG(opened.st_mode) or opened_inode != before_inode:
                raise CandidateError("Scout proposal changed while it was being opened")
            raw = candidate_file.read()
            after = os.fstat(candidate_file.fileno())
    except OSError as error:
        raise CandidateError(f"cannot read candidate JSON: {candidate_path}") from error
    if (
        not stat.S_ISREG(after.st_mode)
        or (after.st_dev, after.st_ino) != before_inode
        or after.st_size != len(raw)
    ):
        raise CandidateError("Scout proposal changed while it was being read")
    identity: ProposalIdentity = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        hashlib.sha256(raw).hexdigest(),
    )
    return raw, identity


def _load_regular_candidate_file(candidate_path: Path) -> tuple[dict[str, Any], ProposalIdentity]:
    """Decode an exact no-follow proposal read for safe staged consumption."""
    raw, identity = _read_regular_candidate_bytes(candidate_path)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CandidateError(f"cannot read candidate JSON: {candidate_path}") from error
    if not isinstance(payload, Mapping):
        raise CandidateError("candidate JSON must be an object")
    return dict(payload), identity


def _proposal_matches_identity(candidate_path: Path, expected: ProposalIdentity) -> bool:
    """Verify a claimed handoff has not changed through a held descriptor."""
    try:
        _, current = _read_regular_candidate_bytes(candidate_path)
    except CandidateError:
        return False
    return current == expected


def _claim_scout_proposal(proposal: Path) -> Path:
    """Atomically move the one approved proposal aside before consuming it.

    ``os.replace`` moves a symlink itself rather than following it.  The
    subsequent no-follow regular-file read therefore rejects a swap safely,
    while a new Scout proposal written at the original lexical path survives.
    """
    try:
        before = proposal.lstat()
    except OSError as error:
        raise CandidateError(f"cannot read candidate JSON: {proposal}") from error
    if not stat.S_ISREG(before.st_mode):
        raise CandidateError("Scout proposal must be a regular file, not a symlink or special file")
    claimed = proposal.with_name(f".{proposal.name}.claim-{uuid.uuid4().hex}")
    try:
        os.replace(proposal, claimed)
    except OSError as error:
        raise CandidateError("Scout proposal could not be claimed atomically") from error
    return claimed


def _scout_receipt_root(repo_root: Path) -> Path:
    """Return the bounded ignored receipt quarantine without following links."""
    receipts = _state_root(repo_root) / SCOUT_RECEIPTS_DIRECTORY
    try:
        details = receipts.lstat()
    except FileNotFoundError:
        try:
            receipts.mkdir()
            details = receipts.lstat()
        except OSError as error:
            raise CandidateError("Scout receipt quarantine could not be created") from error
    except OSError as error:
        raise CandidateError("Scout receipt quarantine could not be inspected") from error
    if receipts.is_symlink() or not stat.S_ISDIR(details.st_mode):
        raise CandidateError("Scout receipt quarantine must be a regular directory")
    return receipts


def _create_scout_receipt(repo_root: Path, claimed: Path, identity: ProposalIdentity) -> Path:
    """Hard-link claimed bytes into bounded recovery before staging them.

    Scout writers may only atomically replace the approved proposal pathname.
    A successful consume preserves a durable receipt instead of destroying the
    claimed inode; if a producer retains an open descriptor and writes later,
    those bytes remain recoverable through this receipt.  Receipts are never
    automatically removed; capacity exhaustion requires operator review.
    """
    # The capacity check and hard-link reservation must be one state-locked
    # operation.  Independent Scout handoffs can otherwise both observe the
    # final apparent slot and create an unbounded pair of durable receipts.
    with state_lock(repo_root):
        return _create_scout_receipt_unlocked(repo_root, claimed, identity)


def _create_scout_receipt_unlocked(repo_root: Path, claimed: Path, identity: ProposalIdentity) -> Path:
    """Reserve one receipt while the Foundry state lock is already held."""
    receipts = _scout_receipt_root(repo_root)
    try:
        entries = list(receipts.iterdir())
    except OSError as error:
        raise CandidateError("Scout receipt quarantine could not be listed") from error
    if len(entries) >= SCOUT_RECEIPT_LIMIT:
        raise CandidateError("Scout receipt quarantine is full; operator recovery is required")
    for entry in entries:
        try:
            entry_stat = entry.lstat()
        except OSError as error:
            raise CandidateError("Scout receipt quarantine contains an unreadable entry") from error
        if entry.is_symlink() or not stat.S_ISREG(entry_stat.st_mode):
            raise CandidateError("Scout receipt quarantine contains an unsafe entry")

    for _ in range(8):
        receipt = receipts / f"{identity[-1]}-{uuid.uuid4().hex}.json"
        try:
            os.link(claimed, receipt, follow_symlinks=False)
        except FileExistsError:
            continue
        except (NotImplementedError, OSError) as error:
            raise CandidateError("Scout receipt quarantine cannot safely retain this proposal") from error
        if not _proposal_matches_identity(claimed, identity) or not _proposal_matches_identity(receipt, identity):
            raise CandidateError("Scout proposal changed before durable receipt creation")
        return receipt
    raise CandidateError("Scout receipt quarantine could not reserve a durable receipt")


def stage_candidate_file(repo_root: str | Path, candidate_path: str | Path, *, consume: bool = False) -> dict[str, Any]:
    """Stage a JSON candidate and consume only the one approved Scout proposal.

    ``--consume`` cannot remove an arbitrary path.  It is deliberately limited
    to the ignored ``foundry/state/scout-candidate.json`` handoff file, and it
    hard-links its claimed inode into a bounded ignored durable receipt before
    a successful stage or safe same-candidate no-op.  Scout writers must never
    write a claim or receipt path directly.
    """
    root = _repo_root(repo_root)
    proposal = _lexical_absolute(candidate_path)
    approved_proposal = _lexical_absolute(_state_root(root) / SCOUT_PROPOSAL_FILE)
    if consume and proposal != approved_proposal:
        raise CandidateError("only the approved ignored Scout proposal path may be consumed")
    if not consume:
        return stage_candidate(root, _load_candidate_file(proposal))

    claimed = _claim_scout_proposal(proposal)
    try:
        candidate, identity = _load_regular_candidate_file(claimed)
        _create_scout_receipt(root, claimed, identity)
        result = stage_candidate(root, candidate)
        try:
            if not _proposal_matches_identity(claimed, identity):
                raise CandidateError("Scout proposal changed before it could be consumed")
            # The durable hard-linked receipt still names this inode, so this
            # removes only the transient claim path, never held producer data.
            claimed.unlink()
        except FileNotFoundError as error:
            raise CandidateError("Scout proposal disappeared before it could be consumed") from error
        return result
    except Exception:
        # Preserve the original handoff if nothing newer has appeared at the
        # approved path.  If a newer proposal exists, never replace or delete
        # it; the claimed file remains for explicit recovery instead.
        try:
            proposal.lstat()
        except FileNotFoundError:
            if claimed.exists() or claimed.is_symlink():
                os.replace(claimed, proposal)
        raise


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


def _has_successful_candidate_unlocked(repo_root: Path, candidate_digest: str) -> bool:
    return any(
        release.get("status") == "success" and release.get("candidate_sha256") == candidate_digest
        for release in _ledger_unlocked(repo_root)
    )


def _has_successful_slug_version_unlocked(repo_root: Path, slug: str, version: str) -> bool:
    return any(
        release.get("status") == "success"
        and release.get("slug") == slug
        and release.get("version") == version
        for release in _ledger_unlocked(repo_root)
    )


def _append_success_unlocked(repo_root: Path, candidate_digest: str, slug: str, version: str,
                             archive_sha256: str | None = None) -> None:
    releases = _ledger_unlocked(repo_root)
    if _has_successful_release_unlocked(repo_root, candidate_digest, version):
        return
    if _has_successful_slug_version_unlocked(repo_root, slug, version):
        raise ReleaseError("a different candidate already owns this successful slug/version")
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


def _update_gate(candidate: Mapping[str, Any], signals: Sequence[Signal],
                 released_slugs: set[str]) -> tuple[bool, list[str], list[str]]:
    if candidate["slug"] not in released_slugs:
        _, reasons, matched_ids = _new_sku_gate(candidate, signals, released_slugs)
        return False, ["update_requires_existing_successful_release", *reasons], matched_ids
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


def _signal_observation_date(signal: Signal) -> date | None:
    try:
        observed = datetime.fromisoformat(signal.observed_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    if observed.tzinfo is None:
        return None
    return observed.astimezone(CHICAGO).date()


def _stale_signal_ids(candidate: Mapping[str, Any], signals: Sequence[Signal], as_of: str) -> list[str]:
    """Return cited evidence that is malformed, future-dated, or beyond TTL."""
    today = date.fromisoformat(as_of)
    cited = set(candidate["signal_ids"])
    stale: list[str] = []
    for signal in signals:
        if signal.signal_id not in cited:
            continue
        observed_date = _signal_observation_date(signal)
        age = (today - observed_date).days if observed_date is not None else None
        if age is None or age < 0 or age > MAX_SIGNAL_AGE_DAYS:
            stale.append(signal.signal_id)
    return stale


def _remove_stale_gate_unlocked(repo_root: Path) -> None:
    try:
        (_state_root(repo_root) / GATE_FILE).unlink()
    except FileNotFoundError:
        pass


def gate_candidate(repo_root: str | Path, payload: Mapping[str, Any], *,
                   as_of_date: str | None = None) -> dict[str, Any]:
    """Evaluate a candidate and persist a gate artifact only when it passes."""
    root = _repo_root(repo_root)
    candidate = validate_candidate(payload)
    digest = candidate_sha256(candidate)
    generated_date = _validated_date(as_of_date)
    with state_lock(root):
        signals = _latest_signals_unlocked(root)
        ready_signals_sha256 = _ready_signal_state_sha256_unlocked(root)
        collection_stale = ready_signals_sha256 is None
        released_slugs = _released_slugs_unlocked(root)
        if _has_successful_candidate_unlocked(root, digest):
            passed, reasons, matched_ids = False, ["candidate_already_successfully_released"], []
        elif candidate["candidate_type"] == NEW_SKU:
            passed, reasons, matched_ids = _new_sku_gate(candidate, signals, released_slugs)
        else:
            passed, reasons, matched_ids = _update_gate(candidate, signals, released_slugs)
        stale_signal_ids = _stale_signal_ids(candidate, signals, generated_date)
        if stale_signal_ids or collection_stale:
            passed = False
            if "stale_signal_evidence" not in reasons:
                reasons = [*reasons, "stale_signal_evidence"]
        result = {
            "candidate": candidate,
            "candidate_sha256": digest,
            "candidate_type": candidate["candidate_type"],
            "generated_date": generated_date,
            "matched_signal_ids": matched_ids,
            "passed": passed,
            "reasons": reasons,
            "collection_stale": collection_stale,
            "signals_sha256": ready_signals_sha256,
            "stale_signal_ids": stale_signal_ids,
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
        digest = staged.get("candidate_sha256")
        if isinstance(digest, str) and _has_successful_candidate_unlocked(root, digest):
            _candidate_path(root).unlink(missing_ok=True)
            _remove_stale_gate_unlocked(root)
            return None
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


def _is_reparse_point(path: Path | str, details: os.stat_result | None = None) -> bool:
    """Recognize a Windows junction/reparse entry without following it."""
    try:
        entry = details if details is not None else Path(path).lstat()
    except OSError:
        return False
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(entry, "st_file_attributes", 0) & flag)


def _is_link_or_reparse(path: Path | str, details: os.stat_result) -> bool:
    """Treat symbolic links and Windows reparse points as equally unsafe."""
    return stat.S_ISLNK(details.st_mode) or _is_reparse_point(path, details)


def source_tree_revision(source: str | Path) -> str:
    """Return the release source identity, including ignored paid-source bytes.

    Public Git history intentionally does not contain paid source, so a Git
    commit is never used as ``source_revision``.  The deterministic tree
    digest is the source identity that goes into the archive manifest.
    """
    source_root = Path(os.path.abspath(os.fspath(Path(source).expanduser())))
    try:
        root_stat = source_root.lstat()
    except OSError as error:
        raise ReleaseError("source tree for revision hashing is unavailable") from error
    if _is_link_or_reparse(source_root, root_stat) or not stat.S_ISDIR(root_stat.st_mode):
        raise ReleaseError("source tree contains a link or reparse root")
    resolved_root = source_root.resolve()
    try:
        resolved_stat = resolved_root.lstat()
    except OSError as error:
        raise ReleaseError("source tree changed while being resolved") from error
    if (
        _is_link_or_reparse(resolved_root, resolved_stat)
        or not stat.S_ISDIR(resolved_stat.st_mode)
        or (root_stat.st_dev, root_stat.st_ino) != (resolved_stat.st_dev, resolved_stat.st_ino)
    ):
        raise ReleaseError("source tree changed or contains a link or reparse root")
    source_root = resolved_root

    files: list[Path] = []
    for current, directories, filenames in os.walk(source_root, followlinks=False):
        current_path = Path(current)
        for directory in directories:
            directory_path = current_path / directory
            try:
                directory_stat = directory_path.lstat()
            except OSError as error:
                raise ReleaseError("source tree changed while being hashed") from error
            if _is_link_or_reparse(directory_path, directory_stat) or not stat.S_ISDIR(directory_stat.st_mode):
                raise ReleaseError("source tree contains a link or reparse non-directory entry")
        for filename in filenames:
            file_path = current_path / filename
            try:
                file_stat = file_path.lstat()
            except OSError as error:
                raise ReleaseError("source tree changed while being hashed") from error
            if _is_link_or_reparse(file_path, file_stat) or not stat.S_ISREG(file_stat.st_mode):
                raise ReleaseError("source tree contains a link or reparse non-regular file")
            files.append(file_path)

    digest = hashlib.sha256()
    digest.update(b"hermes-source-tree-v2\0")
    for path in sorted(files, key=lambda item: item.relative_to(source_root).as_posix().encode("utf-8")):
        relative_path = path.relative_to(source_root).as_posix().encode("utf-8")
        try:
            content = path.read_bytes()
        except OSError as error:
            raise ReleaseError("source tree changed while being hashed") from error
        # Each entry has its own digest and all variable-length values are
        # length-prefixed.  A NUL in a filename's content can never blur the
        # boundary between the current file and the next file.
        file_digest = hashlib.sha256()
        file_digest.update(struct.pack(">Q", len(relative_path)))
        file_digest.update(relative_path)
        file_digest.update(struct.pack(">Q", len(content)))
        file_digest.update(content)
        digest.update(struct.pack(">Q", len(relative_path)))
        digest.update(relative_path)
        digest.update(file_digest.digest())
    return f"tree-sha256:{digest.hexdigest()}"


def _private_product_source(repo_root: Path, slug: str) -> Path:
    """Return a lexical private-product directory without following links.

    ``package_release`` treats the ignored ``private-products`` directory as
    the security boundary for paid source.  Checking only the final product
    path is insufficient: a linked ``private-products`` parent would make the
    snapshotter execute files outside that boundary.  Keep this check lexical
    so the final component is never resolved as part of validation.
    """
    private_products = repo_root / "private-products"
    source = private_products / slug
    for path, label in ((private_products, "private-products"), (source, "private product source")):
        try:
            entry = path.lstat()
        except OSError as error:
            raise ReleaseError(f"{label} is unavailable") from error
        if _is_link_or_reparse(path, entry) or not stat.S_ISDIR(entry.st_mode):
            raise ReleaseError(f"{label} must be a regular directory, not a link or reparse point")
    return source


def _supports_secure_snapshot_host() -> bool:
    """Whether this host can enforce the FD-relative no-follow snapshot contract.

    Authority packaging needs directory descriptors plus no-follow opens for
    every source component.  Native Windows remains a supported compatibility
    and monitoring target, but its Python file APIs do not offer that same
    verified directory-FD contract.  Refuse packaging there instead of
    silently falling back to path-based traversal.
    """
    return (
        os.name == "posix"
        and bool(getattr(os, "O_DIRECTORY", 0))
        and bool(getattr(os, "O_NOFOLLOW", 0))
    )


def _snapshot_source_tree(source: Path, destination: Path) -> None:
    """Copy a regular-file source tree without ever following source links."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)

    def open_directory(path: Path | str, *, directory_fd: int | None = None) -> int:
        try:
            descriptor = os.open(path, flags | directory_flag, dir_fd=directory_fd)
        except OSError as error:
            raise ReleaseError("private product source contains an unreadable or linked directory") from error
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode):
            os.close(descriptor)
            raise ReleaseError("private product source contains a non-directory entry")
        return descriptor

    def copy_regular_file(name: str, source_fd: int, target: Path, source_path: Path) -> None:
        try:
            descriptor = os.open(name, flags, dir_fd=source_fd)
        except OSError as error:
            raise ReleaseError("private product source contains an unreadable or linked file") from error
        try:
            opened = os.fstat(descriptor)
            if _is_link_or_reparse(source_path, opened) or not stat.S_ISREG(opened.st_mode):
                raise ReleaseError("private product source contains a non-regular file")
            with os.fdopen(descriptor, "rb", closefd=True) as source_file:
                descriptor = -1
                with target.open("xb") as destination_file:
                    while chunk := source_file.read(1024 * 1024):
                        destination_file.write(chunk)
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def copy_directory(source_fd: int, target: Path, source_path: Path) -> None:
        try:
            entries = sorted(list(os.scandir(source_fd)), key=lambda entry: entry.name)
            for entry in entries:
                try:
                    entry_stat = os.stat(entry.name, dir_fd=source_fd, follow_symlinks=False)
                except OSError as error:
                    raise ReleaseError("private product source changed while snapshotting") from error
                entry_path = source_path / entry.name
                if _is_link_or_reparse(entry_path, entry_stat):
                    raise ReleaseError("private product source contains a symlink or reparse point")
                if stat.S_ISDIR(entry_stat.st_mode):
                    child_target = target / entry.name
                    child_target.mkdir()
                    child_fd = open_directory(entry.name, directory_fd=source_fd)
                    try:
                        copy_directory(child_fd, child_target, entry_path)
                    finally:
                        os.close(child_fd)
                elif stat.S_ISREG(entry_stat.st_mode):
                    copy_regular_file(entry.name, source_fd, target / entry.name, entry_path)
                else:
                    raise ReleaseError("private product source contains a non-regular file")
        except OSError as error:
            raise ReleaseError("private product source changed while snapshotting") from error

    try:
        source_stat = source.lstat()
    except OSError as error:
        raise ReleaseError("private product source is unavailable") from error
    if _is_link_or_reparse(source, source_stat) or not stat.S_ISDIR(source_stat.st_mode):
        raise ReleaseError("private product source is unavailable")
    destination.mkdir(parents=True)
    source_fd = open_directory(source)
    try:
        copy_directory(source_fd, destination, source)
    finally:
        os.close(source_fd)


def _snapshot_file_hashes(source: Path) -> dict[str, str]:
    """Return release-member file hashes for a previously safe snapshot."""
    try:
        root_stat = source.lstat()
    except OSError as error:
        raise ReleaseError("snapshot is unavailable") from error
    if _is_link_or_reparse(source, root_stat) or not stat.S_ISDIR(root_stat.st_mode):
        raise ReleaseError("snapshot contains a linked or invalid root")
    root = source.resolve()
    hashes: dict[str, str] = {}
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        for directory in directories:
            path = current_path / directory
            try:
                details = path.lstat()
            except OSError as error:
                raise ReleaseError("snapshot changed while being hashed") from error
            if _is_link_or_reparse(path, details) or not stat.S_ISDIR(details.st_mode):
                raise ReleaseError("snapshot contains a linked or invalid directory")
        for filename in filenames:
            path = current_path / filename
            try:
                details = path.lstat()
            except OSError as error:
                raise ReleaseError("snapshot changed while being hashed") from error
            if _is_link_or_reparse(path, details) or not stat.S_ISREG(details.st_mode):
                raise ReleaseError("snapshot contains a linked or invalid file")
            hashes[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return dict(sorted(hashes.items()))


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


def _release_metadata(repo_root: Path, snapshot_root: Path, source: Path,
                      public_metadata: Mapping[str, Any], slug: str,
                      source_revision: str) -> dict[str, Any]:
    version_file = source / "VERSION"
    if not version_file.is_file():
        raise ReleaseError("private product VERSION file is required")
    source_version = version_file.read_text(encoding="utf-8").strip()
    if source_version != public_metadata["version"]:
        raise ReleaseError("private VERSION does not match public product metadata")
    update_end = date.fromisoformat(chicago_date()) + timedelta(days=30)
    test_command, output_sha256 = _run_product_tests(snapshot_root, slug)
    if source_tree_revision(source) != source_revision:
        raise ReleaseError("safe package snapshot changed during product tests")
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
    ready_signals_sha256 = _ready_signal_state_sha256_unlocked(repo_root)
    if ready_signals_sha256 is None:
        raise ReleaseError("latest collection evidence is stale or unverifiable")
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
    gate_signals_sha256 = gate.get("signals_sha256")
    if not isinstance(gate_signals_sha256, str) or gate_signals_sha256 != ready_signals_sha256:
        raise ReleaseError("gate.json signal evidence no longer matches the current ready collection")
    return dict(gate)


def _audit_generated_copy(payloads: Mapping[str, Mapping[str, Any]]) -> None:
    """Apply the canonical public-claims audit before release files exist."""
    findings = []
    for name, payload in payloads.items():
        findings.extend(audit_text(_canonical_json(dict(payload)).decode("ascii"), name))
    if findings:
        codes = ", ".join(sorted({finding.code for finding in findings}))
        raise ReleaseError(f"generated public release copy audit failed: {codes}")


def _retire_staged_candidate_unlocked(repo_root: Path, candidate_digest: str) -> None:
    """Remove a fulfilled handoff so the weekly Builder monitor stays quiet."""
    staged = _read_json(_candidate_path(repo_root))
    if isinstance(staged, Mapping) and staged.get("candidate_sha256") == candidate_digest:
        _candidate_path(repo_root).unlink(missing_ok=True)
    _remove_stale_gate_unlocked(repo_root)


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
        if _has_successful_slug_version_unlocked(root, slug, version):
            raise ReleaseError("a different candidate already owns this successful slug/version")
        if not _supports_secure_snapshot_host():
            raise ReleaseError("authority packaging requires a secure no-follow snapshot host")

        public_metadata = _read_public_metadata(root, slug, version)
        _audit_generated_copy({"public-product-metadata.json": public_metadata})
        source = _private_product_source(root, slug)
        archive_path = root / "dist" / "hermespacks" / f"{slug}-{version}.zip"
        manifest_path = archive_path.with_suffix(".manifest.json")
        listing_path = archive_path.with_suffix(".listing.json")

        # The snapshot is a disposable, private-products-contained miniature
        # repository.  The original paid source is never audited through a
        # test process and is never reread after this copy completes.
        with tempfile.TemporaryDirectory(prefix="hermes-foundry-package-") as temporary_directory:
            snapshot_root = Path(temporary_directory) / "repository"
            snapshot_source = snapshot_root / "private-products" / slug
            _snapshot_source_tree(source, snapshot_source)
            source_findings = audit_tree(snapshot_source)
            if source_findings:
                codes = ", ".join(sorted({finding.code for finding in source_findings}))
                raise ReleaseError(f"private source snapshot audit failed: {codes}")
            source_revision = source_tree_revision(snapshot_source)
            release_metadata = _release_metadata(
                root, snapshot_root, snapshot_source, public_metadata, slug, source_revision
            )
            snapshot_archive = snapshot_root / "dist" / "hermespacks" / archive_path.name
            try:
                manifest = build_release(
                    snapshot_source, snapshot_archive, release_metadata, repo_root=snapshot_root
                )
            except PackagingError as error:
                raise ReleaseError(f"package verification failed: {error}") from error
            if source_tree_revision(snapshot_source) != source_revision:
                raise ReleaseError("safe package snapshot changed while packaging")
            if _snapshot_file_hashes(snapshot_source) != manifest.files:
                raise ReleaseError("safe package snapshot bytes do not match the verified archive")

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
            _audit_generated_copy({
                "release-manifest.json": release_report,
                "listing-copy.json": listing_copy,
            })
            archive_bytes = snapshot_archive.read_bytes()
            if hashlib.sha256(archive_bytes).hexdigest() != manifest.archive_sha256:
                raise ReleaseError("verified snapshot archive changed before installation")

        atomic_write_bytes(archive_path, archive_bytes)
        atomic_write_json(manifest_path, release_report)
        atomic_write_json(listing_path, listing_copy)
        _append_success_unlocked(root, candidate_digest, slug, version, manifest.archive_sha256)
        _retire_staged_candidate_unlocked(root, candidate_digest)
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
