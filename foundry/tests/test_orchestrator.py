"""Tests for deterministic, fail-closed Foundry orchestration."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import zipfile

from foundry.src.models import Signal
from foundry.src.orchestrator import (
    CandidateError,
    CollectionError,
    ReleaseError,
    atomic_write_json,
    chicago_date,
    collect_signals,
    gate_candidate,
    gate_current_candidate,
    latest_signals,
    mark_successful_release,
    monitor_payload,
    package_release,
    stage_candidate,
    stage_candidate_file,
    source_tree_revision,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "foundry" / "tests" / "fixtures"
OBSERVED_AT = "2026-08-23T00:00:00-05:00"
# Windows is a compatibility target.  Authority packaging requires the
# FD-relative O_DIRECTORY/O_NOFOLLOW snapshot contract and is tested on the
# POSIX builder host; native Windows instead runs the explicit fail-closed
# coverage below.
POSIX_SNAPSHOT_REQUIRED = unittest.skipIf(
    os.name == "nt", "authority packaging requires the POSIX no-follow snapshot host"
)


def signal(signal_id: str, source_url: str, source_type: str, metrics: dict,
           independence_key: str) -> Signal:
    return Signal(
        signal_id=signal_id,
        source_url=source_url,
        source_type=source_type,
        observed_at=OBSERVED_AT,
        title=signal_id,
        metrics=metrics,
        content_sha256=f"content-{signal_id}",
        independence_key=independence_key,
    )


def accepted_signals() -> list[Signal]:
    return [
        signal("paid", "https://gumroad.com/l/comparable", "paid_comparable", {"sales_count": 119}, "gumroad"),
        signal("release", "https://api.github.com/repos/NousResearch/hermes-agent/releases/latest", "official_release", {}, "github"),
        signal("adoption", "https://pypistats.org/api/packages/hermes-agent/recent", "adoption_signal", {}, "pypistats"),
    ]


def new_candidate(slug: str = "operator-kit") -> dict:
    return {
        "candidate_type": "new_sku",
        "slug": slug,
        "signal_ids": ["paid", "release", "adoption"],
        "buyer": "Hermes operator",
        "job_to_be_done": "route routine work locally without unsafe fallback",
        "product_delta": "tested diagnostics and routing checklists",
    }


@unittest.skipIf(os.name == "nt", "mutable Foundry authority state requires the POSIX descriptor-bound host")
class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        state = self.root / "foundry" / "state"
        state.mkdir(parents=True)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def write_signals(self, signals: list[Signal]) -> None:
        signal_path = self.root / "foundry" / "state" / "latest-signals.json"
        atomic_write_json(
            signal_path,
            {
                "observed_date": signals[0].observed_at[:10] if signals else "2026-08-23",
                "signals": [
                    {
                        "signal_id": item.signal_id,
                        "source_url": item.source_url,
                        "source_type": item.source_type,
                        "observed_at": item.observed_at,
                        "title": item.title,
                        "metrics": item.metrics,
                        "content_sha256": item.content_sha256,
                        "independence_key": item.independence_key,
                    }
                    for item in signals
                ],
            },
        )
        atomic_write_json(
            self.root / "foundry" / "state" / "collection-status.json",
            {
                "observed_date": signals[0].observed_at[:10] if signals else "2026-08-23",
                "signals_sha256": hashlib.sha256(signal_path.read_bytes()).hexdigest(),
                "status": "ready",
            },
        )

    def scout_receipts(self, state_root: Path | None = None) -> list[Path]:
        """Return only durable direct-state receipt entries, not claims or locks."""
        root = state_root if state_root is not None else self.root / "foundry" / "state"
        return sorted(root.glob(".scout-receipt-*.json"))

    def write_packagable_product(self, slug: str = "hermes-hybrid-operator-kit", *,
                                 version: str = "1.0.0", test_body: str | None = None,
                                 metadata_overrides: dict | None = None) -> Path:
        """Create the minimum safe private/public pair used by package tests."""
        source = self.root / "private-products" / slug
        (source / "tests").mkdir(parents=True)
        (source / "scripts").mkdir()
        (source / "README.md").write_text("Local routing kit.\n", encoding="utf-8")
        (source / "VERSION").write_text(f"{version}\n", encoding="utf-8")
        (source / "THIRD_PARTY_NOTICES.md").write_text("No third-party code.\n", encoding="utf-8")
        (source / "tests" / "test_kit.py").write_text(
            test_body or "import sys\nraise SystemExit(0)\n", encoding="utf-8"
        )
        (source / "scripts" / "install.sh").write_text("#!/bin/sh\necho staged\n", encoding="utf-8")
        products = self.root / "products"
        products.mkdir(exist_ok=True)
        metadata = {
            "slug": slug,
            "name": "Hermes Hybrid Operator Kit",
            "version": version,
            "price_usd": 12,
            "checkout_status": "pending_payout_onboarding",
            "publication_status": "not_published",
            "verified_revenue_usd": 0,
            "update_policy": "Current release plus 30 days of corrections.",
            "verification_status": "pending_release_verification",
            "non_affiliation": "Independent product with no relationship to Nous Research or OpenAI.",
        }
        metadata.update(metadata_overrides or {})
        (products / f"{slug}.json").write_text(json.dumps(metadata), encoding="utf-8")
        return source

    def test_no_change_monitor_is_byte_stable(self):
        first = monitor_payload(accepted_signals())
        second = monitor_payload(accepted_signals())
        self.assertEqual(first, second)
        self.assertNotIn(b"observed_at", first)
        self.assertNotIn(b"content_sha256", first)
        self.assertNotIn(b"2026-08-23", first)

    def test_monitor_normalization_ignores_dates_and_content_hash_churn(self):
        original = accepted_signals()[0]
        changed_churn_only = Signal(
            **{**original.__dict__, "observed_at": "2026-08-24T00:00:00-05:00", "content_sha256": "new"}
        )
        self.assertEqual(monitor_payload([original]), monitor_payload([changed_churn_only]))

    def test_atomic_json_uses_a_sibling_replace_and_never_leaves_torn_output(self):
        target = self.root / "foundry" / "state" / "payload.json"
        atomic_write_json(target, {"z": 1, "a": [2]})
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"a": [2], "z": 1})
        self.assertEqual(list(target.parent.glob(f".{target.name}.*.tmp")), [])

    def test_windows_authority_candidate_mutation_fails_closed_without_directory_relative_io(self):
        """A Windows path lock is not authority to alter Scout/Builder state.

        The standard library cannot make Windows claim/stage writes immune to
        an ancestor-entry rename, so the control plane rejects them before
        state access. The authority host is the POSIX descriptor-bound builder,
        rather than a best-effort Windows pathname transaction.
        """
        from foundry.src import orchestrator

        state = self.root / "foundry" / "state"
        proposal = state / "scout-candidate.json"
        proposal.write_text(json.dumps(new_candidate("windows-authority-kit")), encoding="utf-8")
        original_proposal = proposal.read_bytes()
        with mock.patch.object(orchestrator, "fcntl", None):
            with self.assertRaisesRegex(orchestrator.FoundryError, "POSIX no-follow directory-FD host"):
                stage_candidate(self.root, new_candidate("windows-authority-kit"))
            with self.assertRaisesRegex(orchestrator.FoundryError, "POSIX no-follow directory-FD host"):
                stage_candidate_file(self.root, proposal, consume=True)
        self.assertFalse((state / "candidate.json").exists())
        self.assertEqual(proposal.read_bytes(), original_proposal)

    @unittest.skipIf(os.name == "nt", "POSIX directory descriptors are required")
    def test_posix_state_lock_survives_legacy_lock_path_replacement(self):
        """Replacing a legacy file cannot split a directory-FD state lock."""
        from foundry.src import orchestrator

        state = self.root / "foundry" / "state"
        legacy_lock = state / ".lock"
        legacy_lock.write_bytes(b"old")
        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()
        errors: list[BaseException] = []

        def first_holder():
            try:
                with orchestrator.state_lock(self.root):
                    first_entered.set()
                    release_first.wait(timeout=5)
            except BaseException as error:
                errors.append(error)

        def second_holder():
            try:
                with orchestrator.state_lock(self.root):
                    second_entered.set()
            except BaseException as error:
                errors.append(error)

        first = threading.Thread(target=first_holder)
        first.start()
        self.assertTrue(first_entered.wait(timeout=3))
        legacy_lock.unlink()
        legacy_lock.write_bytes(b"replacement")
        second = threading.Thread(target=second_holder)
        second.start()
        try:
            self.assertFalse(second_entered.wait(timeout=0.25))
        finally:
            release_first.set()
            first.join(timeout=5)
            second.join(timeout=5)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(second_entered.is_set())

    @unittest.skipIf(os.name == "nt", "POSIX directory descriptors are required")
    def test_posix_state_lock_survives_state_directory_replacement(self):
        """A replacement state directory cannot create a second lock domain."""
        from foundry.src import orchestrator

        state = self.root / "foundry" / "state"
        original_state = self.root / "foundry" / "state-original"
        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()
        errors: list[BaseException] = []

        def holder(event: threading.Event, release: threading.Event | None = None):
            try:
                with orchestrator.state_lock(self.root):
                    event.set()
                    if release is not None:
                        release.wait(timeout=5)
            except BaseException as error:
                errors.append(error)

        first = threading.Thread(target=holder, args=(first_entered, release_first))
        first.start()
        self.assertTrue(first_entered.wait(timeout=3))
        os.replace(state, original_state)
        state.mkdir()
        second = threading.Thread(target=holder, args=(second_entered,))
        second.start()
        try:
            self.assertFalse(second_entered.wait(timeout=0.25))
        finally:
            release_first.set()
            first.join(timeout=5)
            second.join(timeout=5)
            if state.exists() and not state.is_symlink():
                state.rmdir()
            if original_state.exists():
                os.replace(original_state, state)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(second_entered.is_set())

    @unittest.skipIf(os.name == "nt", "POSIX directory descriptors are required")
    def test_posix_state_lock_never_follows_a_legacy_lock_symlink(self):
        from foundry.src import orchestrator

        outside = self.root / "outside-lock"
        outside.write_bytes(b"")
        (self.root / "foundry" / "state" / ".lock").symlink_to(outside)
        with orchestrator.state_lock(self.root):
            pass
        self.assertEqual(outside.read_bytes(), b"")

    def test_collection_uses_fixtures_and_preserves_last_good_signals_on_failure(self):
        config = json.loads((REPO_ROOT / "foundry" / "config.json").read_text(encoding="utf-8"))
        result = collect_signals(self.root, "2026-08-23", config=config, fixture_dir=FIXTURES)
        self.assertTrue(result["changed"])
        before = (self.root / "foundry" / "state" / "latest-signals.json").read_bytes()
        with mock.patch("foundry.src.orchestrator.collect", side_effect=ValueError("source unavailable")):
            with self.assertRaises(CollectionError) as raised:
                collect_signals(self.root, "2026-08-24", config=config, fixture_dir=FIXTURES)
        self.assertIn("last good signals preserved", str(raised.exception))
        self.assertEqual((self.root / "foundry" / "state" / "latest-signals.json").read_bytes(), before)

    def test_unchanged_collection_is_a_silent_noop_that_refreshes_last_good_observation(self):
        config = json.loads((REPO_ROOT / "foundry" / "config.json").read_text(encoding="utf-8"))
        first = collect_signals(self.root, "2026-08-23", config=config, fixture_dir=FIXTURES)
        before = (self.root / "foundry" / "state" / "latest-signals.json").read_bytes()
        second = collect_signals(self.root, "2026-08-24", config=config, fixture_dir=FIXTURES)
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        refreshed = (self.root / "foundry" / "state" / "latest-signals.json").read_bytes()
        self.assertNotEqual(refreshed, before)
        refreshed_payload = json.loads(refreshed)
        self.assertEqual(refreshed_payload["observed_date"], "2026-08-24")
        self.assertTrue(all("2026-08-24" in item["observed_at"] for item in refreshed_payload["signals"]))
        self.assertEqual(monitor_payload(json.loads(before)), monitor_payload(refreshed_payload))

    def test_staged_candidate_citations_survive_a_multiday_unchanged_collection(self):
        config = json.loads((REPO_ROOT / "foundry" / "config.json").read_text(encoding="utf-8"))
        collect_signals(self.root, "2026-08-23", config=config, fixture_dir=FIXTURES)
        cited = [
            item.signal_id
            for item in latest_signals(self.root)
            if item.source_type in {"paid_comparable", "adoption_signal", "official_release"}
        ]
        candidate = new_candidate("multiday-kit")
        candidate["signal_ids"] = cited
        stage_candidate(self.root, candidate)
        refreshed = collect_signals(self.root, "2026-08-24", config=config, fixture_dir=FIXTURES)
        self.assertFalse(refreshed["changed"])
        with mock.patch("foundry.src.orchestrator.chicago_date", return_value="2026-08-24"):
            gate = gate_current_candidate(self.root)
        self.assertTrue(gate and gate["passed"], gate)

    def test_failed_collection_immediately_makes_preserved_evidence_gate_ineligible_until_recovery(self):
        config = json.loads((REPO_ROOT / "foundry" / "config.json").read_text(encoding="utf-8"))
        collect_signals(self.root, "2026-08-23", config=config, fixture_dir=FIXTURES)
        candidate = new_candidate("collection-health-kit")
        candidate["signal_ids"] = [
            item.signal_id
            for item in latest_signals(self.root)
            if item.source_type in {"paid_comparable", "adoption_signal", "official_release"}
        ]
        self.assertTrue(gate_candidate(self.root, candidate, as_of_date="2026-08-23")["passed"])
        before = (self.root / "foundry" / "state" / "latest-signals.json").read_bytes()
        with mock.patch("foundry.src.orchestrator.collect", side_effect=ValueError("source unavailable")):
            with self.assertRaises(CollectionError):
                collect_signals(self.root, "2026-08-24", config=config, fixture_dir=FIXTURES)
        self.assertEqual((self.root / "foundry" / "state" / "latest-signals.json").read_bytes(), before)
        failed_gate = gate_candidate(self.root, candidate, as_of_date="2026-08-24")
        self.assertFalse(failed_gate["passed"])
        self.assertIn("stale_signal_evidence", failed_gate["reasons"])
        status_path = self.root / "foundry" / "state" / "collection-status.json"
        self.assertTrue(status_path.is_file())
        self.assertEqual(json.loads(status_path.read_text(encoding="utf-8"))["status"], "failed")
        collect_signals(self.root, "2026-08-25", config=config, fixture_dir=FIXTURES)
        self.assertTrue(status_path.is_file())
        self.assertEqual(json.loads(status_path.read_text(encoding="utf-8"))["status"], "ready")
        self.assertTrue(gate_candidate(self.root, candidate, as_of_date="2026-08-25")["passed"])

    def test_gate_requires_a_matching_successful_collection_marker(self):
        config = json.loads((REPO_ROOT / "foundry" / "config.json").read_text(encoding="utf-8"))
        collect_signals(self.root, "2026-08-23", config=config, fixture_dir=FIXTURES)
        candidate = new_candidate("marker-integrity-kit")
        candidate["signal_ids"] = [
            item.signal_id
            for item in latest_signals(self.root)
            if item.source_type in {"paid_comparable", "adoption_signal", "official_release"}
        ]
        status_path = self.root / "foundry" / "state" / "collection-status.json"
        signal_path = self.root / "foundry" / "state" / "latest-signals.json"
        self.assertTrue(status_path.is_file())
        ready = json.loads(status_path.read_text(encoding="utf-8"))
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["signals_sha256"], hashlib.sha256(signal_path.read_bytes()).hexdigest())

        status_path.unlink()
        missing = gate_candidate(self.root, candidate, as_of_date="2026-08-23")
        self.assertFalse(missing["passed"])
        self.assertIn("stale_signal_evidence", missing["reasons"])

        collect_signals(self.root, "2026-08-24", config=config, fixture_dir=FIXTURES)
        ready = json.loads(status_path.read_text(encoding="utf-8"))
        ready.pop("observed_date")
        atomic_write_json(status_path, ready)
        missing_date = gate_candidate(self.root, candidate, as_of_date="2026-08-24")
        self.assertFalse(missing_date["passed"])
        self.assertIn("stale_signal_evidence", missing_date["reasons"])

        collect_signals(self.root, "2026-08-24", config=config, fixture_dir=FIXTURES)
        ready = json.loads(status_path.read_text(encoding="utf-8"))
        ready["observed_date"] = "not-a-date"
        atomic_write_json(status_path, ready)
        invalid_date = gate_candidate(self.root, candidate, as_of_date="2026-08-24")
        self.assertFalse(invalid_date["passed"])
        self.assertIn("stale_signal_evidence", invalid_date["reasons"])

        collect_signals(self.root, "2026-08-24", config=config, fixture_dir=FIXTURES)
        ready = json.loads(status_path.read_text(encoding="utf-8"))
        ready["observed_date"] = "2026-08-23"
        atomic_write_json(status_path, ready)
        mismatched_date = gate_candidate(self.root, candidate, as_of_date="2026-08-24")
        self.assertFalse(mismatched_date["passed"])
        self.assertIn("stale_signal_evidence", mismatched_date["reasons"])

        collect_signals(self.root, "2026-08-24", config=config, fixture_dir=FIXTURES)
        atomic_write_json(status_path, {"status": "ready", "signals_sha256": "wrong"})
        mismatched = gate_candidate(self.root, candidate, as_of_date="2026-08-24")
        self.assertFalse(mismatched["passed"])
        self.assertIn("stale_signal_evidence", mismatched["reasons"])

        status_path.write_text("not json", encoding="utf-8")
        malformed = gate_candidate(self.root, candidate, as_of_date="2026-08-24")
        self.assertFalse(malformed["passed"])
        self.assertIn("stale_signal_evidence", malformed["reasons"])

    def test_task6_candidate_fixture_passes_against_collected_fixture_signals(self):
        config = json.loads((REPO_ROOT / "foundry" / "config.json").read_text(encoding="utf-8"))
        candidate = json.loads((FIXTURES / "operator-kit-candidate.json").read_text(encoding="utf-8"))
        collect_signals(self.root, "2026-08-23", config=config, fixture_dir=FIXTURES)
        result = gate_candidate(self.root, candidate)
        self.assertTrue(result["passed"], result["reasons"])

    def test_stage_candidate_permits_only_one_distinct_candidate(self):
        self.write_signals(accepted_signals())
        self.assertEqual(stage_candidate(self.root, new_candidate())["status"], "staged")
        self.assertEqual(stage_candidate(self.root, new_candidate())["status"], "noop")
        second = new_candidate("different-kit")
        with self.assertRaises(CandidateError):
            stage_candidate(self.root, second)

    def test_scout_proposal_path_is_consumed_only_after_successful_stage(self):
        proposal = self.root / "foundry" / "state" / "scout-candidate.json"
        original_bytes = json.dumps(new_candidate()).encode("utf-8")
        proposal.write_bytes(original_bytes)
        result = stage_candidate_file(self.root, proposal, consume=True)
        self.assertEqual(result["status"], "staged")
        self.assertFalse(proposal.exists())
        receipts = self.scout_receipts()
        self.assertEqual(len(receipts), 1)
        self.assertTrue(receipts[0].name.startswith(f".scout-receipt-{hashlib.sha256(original_bytes).hexdigest()}-"))
        self.assertEqual(receipts[0].read_bytes(), original_bytes)
        other = self.root / "candidate.json"
        other.write_text(json.dumps(new_candidate("other-kit")), encoding="utf-8")
        with self.assertRaises(CandidateError):
            stage_candidate_file(self.root, other, consume=True)
        self.assertTrue(other.exists())

    def test_scout_proposal_symlink_is_never_followed_or_target_deleted(self):
        proposal = self.root / "foundry" / "state" / "scout-candidate.json"
        target = self.root / "outside-candidate.json"
        target.write_text(json.dumps(new_candidate()), encoding="utf-8")
        proposal.symlink_to(target)
        with self.assertRaises(CandidateError):
            stage_candidate_file(self.root, proposal, consume=True)
        self.assertTrue(proposal.is_symlink())
        self.assertTrue(target.is_file())

    def test_scout_proposal_reparse_point_is_never_claimed(self):
        """Windows file reparse points fail before a claim can move them."""
        proposal = self.root / "foundry" / "state" / "scout-candidate.json"
        proposal.write_text(json.dumps(new_candidate()), encoding="utf-8")
        from foundry.src import orchestrator

        original_reparse = orchestrator._is_reparse_point

        def proposal_is_reparse(path, details=None):
            if Path(path).name == proposal.name:
                return True
            return original_reparse(path, details)

        with mock.patch.object(orchestrator, "_is_reparse_point", side_effect=proposal_is_reparse):
            with self.assertRaisesRegex(CandidateError, "regular file"):
                stage_candidate_file(self.root, proposal, consume=True)
        self.assertTrue(proposal.is_file())

    @unittest.skipIf(os.name == "nt", "FD-relative POSIX claim operations are required")
    def test_posix_scout_claim_never_relocates_an_outside_proposal_during_state_swap(self):
        """A swap before the claim cannot move an outside proposal into a claim."""
        from foundry.src import orchestrator

        state = self.root / "foundry" / "state"
        original_state = self.root / "foundry" / "state-original"
        outside = self.root / "outside-state"
        outside.mkdir()
        proposal = state / "scout-candidate.json"
        proposal.write_text(json.dumps(new_candidate("local-kit")), encoding="utf-8")
        outside_proposal = outside / proposal.name
        outside_proposal.write_text(json.dumps(new_candidate("outside-kit")), encoding="utf-8")
        original_replace = os.replace
        original_rename = os.rename
        swapped = False

        def swap_state() -> None:
            nonlocal swapped
            if swapped:
                return
            swapped = True
            original_replace(state, original_state)
            state.symlink_to(outside, target_is_directory=True)

        def replace_with_swap(source, destination, *args, **kwargs):
            if not swapped and Path(source).name == proposal.name:
                swap_state()
                result = original_replace(source, destination, *args, **kwargs)
                outside_proposal.write_text(json.dumps(new_candidate("newer-outside-kit")), encoding="utf-8")
                return result
            return original_replace(source, destination, *args, **kwargs)

        def rename_with_swap(source, destination, *args, **kwargs):
            if not swapped and source == proposal.name and kwargs.get("src_dir_fd") is not None:
                swap_state()
            return original_rename(source, destination, *args, **kwargs)

        try:
            with mock.patch.object(orchestrator.os, "replace", side_effect=replace_with_swap), mock.patch.object(
                orchestrator.os, "rename", side_effect=rename_with_swap
            ):
                with self.assertRaises((CandidateError, orchestrator.FoundryError)):
                    stage_candidate_file(self.root, proposal, consume=True)
            self.assertEqual(list(outside.glob(".scout-candidate.json.claim-*")), [])
            self.assertTrue(outside_proposal.is_file())
        finally:
            if state.is_symlink():
                state.unlink()
            if original_state.exists():
                os.replace(original_state, state)

    def test_scout_handoff_command_works_from_the_foundry_job_workdir(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repository"
            (root / "foundry" / "state").mkdir(parents=True)
            shutil.copytree(REPO_ROOT / "foundry" / "src", root / "foundry" / "src")
            proposal = root / "foundry" / "state" / "scout-candidate.json"
            proposal.write_text(json.dumps(new_candidate()), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "foundry.src.orchestrator",
                    "stage-candidate",
                    "--candidate",
                    "foundry/state/scout-candidate.json",
                    "--consume",
                ],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(proposal.exists())
            self.assertTrue((root / "foundry" / "state" / "current-candidate.json").is_file())

    def test_new_sku_gate_requires_paid_three_provider_evidence_and_persists_only_pass(self):
        self.write_signals(accepted_signals())
        failed = new_candidate()
        failed["signal_ids"] = ["paid", "release"]
        result = gate_candidate(self.root, failed)
        self.assertFalse(result["passed"])
        self.assertFalse((self.root / "foundry" / "state" / "gate.json").exists())
        passed = gate_candidate(self.root, new_candidate())
        self.assertTrue(passed["passed"])
        persisted = json.loads((self.root / "foundry" / "state" / "gate.json").read_text(encoding="utf-8"))
        self.assertTrue(persisted["passed"])
        self.assertEqual(persisted["generated_date"], chicago_date())

    def test_passed_gate_records_the_exact_ready_signal_state_hash(self):
        self.write_signals(accepted_signals())
        gate = gate_candidate(self.root, new_candidate())
        signal_bytes = (self.root / "foundry" / "state" / "latest-signals.json").read_bytes()
        expected_hash = hashlib.sha256(signal_bytes).hexdigest()
        self.assertEqual(gate.get("signals_sha256"), expected_hash)
        persisted = json.loads((self.root / "foundry" / "state" / "gate.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted.get("signals_sha256"), expected_hash)

    def test_changed_same_day_paid_metrics_invalidate_the_old_gate_before_package(self):
        slug = "hermes-hybrid-operator-kit"
        self.write_packagable_product(slug)
        initial_signals = accepted_signals()
        changed_signals = [
            Signal(
                **{
                    **item.__dict__,
                    "content_sha256": "changed-paid-content",
                    "metrics": {"sales_count": 0},
                }
            )
            if item.signal_id == "paid"
            else item
            for item in initial_signals
        ]
        self.assertEqual(initial_signals[0].signal_id, changed_signals[0].signal_id)
        with mock.patch("foundry.src.orchestrator.collect", return_value=initial_signals):
            collect_signals(self.root, "2026-08-23", config={})
        with mock.patch("foundry.src.orchestrator.chicago_date", return_value="2026-08-23"):
            gate = gate_candidate(self.root, new_candidate(slug), as_of_date="2026-08-23")
        self.assertTrue(gate["passed"])
        old_gate = (self.root / "foundry" / "state" / "gate.json").read_bytes()

        with mock.patch("foundry.src.orchestrator.collect", return_value=changed_signals):
            refreshed = collect_signals(self.root, "2026-08-23", config={})
        self.assertTrue(refreshed["changed"])
        with mock.patch("foundry.src.orchestrator.chicago_date", return_value="2026-08-23"):
            rejected = gate_candidate(self.root, new_candidate(slug), as_of_date="2026-08-23")
        self.assertFalse(rejected["passed"])
        self.assertIn("need_paid_transactional_evidence", rejected["reasons"])

        (self.root / "foundry" / "state" / "gate.json").write_bytes(old_gate)
        with mock.patch("foundry.src.orchestrator.chicago_date", return_value="2026-08-23"):
            with self.assertRaisesRegex(ReleaseError, "signal"):
                package_release(self.root, slug, "1.0.0")
        self.assertFalse((self.root / "dist" / "hermespacks" / f"{slug}-1.0.0.zip").exists())

    def test_update_gate_is_not_blocked_merely_as_a_duplicate(self):
        self.write_signals(accepted_signals())
        mark_successful_release(self.root, "old-candidate", "operator-kit", "1.0.0")
        update = {
            "candidate_type": "update",
            "slug": "operator-kit",
            "signal_ids": ["release"],
            "buyer": "Hermes operator",
            "job_to_be_done": "keep routing compatible with an upstream release",
            "product_delta": "update compatibility checklist",
            "upstream_change": "official release changes a supported route",
        }
        result = gate_candidate(self.root, update)
        self.assertTrue(result["passed"])
        self.assertNotIn("duplicate_slug", result["reasons"])

    def test_unknown_update_slug_cannot_bypass_new_sku_evidence_requirements(self):
        self.write_signals(accepted_signals())
        update = {
            "candidate_type": "update",
            "slug": "unreleased-kit",
            "signal_ids": ["release"],
            "buyer": "Hermes operator",
            "job_to_be_done": "keep routing compatible with an upstream release",
            "product_delta": "update compatibility checklist",
            "upstream_change": "official release changes a supported route",
        }
        result = gate_candidate(self.root, update)
        self.assertFalse(result["passed"])
        self.assertIn("update_requires_existing_successful_release", result["reasons"])
        self.assertIn("need_at_least_3_independent_signals", result["reasons"])
        self.assertIn("need_paid_transactional_evidence", result["reasons"])

    def test_gate_rejects_stale_last_good_signal_evidence_at_a_deterministic_date(self):
        stale_signals = [
            Signal(**{**item.__dict__, "observed_at": "2020-01-01T00:00:00-06:00"})
            for item in accepted_signals()
        ]
        self.write_signals(stale_signals)
        result = gate_candidate(self.root, new_candidate(), as_of_date="2026-08-23")
        self.assertFalse(result["passed"])
        self.assertIn("stale_signal_evidence", result["reasons"])
        self.assertEqual(result["stale_signal_ids"], ["paid", "release", "adoption"])

    def test_update_without_upstream_defect_or_repeated_buyer_pain_fails_closed(self):
        self.write_signals(accepted_signals())
        mark_successful_release(self.root, "old-candidate", "operator-kit", "1.0.0")
        update = {
            "candidate_type": "update",
            "slug": "operator-kit",
            "signal_ids": ["adoption"],
            "buyer": "Hermes operator",
            "job_to_be_done": "keep routing current",
            "product_delta": "minor wording",
        }
        result = gate_candidate(self.root, update)
        self.assertFalse(result["passed"])
        self.assertIn("need_update_trigger", result["reasons"])

    def test_existing_successful_candidate_and_version_is_a_noop_before_packaging(self):
        self.write_signals(accepted_signals())
        gate = gate_candidate(self.root, new_candidate("hermes-hybrid-operator-kit"))
        mark_successful_release(
            self.root,
            gate["candidate_sha256"],
            "hermes-hybrid-operator-kit",
            "1.0.0",
        )
        result = package_release(self.root, "hermes-hybrid-operator-kit", "1.0.0")
        self.assertEqual(result["status"], "noop")
        self.assertFalse((self.root / "dist").exists())

    @POSIX_SNAPSHOT_REQUIRED
    def test_distinct_candidate_cannot_overwrite_an_existing_slug_version(self):
        slug = "hermes-hybrid-operator-kit"
        self.write_packagable_product(slug)
        self.write_signals(accepted_signals())
        first_gate = gate_candidate(self.root, new_candidate(slug))
        package_release(self.root, slug, "1.0.0")
        update = {
            "candidate_type": "update",
            "slug": slug,
            "signal_ids": ["release"],
            "buyer": "Hermes operator",
            "job_to_be_done": "keep routing compatible with an upstream release",
            "product_delta": "update compatibility checklist",
            "upstream_change": "official release changes a supported route",
        }
        second_gate = gate_candidate(self.root, update)
        self.assertNotEqual(first_gate["candidate_sha256"], second_gate["candidate_sha256"])
        with self.assertRaisesRegex(ReleaseError, "slug/version"):
            package_release(self.root, slug, "1.0.0")

    def test_successful_candidate_is_retired_so_the_next_monitor_run_is_silent(self):
        self.write_signals(accepted_signals())
        mark_successful_release(self.root, "old-candidate", "operator-kit", "1.0.0")
        update = {
            "candidate_type": "update",
            "slug": "operator-kit",
            "signal_ids": ["release"],
            "buyer": "Hermes operator",
            "job_to_be_done": "keep routing compatible with an upstream release",
            "product_delta": "update compatibility checklist",
            "upstream_change": "official release changes a supported route",
        }
        staged = stage_candidate(self.root, update)
        gate = gate_current_candidate(self.root)
        self.assertTrue(gate and gate["passed"])
        mark_successful_release(self.root, staged["candidate_sha256"], "operator-kit", "1.0.1")
        self.assertIsNone(gate_current_candidate(self.root))
        self.assertFalse((self.root / "foundry" / "state" / "current-candidate.json").exists())

    def test_unbuilt_queued_candidate_is_never_replaced_only_because_the_date_changed(self):
        with mock.patch("foundry.src.orchestrator.chicago_date", return_value="2026-08-22"):
            stage_candidate(self.root, new_candidate("monday-kit"))
        with mock.patch("foundry.src.orchestrator.chicago_date", return_value="2026-08-23"):
            with self.assertRaisesRegex(CandidateError, "already staged"):
                stage_candidate(self.root, new_candidate("tuesday-kit"))
        staged = json.loads((self.root / "foundry" / "state" / "current-candidate.json").read_text(encoding="utf-8"))
        self.assertEqual(staged["candidate"]["slug"], "monday-kit")

    def test_atomic_proposal_claim_preserves_a_newer_in_place_rewrite(self):
        proposal = self.root / "foundry" / "state" / "scout-candidate.json"
        proposal.write_text(json.dumps(new_candidate("first-kit")), encoding="utf-8")
        from foundry.src import orchestrator

        original_stage = orchestrator._stage_candidate_unlocked

        def stage_then_replace(*args):
            result = original_stage(*args)
            proposal.write_text(json.dumps(new_candidate("second-kit")), encoding="utf-8")
            return result

        with mock.patch.object(orchestrator, "_stage_candidate_unlocked", side_effect=stage_then_replace):
            result = stage_candidate_file(self.root, proposal, consume=True)
        self.assertEqual(result["status"], "staged")
        self.assertTrue(proposal.exists())
        self.assertEqual(json.loads(proposal.read_text(encoding="utf-8"))["slug"], "second-kit")

    @unittest.skipIf(os.name == "nt", "FD-relative POSIX restoration is required")
    def test_failed_claim_restore_never_overwrites_a_newer_proposal(self):
        """A producer racing recovery retains its newer proposal atomically."""
        proposal = self.root / "foundry" / "state" / "scout-candidate.json"
        proposal.write_text(json.dumps(new_candidate("first-kit")), encoding="utf-8")
        from foundry.src import orchestrator

        original_rename = os.rename
        original_link = os.link
        injected = False

        def write_newer(directory_fd: int) -> None:
            descriptor = os.open(
                proposal.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory_fd,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                target.write(json.dumps(new_candidate("newer-kit")))

        def rename_with_newer(source, destination, *args, **kwargs):
            nonlocal injected
            if not injected and source.startswith(".scout-candidate.json.claim-") and destination == proposal.name:
                injected = True
                write_newer(kwargs["dst_dir_fd"])
            return original_rename(source, destination, *args, **kwargs)

        def link_with_newer(source, destination, *args, **kwargs):
            nonlocal injected
            if not injected and source.startswith(".scout-candidate.json.claim-") and destination == proposal.name:
                injected = True
                write_newer(kwargs["dst_dir_fd"])
            return original_link(source, destination, *args, **kwargs)

        with mock.patch.object(orchestrator, "_stage_candidate_unlocked", side_effect=CandidateError("abort")), mock.patch.object(
            orchestrator.os, "rename", side_effect=rename_with_newer
        ), mock.patch.object(orchestrator.os, "link", side_effect=link_with_newer):
            with self.assertRaisesRegex(CandidateError, "abort"):
                stage_candidate_file(self.root, proposal, consume=True)
        self.assertTrue(injected)
        self.assertEqual(json.loads(proposal.read_text(encoding="utf-8"))["slug"], "newer-kit")

    def test_held_descriptor_rewrite_of_claimed_proposal_is_preserved_not_deleted(self):
        proposal = self.root / "foundry" / "state" / "scout-candidate.json"
        proposal.write_text(json.dumps(new_candidate("first-kit")), encoding="utf-8")
        from foundry.src import orchestrator

        original_stage = orchestrator._stage_candidate_unlocked
        with proposal.open("r+", encoding="utf-8") as producer:
            if os.name == "nt":
                # Windows denies the atomic rename while a producer opens the
                # handoff without delete sharing.  Refusing before any claim
                # is the native equivalent of preserving its still-live data.
                with self.assertRaisesRegex(CandidateError, "claimed atomically"):
                    stage_candidate_file(self.root, proposal, consume=True)
                self.assertEqual(json.loads(proposal.read_text(encoding="utf-8"))["slug"], "first-kit")
                return

            def stage_then_rewrite(*args):
                result = original_stage(*args)
                producer.seek(0)
                producer.write(json.dumps(new_candidate("second-kit")))
                producer.truncate()
                producer.flush()
                os.fsync(producer.fileno())
                return result

            with mock.patch.object(orchestrator, "_stage_candidate_unlocked", side_effect=stage_then_rewrite):
                with self.assertRaisesRegex(CandidateError, "changed"):
                    stage_candidate_file(self.root, proposal, consume=True)
        self.assertTrue(proposal.exists())
        self.assertEqual(json.loads(proposal.read_text(encoding="utf-8"))["slug"], "second-kit")

    def test_post_identity_held_fd_rewrite_is_preserved_in_a_durable_receipt(self):
        proposal = self.root / "foundry" / "state" / "scout-candidate.json"
        original_bytes = json.dumps(new_candidate("first-kit")).encode("utf-8")
        replacement_bytes = json.dumps(new_candidate("second-kit")).encode("utf-8")
        proposal.write_bytes(original_bytes)
        from foundry.src import orchestrator

        original_unlink = os.unlink
        with proposal.open("r+b") as producer:
            if os.name == "nt":
                # Native Windows rejects the producer-held rename, so no
                # claim or receipt may be consumed behind that open handle.
                with self.assertRaisesRegex(CandidateError, "claimed atomically"):
                    stage_candidate_file(self.root, proposal, consume=True)
                self.assertEqual(proposal.read_bytes(), original_bytes)
                self.assertEqual(self.scout_receipts(), [])
                return

            def rewrite_then_unlink(path, *args, **kwargs):
                if ".scout-candidate.json.claim-" in Path(path).name:
                    producer.seek(0)
                    producer.write(replacement_bytes)
                    producer.truncate()
                    producer.flush()
                    os.fsync(producer.fileno())
                return original_unlink(path, *args, **kwargs)

            with mock.patch.object(orchestrator.os, "unlink", side_effect=rewrite_then_unlink):
                result = stage_candidate_file(self.root, proposal, consume=True)
        self.assertEqual(result["status"], "staged")
        self.assertFalse(proposal.exists())
        receipts = self.scout_receipts()
        self.assertEqual(len(receipts), 1)
        self.assertTrue(receipts[0].name.startswith(f".scout-receipt-{hashlib.sha256(original_bytes).hexdigest()}-"))
        self.assertEqual(receipts[0].read_bytes(), replacement_bytes)

    def test_full_receipt_quarantine_refuses_consumption_and_preserves_the_proposal(self):
        proposal = self.root / "foundry" / "state" / "scout-candidate.json"
        proposal.write_text(json.dumps(new_candidate()), encoding="utf-8")
        state = self.root / "foundry" / "state"
        for number in range(32):
            (state / f".scout-receipt-{number:064x}.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(CandidateError, "receipt quarantine"):
            stage_candidate_file(self.root, proposal, consume=True)
        self.assertTrue(proposal.is_file())

    def test_receipt_capacity_remains_bounded_during_simultaneous_claims(self):
        """Two fresh handoffs cannot both reserve the final receipt slot."""
        from foundry.src import orchestrator

        receipts = self.root / "foundry" / "state"
        for number in range(31):
            (receipts / f".scout-receipt-{number:064x}.json").write_text("{}", encoding="utf-8")
        claims = []
        for name in ("first", "second"):
            claim = self.root / "foundry" / "state" / f".{name}.claim"
            claim.write_bytes(json.dumps(new_candidate(f"{name}-kit")).encode("utf-8"))
            _, identity = orchestrator._read_regular_candidate_bytes(claim)
            claims.append((claim, identity))

        first_link_entered = threading.Event()
        release_first_link = threading.Event()
        link_lock = threading.Lock()
        calls = 0
        original_link = os.link
        results: list[Path] = []
        errors: list[BaseException] = []

        def delayed_link(source, destination, *args, **kwargs):
            nonlocal calls
            with link_lock:
                calls += 1
                call_number = calls
            if call_number == 1:
                first_link_entered.set()
                release_first_link.wait(timeout=3)
            return original_link(source, destination, *args, **kwargs)

        def reserve(claim, identity):
            try:
                results.append(orchestrator._create_scout_receipt(self.root, claim, identity))
            except BaseException as error:
                errors.append(error)

        with mock.patch.object(orchestrator.os, "link", side_effect=delayed_link):
            first = threading.Thread(target=reserve, args=claims[0])
            second = threading.Thread(target=reserve, args=claims[1])
            first.start()
            self.assertTrue(first_link_entered.wait(timeout=3))
            second.start()
            # The vulnerable check-then-link code lets the second thread reach
            # the link while the first still holds the last apparent slot.
            time.sleep(0.15)
            release_first_link.set()
            first.join(timeout=5)
            second.join(timeout=5)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], CandidateError)
        self.assertEqual(len(self.scout_receipts()), 32)

    @unittest.skipIf(os.name == "nt", "FD-relative receipt operations are required")
    def test_posix_receipt_stays_in_the_verified_state_directory_during_path_swap(self):
        """A state-path replacement cannot redirect a held directory-FD receipt."""
        from foundry.src import orchestrator

        state = self.root / "foundry" / "state"
        original_state = self.root / "foundry" / "state-original"
        outside = self.root / "outside-state"
        outside.mkdir()
        claim = state / ".candidate.claim"
        raw = json.dumps(new_candidate("swap-kit")).encode("utf-8")
        claim.write_bytes(raw)
        # The vulnerable pathname implementation re-resolves both source and
        # destination after the swap.  Populate its alternate lookup path so
        # the test catches a successful redirect rather than just an error.
        os.link(claim, outside / claim.name)
        (outside / "scout-receipts").mkdir()
        _, identity = orchestrator._read_regular_candidate_bytes(claim)
        original_link = os.link
        swapped = False

        def swap_before_link(source, destination, *args, **kwargs):
            nonlocal swapped
            if not swapped:
                os.replace(state, original_state)
                state.symlink_to(outside, target_is_directory=True)
                swapped = True
            return original_link(source, destination, *args, **kwargs)

        try:
            with self.assertRaisesRegex(CandidateError, "state directory changed"):
                with mock.patch.object(orchestrator.os, "link", side_effect=swap_before_link):
                    orchestrator._create_scout_receipt(self.root, claim, identity)
            self.assertEqual(list(outside.rglob("*.json")), [])
            self.assertEqual(len(self.scout_receipts(original_state)), 1)
        finally:
            if state.is_symlink():
                state.unlink()
            if original_state.exists():
                os.replace(original_state, state)

    @POSIX_SNAPSHOT_REQUIRED
    def test_package_writes_verified_manifest_and_truthful_listing_without_publication(self):
        slug = "hermes-hybrid-operator-kit"
        source = self.root / "private-products" / slug
        (source / "tests").mkdir(parents=True)
        (source / "README.md").write_text("Tested local routing kit.\n", encoding="utf-8")
        (source / "VERSION").write_text("1.0.0\n", encoding="utf-8")
        (source / "THIRD_PARTY_NOTICES.md").write_text("No third-party code.\n", encoding="utf-8")
        (source / "tests" / "test_kit.py").write_text("import sys\nraise SystemExit(0)\n", encoding="utf-8")
        (source / "scripts").mkdir()
        (source / "scripts" / "install.sh").write_text("#!/bin/sh\necho staged\n", encoding="utf-8")
        products = self.root / "products"
        products.mkdir()
        (products / f"{slug}.json").write_text(json.dumps({
            "slug": slug,
            "name": "Hermes Hybrid Operator Kit",
            "version": "1.0.0",
            "price_usd": 12,
            "checkout_status": "pending_payout_onboarding",
            "publication_status": "not_published",
            "verified_revenue_usd": 0,
            "update_policy": "Current release plus 30 days of corrections.",
            "verification_status": "pending_release_verification",
            "non_affiliation": "Independent product with no relationship to Nous Research or OpenAI.",
        }), encoding="utf-8")
        atomic_write_json(
            self.root / "foundry" / "state" / "platform-verification.json",
            {
                "source_revision": source_tree_revision(source),
                "windows_native_compatibility": {"failures": 0, "status": "passed"},
            },
        )
        self.write_signals(accepted_signals())
        gate_candidate(self.root, new_candidate(slug))
        result = package_release(self.root, slug, "1.0.0")
        self.assertEqual(result["status"], "verified")
        manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        listing = json.loads(Path(result["listing_path"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["archive_sha256"], result["archive_sha256"])
        self.assertEqual(listing["verified_revenue_usd"], 0)
        self.assertEqual(listing["publication_status"], "not_published")
        self.assertEqual(listing["checkout_status"], "pending_payout_onboarding")
        self.assertIn("OpenAI", listing["non_affiliation"])
        self.assertIn("Nous Research", listing["non_affiliation"])
        self.assertEqual(
            manifest["tested_scope"],
            "Mac product suite; Windows native compatibility canary; no Windows live Hermes readiness.",
        )
        self.assertEqual(
            manifest["tested_platforms"],
            ["Mac product suite", "Windows native compatibility canary (no Windows live Hermes readiness)"],
        )
        self.assertEqual(listing["tested_scope"], manifest["tested_scope"])
        with zipfile.ZipFile(result["archive_path"]) as archive:
            self.assertEqual(archive.getinfo("scripts/install.sh").external_attr >> 16, 0o100755)
            with tempfile.TemporaryDirectory() as extracted_directory:
                extracted = Path(extracted_directory)
                archive.extractall(extracted)
                (extracted / "RELEASE-MANIFEST.json").unlink()
                self.assertEqual(source_tree_revision(extracted), manifest["source_revision"])

    def test_source_tree_revision_has_unambiguous_file_boundaries(self):
        first = self.root / "private-products" / "first"
        second = self.root / "private-products" / "second"
        first.mkdir(parents=True)
        second.mkdir(parents=True)
        # Vulnerable NUL-delimited framing produces the same byte stream:
        # a\0x\0b\0y\0 for both trees.
        (first / "a").write_bytes(b"x\0b\0y")
        (second / "a").write_bytes(b"x")
        (second / "b").write_bytes(b"y")
        self.assertNotEqual(source_tree_revision(first), source_tree_revision(second))

    def test_source_tree_revision_uses_relative_posix_order_not_host_path_order(self):
        source = self.root / "private-products" / "ordered-kit"
        source.mkdir(parents=True)
        (source / "Z").write_text("uppercase\n", encoding="utf-8")
        (source / "a").write_text("lowercase\n", encoding="utf-8")
        canonical = source_tree_revision(source)

        def reverse_path_order(left, right):
            return str(left) > str(right)

        with mock.patch.object(type(source), "__lt__", new=reverse_path_order):
            self.assertEqual(source_tree_revision(source), canonical)

    def test_source_tree_revision_rejects_windows_style_reparse_root_directory_and_file(self):
        """Direct identity helpers must not traverse a Windows junction either."""
        source = self.root / "private-products" / "reparse-kit"
        nested = source / "nested"
        nested.mkdir(parents=True)
        payload = nested / "README.md"
        payload.write_text("safe bytes\n", encoding="utf-8")
        from foundry.src import orchestrator

        for marked in (source, nested, payload):
            with self.subTest(marked=marked.relative_to(self.root).as_posix()):
                with mock.patch.object(
                    orchestrator,
                    "_is_reparse_point",
                    side_effect=lambda path, _details=None: Path(path).resolve() == marked.resolve(),
                    create=True,
                ):
                    with self.assertRaisesRegex(ReleaseError, "link or reparse"):
                        source_tree_revision(source)

    @POSIX_SNAPSHOT_REQUIRED
    def test_private_source_reparse_point_is_rejected_before_a_test_can_run(self):
        slug = "hermes-hybrid-operator-kit"
        sentinel = self.root / "reparse-source-test-ran"
        source = self.write_packagable_product(
            slug,
            test_body=(
                "from pathlib import Path\n"
                f"Path({str(sentinel)!r}).write_text('ran', encoding='utf-8')\n"
            ),
        )
        self.write_signals(accepted_signals())
        gate_candidate(self.root, new_candidate(slug))
        from foundry.src import orchestrator

        with mock.patch.object(
            orchestrator,
            "_is_reparse_point",
            side_effect=lambda path, _details=None: Path(path).resolve() == source.resolve(),
            create=True,
        ):
            with self.assertRaisesRegex(ReleaseError, "reparse"):
                package_release(self.root, slug, "1.0.0")
        self.assertFalse(sentinel.exists())

    @POSIX_SNAPSHOT_REQUIRED
    def test_package_audits_a_snapshot_before_a_symlinked_test_can_execute(self):
        slug = "hermes-hybrid-operator-kit"
        source = self.write_packagable_product(slug)
        sentinel = self.root / "source-test-ran"
        target = self.root / "outside-test.py"
        target.write_text(
            f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('ran')\n",
            encoding="utf-8",
        )
        test_path = source / "tests" / "test_kit.py"
        test_path.unlink()
        test_path.symlink_to(target)
        self.write_signals(accepted_signals())
        gate_candidate(self.root, new_candidate(slug))
        with self.assertRaises(ReleaseError):
            package_release(self.root, slug, "1.0.0")
        self.assertFalse(sentinel.exists())
        self.assertFalse((self.root / "dist" / "hermespacks" / f"{slug}-1.0.0.zip").exists())

    def test_unsupported_snapshot_host_fails_closed_before_private_source_execution(self):
        """Windows compatibility runs never become an authority packager."""
        slug = "hermes-hybrid-operator-kit"
        sentinel = self.root / "unsupported-host-test-ran"
        self.write_packagable_product(
            slug,
            test_body=(
                "from pathlib import Path\n"
                f"Path({str(sentinel)!r}).write_text('ran', encoding='utf-8')\n"
            ),
        )
        self.write_signals(accepted_signals())
        gate_candidate(self.root, new_candidate(slug))
        from foundry.src import orchestrator

        with mock.patch.object(orchestrator, "_supports_secure_snapshot_host", return_value=False, create=True):
            with self.assertRaisesRegex(ReleaseError, "secure no-follow snapshot host"):
                package_release(self.root, slug, "1.0.0")
        self.assertFalse(sentinel.exists())
        self.assertFalse((self.root / "dist" / "hermespacks" / f"{slug}-1.0.0.zip").exists())

    @POSIX_SNAPSHOT_REQUIRED
    def test_package_rejects_linked_private_products_ancestry_before_execution(self):
        slug = "hermes-hybrid-operator-kit"
        source = self.write_packagable_product(slug)
        sentinel = self.root / "linked-parent-test-ran"
        (source / "tests" / "test_kit.py").write_text(
            f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('ran')\n",
            encoding="utf-8",
        )
        private_products = self.root / "private-products"
        relocated = self.root / "relocated-private-products"
        private_products.rename(relocated)
        private_products.symlink_to(relocated, target_is_directory=True)
        self.write_signals(accepted_signals())
        gate_candidate(self.root, new_candidate(slug))
        with self.assertRaises(ReleaseError):
            package_release(self.root, slug, "1.0.0")
        self.assertFalse(sentinel.exists())

    @POSIX_SNAPSHOT_REQUIRED
    def test_snapshot_mutation_during_product_tests_fails_before_any_release_output(self):
        slug = "hermes-hybrid-operator-kit"
        test_body = (
            "from pathlib import Path\n"
            "Path(__file__).parents[1].joinpath('README.md').write_text('mutated')\n"
        )
        self.write_packagable_product(slug, test_body=test_body)
        self.write_signals(accepted_signals())
        gate_candidate(self.root, new_candidate(slug))
        with self.assertRaisesRegex(ReleaseError, "snapshot changed"):
            package_release(self.root, slug, "1.0.0")
        self.assertFalse((self.root / "dist" / "hermespacks" / f"{slug}-1.0.0.zip").exists())

    @POSIX_SNAPSHOT_REQUIRED
    def test_release_rejects_a_build_race_instead_of_misbinding_source_revision_and_windows_claim(self):
        slug = "hermes-hybrid-operator-kit"
        source = self.write_packagable_product(slug)
        atomic_write_json(
            self.root / "foundry" / "state" / "platform-verification.json",
            {
                "source_revision": source_tree_revision(source),
                "windows_native_compatibility": {"failures": 0, "status": "passed"},
            },
        )
        self.write_signals(accepted_signals())
        gate_candidate(self.root, new_candidate(slug))
        from foundry.src import orchestrator

        original_build = orchestrator.build_release

        def mutate_then_build(snapshot_source, output_zip, metadata, repo_root=None):
            (Path(snapshot_source) / "README.md").write_text("raced\n", encoding="utf-8")
            return original_build(snapshot_source, output_zip, metadata, repo_root=repo_root)

        with mock.patch.object(orchestrator, "build_release", side_effect=mutate_then_build):
            with self.assertRaisesRegex(ReleaseError, "snapshot changed"):
                package_release(self.root, slug, "1.0.0")
        self.assertFalse((self.root / "dist" / "hermespacks" / f"{slug}-1.0.0.zip").exists())

    @POSIX_SNAPSHOT_REQUIRED
    def test_generated_release_and_listing_copy_are_claim_audited_before_output(self):
        cases = (
            ("best-seller", {"name": "Best-selling operator kit"}, "unsupported_bestseller"),
            ("social-proof", {"name": "Built for hundreds of developers"}, "unsupported_social_proof"),
            ("lifetime", {"update_policy": "Lifetime updates."}, "unsupported_lifetime"),
        )
        for suffix, overrides, finding in cases:
            with self.subTest(finding=finding):
                slug = f"claim-{suffix}"
                self.write_packagable_product(slug, metadata_overrides=overrides)
                self.write_signals(accepted_signals())
                gate_candidate(self.root, new_candidate(slug))
                with self.assertRaisesRegex(ReleaseError, finding):
                    package_release(self.root, slug, "1.0.0")
                self.assertFalse((self.root / "dist" / "hermespacks" / f"{slug}-1.0.0.zip").exists())

    @POSIX_SNAPSHOT_REQUIRED
    def test_all_public_metadata_copy_fields_are_claim_audited_before_output(self):
        cases = (
            ("description-bestseller", {"description": "Best-selling operator kit."}, "unsupported_bestseller"),
            ("extra-social", {"public_copy": "Built for hundreds of developers."}, "unsupported_social_proof"),
            ("extra-lifetime", {"support_copy": "Lifetime updates."}, "unsupported_lifetime"),
        )
        for suffix, overrides, finding in cases:
            with self.subTest(finding=finding):
                slug = f"metadata-{suffix}"
                self.write_packagable_product(slug, metadata_overrides=overrides)
                self.write_signals(accepted_signals())
                gate_candidate(self.root, new_candidate(slug))
                with self.assertRaisesRegex(ReleaseError, finding):
                    package_release(self.root, slug, "1.0.0")
                self.assertFalse((self.root / "dist" / "hermespacks" / f"{slug}-1.0.0.zip").exists())

    @POSIX_SNAPSHOT_REQUIRED
    def test_stale_windows_platform_evidence_is_excluded_from_release_scope(self):
        slug = "hermes-hybrid-operator-kit"
        source = self.root / "private-products" / slug
        (source / "tests").mkdir(parents=True)
        (source / "scripts").mkdir()
        (source / "README.md").write_text("Tested local routing kit.\n", encoding="utf-8")
        (source / "VERSION").write_text("1.0.0\n", encoding="utf-8")
        (source / "THIRD_PARTY_NOTICES.md").write_text("No third-party code.\n", encoding="utf-8")
        (source / "tests" / "test_kit.py").write_text("import sys\nraise SystemExit(0)\n", encoding="utf-8")
        (source / "scripts" / "install.sh").write_text("#!/bin/sh\necho staged\n", encoding="utf-8")
        products = self.root / "products"
        products.mkdir()
        (products / f"{slug}.json").write_text(json.dumps({
            "slug": slug,
            "name": "Hermes Hybrid Operator Kit",
            "version": "1.0.0",
            "price_usd": 12,
            "checkout_status": "pending_payout_onboarding",
            "publication_status": "not_published",
            "verified_revenue_usd": 0,
            "update_policy": "Current release plus 30 days of corrections.",
            "verification_status": "pending_release_verification",
            "non_affiliation": "Independent product with no relationship to Nous Research or OpenAI.",
        }), encoding="utf-8")
        atomic_write_json(
            self.root / "foundry" / "state" / "platform-verification.json",
            {
                "source_revision": "tree-sha256:stale",
                "windows_native_compatibility": {"failures": 0, "status": "passed"},
            },
        )
        self.write_signals(accepted_signals())
        gate_candidate(self.root, new_candidate(slug))
        result = package_release(self.root, slug, "1.0.0")
        manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["tested_platforms"], ["Mac product suite"])
        self.assertEqual(
            manifest["tested_scope"],
            "Mac product suite; no Windows native compatibility evidence for this source tree; no Windows live Hermes readiness.",
        )

    def test_nonzero_windows_platform_failures_are_excluded_even_for_matching_source(self):
        source = self.root / "private-products" / "operator-kit"
        source.mkdir(parents=True)
        (source / "README.md").write_text("source\n", encoding="utf-8")
        atomic_write_json(
            self.root / "foundry" / "state" / "platform-verification.json",
            {
                "source_revision": source_tree_revision(source),
                "windows_native_compatibility": {"failures": 1, "status": "passed"},
            },
        )
        from foundry.src.orchestrator import _tested_platform_scope_unlocked

        platforms, scope = _tested_platform_scope_unlocked(self.root, source_tree_revision(source))
        self.assertEqual(platforms, ["Mac product suite"])
        self.assertIn("no Windows native compatibility evidence", scope)

    def test_private_source_tree_revision_changes_when_paid_source_changes(self):
        source = self.root / "private-products" / "operator-kit"
        source.mkdir(parents=True)
        readme = source / "README.md"
        readme.write_text("first\n", encoding="utf-8")
        first = source_tree_revision(source)
        readme.write_text("second\n", encoding="utf-8")
        second = source_tree_revision(source)
        self.assertTrue(first.startswith("tree-sha256:"))
        self.assertNotEqual(first, second)

    def test_latest_signals_reads_only_normalized_state(self):
        self.write_signals(accepted_signals())
        self.assertEqual([item.signal_id for item in latest_signals(self.root)], ["paid", "release", "adoption"])


@unittest.skipUnless(os.name == "nt", "requires native Windows state-path semantics")
class WindowsStateBoundaryTests(unittest.TestCase):
    """Windows deliberately never enters the mutable Foundry control plane."""

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / "foundry").mkdir()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_state_lock_fails_before_creating_a_missing_state_directory(self):
        from foundry.src import orchestrator

        state = self.root / "foundry" / "state"
        with self.assertRaisesRegex(orchestrator.FoundryError, "POSIX no-follow directory-FD host"):
            with orchestrator.state_lock(self.root):
                pass
        self.assertFalse(state.exists())

    def test_collect_fails_before_fetch_or_state_write(self):
        from foundry.src import orchestrator

        with mock.patch.object(orchestrator, "collect") as fetched:
            with self.assertRaisesRegex(orchestrator.FoundryError, "POSIX no-follow directory-FD host"):
                collect_signals(self.root, "2026-08-23", config={})
        fetched.assert_not_called()
        self.assertFalse((self.root / "foundry" / "state").exists())

    def test_consume_leaves_the_approved_proposal_and_state_untouched(self):
        from foundry.src import orchestrator

        state = self.root / "foundry" / "state"
        state.mkdir()
        proposal = state / "scout-candidate.json"
        proposal.write_text(json.dumps(new_candidate("windows-boundary-kit")), encoding="utf-8")
        original = proposal.read_bytes()
        with self.assertRaisesRegex(orchestrator.FoundryError, "POSIX no-follow directory-FD host"):
            stage_candidate_file(self.root, proposal, consume=True)
        self.assertEqual(proposal.read_bytes(), original)
        self.assertFalse((state / "current-candidate.json").exists())
        self.assertFalse((state / ".lock").exists())
        self.assertEqual(list(state.glob(".scout-receipt-*.json")), [])

    def test_state_junction_and_two_link_lock_are_untouched(self):
        from foundry.src import orchestrator

        state = self.root / "foundry" / "state"
        outside = self.root / "outside"
        outside.mkdir()
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", f'mklink /J "{state}" "{outside}"'],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        try:
            with self.assertRaisesRegex(orchestrator.FoundryError, "POSIX no-follow directory-FD host"):
                with orchestrator.state_lock(self.root):
                    pass
            self.assertFalse((outside / ".lock").exists())
        finally:
            os.rmdir(state)

        state.mkdir()
        lock = state / ".lock"
        second_link = self.root / "second-lock-link"
        lock.write_bytes(b"retained")
        os.link(lock, second_link)
        try:
            with self.assertRaisesRegex(orchestrator.FoundryError, "POSIX no-follow directory-FD host"):
                with orchestrator.state_lock(self.root):
                    pass
            self.assertEqual(lock.read_bytes(), b"retained")
            self.assertEqual(second_link.read_bytes(), b"retained")
            self.assertGreaterEqual(lock.stat().st_nlink, 2)
        finally:
            second_link.unlink()


if __name__ == "__main__":
    unittest.main()
