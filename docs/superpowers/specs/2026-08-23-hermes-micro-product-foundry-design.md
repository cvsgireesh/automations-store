# Hermes Micro-Product Foundry — Design

Date: 2026-08-23
Status: approved in advance by the owner ("no questions", "all permissions granted")

## Objective

Turn the existing HermesPacks storefront and Hermes automation stack into a low-touch digital-product business that is grounded in visible buyer behavior. The system should research every day, improve or build only when evidence clears a strict gate, package a tested digital product, and prepare a truthful storefront release. It must not flood marketplaces, fabricate proof, republish third-party work, or promise income.

The first product is **Hermes Hybrid Operator Kit**: a tested setup and operations bundle for routing low-risk Hermes work to local Ollama/llama.cpp models and consequential work to a Codex authority lane.

Launch price: **$12**. Standard price after initial validation: **$15**.

## Evidence and positioning

- Gumroad currently returns 22 results for `Hermes Agent`.
- A directly comparable Hermes workflow playbook visibly shows 119 sales at $14.67+.
- A generic free Hermes guide visibly shows 618 downloads, demonstrating interest but making another generic PDF a poor offer.
- Gumroad searches for `Hermes Codex local models` and `Hermes Ollama` show almost no directly comparable paid operational product.
- Dennis already owns and runs the underlying architecture in `hermes-hybrid-agent-router`, with a live Mac deployment, local models, scheduled jobs, and a public technical article.

The paid value must therefore be verified operational convenience—not copied skills, generic prompts, or information already available in the free repository.

## Approaches considered

### A. Generate a new generic digital product every day

Rejected. It optimizes listing volume rather than buyer value, produces weak quality, creates license and marketplace-policy risk, and would turn the storefront into AI-generated clutter.

### B. Build low-cost printable generators for Etsy

Viable follow-on. Current Etsy listings validate demand for generic swatch-chart and name-tracing utilities around $2–$5. They are highly passive once built. However, there is no verified Etsy seller/payout path in the current environment, and this would require a second brand and distribution surface.

### C. Build one differentiated Hermes operator product and compound it

Selected. It uses existing expertise, code, audience, store, Gumroad account, two-machine test environment, and live Hermes infrastructure. Daily automation researches product gaps and upstream changes; it ships updates only when they improve a real buyer outcome.

## Architecture

The public repository remains the control plane and storefront. Paid artifacts stay in the existing ignored `dist/` tree and are never committed.

1. **Signal collector (deterministic/no-agent)**
   - Fetches a small allowlist of public sources: official Hermes releases/docs/issues, Gumroad search/product pages, the existing repository state, and the product test matrix.
   - Saves normalized records with URL, observed date, source type, visible metric, and content hash.
   - Never logs into marketplaces, posts, messages, or scrapes unbounded result sets.

2. **Daily local scout (Hermes, local model)**
   - Reads only normalized signals and the product ledger.
   - Classifies findings as buyer pain, upstream compatibility change, competitor movement, or noise.
   - Produces at most one candidate. A no-candidate day is normal.

3. **Evidence gate (deterministic)**
   - Requires a directly visited source, a dated observation, a clear buyer/job, and a concrete product delta.
   - A new SKU requires at least three independent signals, including one paid comparable with visible sales/ratings or equivalent transactional evidence.
   - An update requires an upstream compatibility change, reproducible defect, or repeated buyer pain.
   - Rejects duplicated ideas, unsupported revenue claims, legal/compliance advice, credential handling, copyrighted inputs, and generic prompt-only bundles.

4. **Builder (Hermes authority lane)**
   - Runs only for a gated candidate.
   - May edit the private product source and public product metadata, but never credentials or payment settings.
   - Produces scripts/templates, documentation, a manifest, license/third-party notices, checksums, and automated tests.

5. **Verifier and packager (deterministic)**
   - Runs unit/smoke tests, secret scans, placeholder scans, link checks, archive-content checks, and reproducible ZIP packaging.
   - Writes a signed-style release manifest with SHA-256 hashes and test evidence.
   - A failed gate leaves the candidate staged and does not change the public catalog.

6. **Storefront publisher**
   - Updates only factual copy and verified product metadata in the public GitHub Pages repository.
   - Git push deploys GitHub Pages after tests pass.
   - Gumroad upload/publish is supported by a prepared release bundle, listing copy, and image assets. It remains blocked until the existing seller account has a payout method; financial identity and payout data must be entered by the account owner.

7. **Reporter**
   - Sends a short Telegram summary only for a meaningful new signal, gated build, failed verification, or published update.
   - Silence is success on ordinary no-op days.

## First product contents

The Hermes Hybrid Operator Kit will contain:

- sanitized local-first configuration presets and a configuration generator;
- preflight diagnostics with actionable output;
- local/authority routing worksheet and policy examples;
- canary tests for model availability, tool behavior, and fail-closed routing;
- four failure drills: local lane unavailable, authority lane unavailable, forbidden fallback, and secret-exposure check;
- macOS/Linux shell and Windows PowerShell entry points where they can be tested honestly;
- a troubleshooting decision tree, release manifest, checksums, license, and third-party notices;
- tested installation and rollback instructions;
- a clearly bounded update policy: current release plus 30 days, not lifetime support.

The free `hermes-hybrid-agent-router` repository remains the architecture reference. The paid kit sells tested tooling, diagnostics, and packaging convenience.

## Storefront corrections

The current live site must be narrowed before promotion:

- remove fabricated testimonials and unsupported "hundreds of developers", "best seller", "enterprise-quality", and lifetime-update claims;
- remove or hide the seven broad packs until their provenance, licenses, and tests are audited;
- present one verified product, the free architecture repository, the test scope, the exact update promise, and the payment status truthfully;
- never expose Gumroad or GitHub issue flows as working checkout when they are not passive checkout;
- keep the Nous Research non-affiliation notice.

## Scheduling

Use the existing Hermes scheduler and persistent Mac gateway; do not install another cron system.

- 01:10 daily — deterministic signal collection, no-agent mode.
- 01:25 daily — local scout using a registered local model.
- 02:00 Sunday — authority review/build for the strongest gated candidate, if any.
- 02:45 Sunday — deterministic verification/package/storefront preparation.
- Existing Quiet Sentinel remains the health monitor.

Heavy local models run serially. The Windows host is a compatibility-test target, not the primary compute host: it has 8 GB RAM and a 4 GB 940MX, while the Mac has 128 GB unified memory and persistent local 35B/120B model services.

## Failure handling

- Source unavailable: preserve previous observation and mark stale; never infer a new metric.
- Local model unavailable: skip the scout and notify once; never silently spend through a hosted fallback.
- Authority unavailable: keep the candidate queued; do not build or publish.
- Test failure: retain artifacts under a failed run directory; catalog remains unchanged.
- Marketplace/account blocked: produce the complete release bundle and truthful waiting status; do not request or store financial credentials.
- Upstream model/config drift: fail preflight and point to the exact registered/installed mismatch.

## Testing and acceptance

The system is complete when:

1. All deterministic tests pass locally.
2. A full dry run produces normalized signals, one correctly scored candidate or an explicit no-op, a verified ZIP, hashes, listing copy, and a run report.
3. Removing a required signal causes the evidence gate to fail.
4. Inserting a fake testimonial/revenue claim causes the copy audit to fail.
5. Inserting a secret-like token causes packaging to fail.
6. Re-running the same date is idempotent and does not duplicate a release or cron job.
7. The live storefront contains only supported claims and no dead checkout links.
8. Hermes cron jobs are pinned to explicit providers/models and appear healthy in `hermes cron list`.

## Success metric

The first business validation is not "passive income." It is one truthful paid SKU, a working checkout after payout onboarding, and the first non-owner sale. Until then, the system reports **$0 verified revenue** and does not fabricate social proof.
