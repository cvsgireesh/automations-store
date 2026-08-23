# Hermes Product Foundry

Use this skill only for evidence-gated Foundry work in this repository.

## Required boundaries

- A new SKU needs three independent cited signals, including a paid comparable
  with visible transactional evidence. An update needs a cited upstream change,
  reproducible defect, or repeated buyer pain; do not reject an update merely
  because the product slug already exists.
- Every public factual claim needs a source or fresh verification artifact.
  Verified revenue is `$0` unless a non-owner sale has been observed.
- The Scout reads only normalized signals and the release ledger, stages at
  most one candidate, and never builds. The Builder requires fresh
  `gate.json` with `passed: true` before writing.
- Builder changes are limited to ignored private product source and supported
  public factual product metadata. Test the exact product delta.
- Packaging is local preparation only. It may create an ignored archive,
  checksum manifest, and truthful listing copy; it must never publish.
- A package always records the just-run Mac product suite. Include a Windows
  native compatibility canary only when the ignored
  `foundry/state/platform-verification.json` has the exact current
  `source_revision` plus `windows_native_compatibility` with `status` set to
  `passed` and integer `failures` set to `0`. Never claim Windows live Hermes
  readiness from that evidence.

## Forbidden actions

Do not access, retain, or request credentials, financial identity, payout
data, buyer messages, or payment settings. Do not publish, upload, submit a
listing, send buyer messages, copy third-party code or skills, run `git push`,
or make arbitrary public claims. Never fabricate testimonials, customers,
sales, ratings, discounts, benchmarks, affiliations, revenue, or lifetime
support. A third-party component cannot enter a paid archive without a
compatible license and a matching `THIRD_PARTY_NOTICES.md` entry.

## Operations

Use `python3 -m foundry.src.orchestrator` for deterministic state changes.
From the scheduled `<repo>/foundry` workdir, invoke it as
`(cd .. && python3 -m foundry.src.orchestrator ...)` so the repository package
is importable. Only the Darwin/macOS authority host may mutate Foundry state or
package a release. Its transaction retains a no-follow state directory
descriptor for every state-file read, write, and unlink and validates the
pathname identity again on exit. `home-windows`, WSL, and other Linux hosts are
limited to pure product compatibility and fail closed before state mutation or
packaging. No-op runs are successful and silent. Keep heavy local-model work
serialized on the Mac. Gumroad payout onboarding and final publication require
the account owner and action-time confirmation, so they are never automated.
