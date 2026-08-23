"""Tests for deterministic, fail-closed Foundry orchestration."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

from foundry.src.models import Signal
from foundry.src.orchestrator import (
    CandidateError,
    CollectionError,
    atomic_write_json,
    chicago_date,
    collect_signals,
    gate_candidate,
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


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / "foundry" / "state").mkdir(parents=True)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def write_signals(self, signals: list[Signal]) -> None:
        atomic_write_json(
            self.root / "foundry" / "state" / "latest-signals.json",
            {"signals": [
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
            ]},
        )

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

    def test_unchanged_collection_is_a_silent_noop_with_stable_last_good_record(self):
        config = json.loads((REPO_ROOT / "foundry" / "config.json").read_text(encoding="utf-8"))
        first = collect_signals(self.root, "2026-08-23", config=config, fixture_dir=FIXTURES)
        before = (self.root / "foundry" / "state" / "latest-signals.json").read_bytes()
        second = collect_signals(self.root, "2026-08-24", config=config, fixture_dir=FIXTURES)
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        self.assertEqual((self.root / "foundry" / "state" / "latest-signals.json").read_bytes(), before)

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
        proposal.write_text(json.dumps(new_candidate()), encoding="utf-8")
        result = stage_candidate_file(self.root, proposal, consume=True)
        self.assertEqual(result["status"], "staged")
        self.assertFalse(proposal.exists())
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

    def test_scout_handoff_command_works_from_the_foundry_job_workdir(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repository"
            (root / "foundry" / "state").mkdir(parents=True)
            shutil.copytree(REPO_ROOT / "foundry" / "src", root / "foundry" / "src")
            proposal = root / "foundry" / "state" / "scout-candidate.json"
            proposal.write_text(json.dumps(new_candidate()), encoding="utf-8")
            completed = subprocess.run(
                [
                    "/bin/sh",
                    "-c",
                    f'cd .. && "{sys.executable}" -m foundry.src.orchestrator '
                    "stage-candidate --candidate foundry/state/scout-candidate.json --consume",
                ],
                cwd=root / "foundry",
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

    def test_update_without_upstream_defect_or_repeated_buyer_pain_fails_closed(self):
        self.write_signals(accepted_signals())
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


if __name__ == "__main__":
    unittest.main()
