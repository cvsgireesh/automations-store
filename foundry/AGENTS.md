# Foundry agent boundaries

This directory is the working directory for Hermes Product Foundry jobs.

- Treat `foundry/state/`, `foundry/runs/`, `dist/`, and `private-products/` as
  local/ignored operational data. Do not commit paid archives or paid source.
- The Scout may read only normalized signals and the release ledger, stage at
  most one candidate, and never build. The Builder must require a fresh passed
  `gate.json` and may write only ignored private product source or supported
  public factual product metadata.
- A new SKU requires three independent signals including paid transactional
  evidence. An update requires an upstream change, reproducible defect, or
  repeated buyer pain; a duplicate product slug alone is not an update block.
- Public claims require a source or fresh verification artifact. Verified
  revenue remains `$0` until a non-owner sale occurs. Never fabricate
  testimonials, customers, sales, ratings, discounts, benchmarks,
  affiliations, revenue, or lifetime support.
- Do not access credentials, financial identity, payouts, buyer messages, or
  payment settings. Do not publish, upload, submit listings, send buyer
  messages, copy third-party work, run `git push`, or make arbitrary public
  claims. Do not put third-party code or skills in paid archives unless the
  license is compatible and `THIRD_PARTY_NOTICES.md` names it.
- Daily no-op runs are successful and silent. Keep heavy local model work
  serialized on the Mac. Only the Darwin/macOS control plane may mutate
  Foundry state or package a release. It binds every locked state-file read,
  write, and unlink to a retained no-follow directory descriptor and rejects a
  changed state pathname when the transaction exits. `home-windows`, WSL, and
  other Linux hosts are pure product-compatibility targets and fail closed
  before Foundry state mutation or packaging. This covers cooperating Foundry
  processes and pathname replacement, not arbitrary same-account writes made
  through an already-authorized descriptor.
- Payout onboarding and final publication require the account owner's identity
  and action-time confirmation; neither is automated.
- For a Windows compatibility claim, use only sanitized ignored
  `foundry/state/platform-verification.json` evidence whose `source_revision`
  exactly matches the current paid source and whose Windows result is
  `status: passed` with `failures: 0`. This never establishes Windows live
  Hermes readiness.
