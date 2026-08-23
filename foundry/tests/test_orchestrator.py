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

        original_stage = orchestrator.stage_candidate

        def stage_then_replace(root, payload):
            result = original_stage(root, payload)
            proposal.write_text(json.dumps(new_candidate("second-kit")), encoding="utf-8")
            return result

        with mock.patch.object(orchestrator, "stage_candidate", side_effect=stage_then_replace):
            result = stage_candidate_file(self.root, proposal, consume=True)
        self.assertEqual(result["status"], "staged")
        self.assertTrue(proposal.exists())
        self.assertEqual(json.loads(proposal.read_text(encoding="utf-8"))["slug"], "second-kit")

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
