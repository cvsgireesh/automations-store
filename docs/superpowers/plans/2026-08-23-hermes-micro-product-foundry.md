# Hermes Micro-Product Foundry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and deploy an evidence-gated Hermes pipeline that maintains one tested $12 digital product, packages it reproducibly, and exposes only truthful storefront claims.

**Architecture:** A standard-library Python control plane collects bounded public signals, evaluates deterministic evidence gates, audits copy and files, and creates reproducible release bundles. Hermes runs a local-model scout only when normalized signals change and an authority-model builder only for a gated candidate; ignored private product sources feed a public single-product GitHub Pages storefront.

**Tech Stack:** Python 3.11 standard library, `unittest`, Hermes Agent 0.20.5 native cron, Ollama/llama.cpp, OpenAI Codex provider, static HTML/CSS/JavaScript, POSIX shell, PowerShell, GitHub Pages, Gumroad.

## Global Constraints

- Paid sources and ZIP files must remain under ignored `private-products/` and `dist/`; never commit them.
- All public claims require a source or fresh verification artifact; verified revenue remains `$0` until a non-owner sale is observed.
- A new SKU needs three independent signals, including one paid comparable with visible transactional evidence.
- Never fabricate testimonials, customers, sales, ratings, discounts, benchmarks, affiliations, or lifetime support.
- No third-party skill may enter a paid archive without a compatible license and `THIRD_PARTY_NOTICES.md` entry.
- Daily no-op runs are successful and silent.
- Heavy local model work is serialized on the Mac; `home-windows` is a compatibility target, not the primary inference host.
- Gumroad payout onboarding and final product publication are not automated because they require the account owner's financial identity and an action-time confirmation.

---

## File map

- `foundry/config.json` — allowlisted sources, thresholds, product identity, and schedules.
- `foundry/src/models.py` — typed signal/candidate/gate records and JSON I/O.
- `foundry/src/collector.py` — bounded HTTP collection and Gumroad/GitHub parsers.
- `foundry/src/gate.py` — deterministic evidence and deduplication rules.
- `foundry/src/audits.py` — secret, prohibited-claim, license, placeholder, and archive audits.
- `foundry/src/packager.py` — deterministic ZIP and release manifest creation.
- `foundry/src/orchestrator.py` — idempotent CLI joining collection, gating, verification, and reporting.
- `foundry/prompts/SCOUT.md` — local scout contract.
- `foundry/prompts/BUILDER.md` — authority builder contract.
- `foundry/skill/SKILL.md` — Hermes procedural skill.
- `foundry/install_jobs.py` — idempotent skill/script deployment and Hermes cron reconciliation.
- `foundry/scripts/*.py` — small scheduler entry points copied to `~/.hermes/scripts/`.
- `foundry/tests/` — standard-library unit/integration tests and bounded fixtures.
- `products/hermes-hybrid-operator-kit.json` — public factual catalog metadata.
- `private-products/hermes-hybrid-operator-kit/` — ignored paid source tree.
- `index.html`, `order.html`, `css/style.css`, `js/main.js` — honest single-product storefront.
- `proof.html` — public test scope and release hashes, generated only after verification.

### Task 1: Signal records, live collector, and evidence gate

**Files:**
- Create: `foundry/config.json`
- Create: `foundry/src/__init__.py`
- Create: `foundry/src/models.py`
- Create: `foundry/src/collector.py`
- Create: `foundry/src/gate.py`
- Create: `foundry/tests/fixtures/gumroad-search.html`
- Create: `foundry/tests/fixtures/gumroad-product.html`
- Create: `foundry/tests/fixtures/github-release.json`
- Create: `foundry/tests/test_collector.py`
- Create: `foundry/tests/test_gate.py`

**Interfaces:**
- Produces: `Signal`, `Candidate`, `GateDecision`; `collect(config, observed_at, fixture_dir=None) -> list[Signal]`; `evaluate(candidate, signals, ledger) -> GateDecision`.
- Consumes: only JSON config, bounded fixture/live HTTP responses, and prior ledger JSONL.

- [ ] **Step 1: Write collector tests that fail before implementation**

```python
class CollectorTests(unittest.TestCase):
    def test_parses_gumroad_transactional_metrics(self):
        html = fixture("gumroad-product.html")
        signal = parse_gumroad_product(html, "https://example.gumroad.com/l/item", "2026-08-23T00:00:00Z")
        self.assertEqual(signal.metrics["sales_count"], 119)
        self.assertEqual(signal.metrics["price"], 14.67)
        self.assertEqual(signal.metrics["rating_count"], 1)

    def test_rejects_non_allowlisted_url(self):
        with self.assertRaises(ValueError):
            fetch_text("https://example.invalid/private", {"gumroad.com"})
```

- [ ] **Step 2: Run collector tests and confirm the missing-module failure**

Run: `python3 -m unittest foundry.tests.test_collector -v`
Expected: FAIL because `foundry.src.collector` does not exist.

- [ ] **Step 3: Implement typed records and bounded parsing**

```python
@dataclass(frozen=True)
class Signal:
    signal_id: str
    source_url: str
    source_type: str
    observed_at: str
    title: str
    metrics: dict[str, int | float | str]
    content_sha256: str

def parse_gumroad_product(html_text: str, url: str, observed_at: str) -> Signal:
    decoded = html.unescape(html_text)
    price = _required_float(decoded, r'product:price:amount[^>]+content="([0-9.]+)"')
    sales = _required_int(decoded, r'"sales_count":(\d+)')
    ratings = _required_int(decoded, r'"ratings":\{"count":(\d+)')
    title = _required_text(decoded, r'<title[^>]*>(.*?)</title>')
    return Signal(stable_id(url, observed_at[:10]), url, "paid_comparable", observed_at,
                  title, {"price": price, "sales_count": sales, "rating_count": ratings}, sha256(decoded))
```

`fetch_text` must use a 20-second timeout, a descriptive user agent, a 2 MiB response cap, HTTPS only, and exact hostname allowlisting.

- [ ] **Step 4: Write gate tests for pass, weak evidence, duplicate, and unrelated metrics**

```python
def test_new_sku_requires_three_independent_signals_and_paid_comparable(self):
    decision = evaluate(candidate(), [paid_signal(), official_signal(), buyer_signal()], set())
    self.assertTrue(decision.passed)

def test_two_signals_fail_closed(self):
    decision = evaluate(candidate(), [paid_signal(), official_signal()], set())
    self.assertEqual(decision.reasons, ["need_at_least_3_independent_signals"])

def test_duplicate_product_slug_fails(self):
    decision = evaluate(candidate(), [paid_signal(), official_signal(), buyer_signal()], {"operator-kit"})
    self.assertIn("duplicate_slug", decision.reasons)
```

- [ ] **Step 5: Implement the deterministic gate**

```python
def evaluate(candidate: Candidate, signals: Sequence[Signal], ledger: set[str]) -> GateDecision:
    reasons: list[str] = []
    independent = {urlsplit(s.source_url).hostname for s in signals if s.signal_id in candidate.signal_ids}
    matched = [s for s in signals if s.signal_id in candidate.signal_ids]
    if len(independent) < 3:
        reasons.append("need_at_least_3_independent_signals")
    if not any(s.source_type == "paid_comparable" and int(s.metrics.get("sales_count", 0)) > 0 for s in matched):
        reasons.append("need_paid_transactional_evidence")
    if candidate.slug in ledger:
        reasons.append("duplicate_slug")
    if not candidate.buyer or not candidate.job_to_be_done or not candidate.product_delta:
        reasons.append("incomplete_buyer_outcome")
    return GateDecision(not reasons, tuple(reasons), tuple(s.signal_id for s in matched))
```

- [ ] **Step 6: Run both test modules**

Run: `python3 -m unittest foundry.tests.test_collector foundry.tests.test_gate -v`
Expected: all tests PASS.

- [ ] **Step 7: Commit the core**

```bash
git add foundry/config.json foundry/src foundry/tests
git commit -m "feat: add evidence-gated product signal core"
```

### Task 2: Copy, secret, license, and reproducible-package audits

**Files:**
- Create: `foundry/src/audits.py`
- Create: `foundry/src/packager.py`
- Create: `foundry/tests/test_audits.py`
- Create: `foundry/tests/test_packager.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: a product source directory and public copy files.
- Produces: `AuditFinding`, `audit_tree(root) -> list[AuditFinding]`, `build_release(source, output_zip, metadata) -> ReleaseManifest`.

- [ ] **Step 1: Add ignored paid-source paths and write failing audit tests**

```gitignore
private-products/
foundry/runs/
foundry/state/
```

```python
def test_flags_fabricated_social_proof(self):
    findings = audit_text("Join hundreds of developers. Best seller!", "index.html")
    self.assertEqual({f.code for f in findings}, {"unsupported_social_proof", "unsupported_bestseller"})

def test_flags_secret_like_content(self):
    findings = audit_text("OPENAI_API_KEY=sk-live-abcdefghijklmnopqrstuvwxyz", ".env")
    self.assertIn("secret_like_value", {f.code for f in findings})
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `python3 -m unittest foundry.tests.test_audits -v`
Expected: FAIL because `audit_text` is missing.

- [ ] **Step 3: Implement fail-closed audits**

```python
FORBIDDEN_CLAIMS = {
    "unsupported_social_proof": re.compile(r"\b(?:hundreds|thousands) of (?:developers|customers|users)\b", re.I),
    "unsupported_bestseller": re.compile(r"\bbest[ -]?seller\b", re.I),
    "unsupported_lifetime": re.compile(r"\blifetime updates?\b", re.I),
}
SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bgh[opusr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"(?i)(api[_-]?key|token|secret)\s*[:=]\s*[^\s$<{]{12,}"),
)
```

`audit_tree` must also reject symlinks, `.env`, auth/session/state files, unresolved `YOUR_*` placeholders, executable binaries, and third-party directories missing a notice entry.

- [ ] **Step 4: Write deterministic ZIP tests**

```python
def test_same_input_produces_same_zip_hash(self):
    first = build_release(source_dir, path("one.zip"), metadata())
    second = build_release(source_dir, path("two.zip"), metadata())
    self.assertEqual(first.archive_sha256, second.archive_sha256)

def test_failed_audit_does_not_create_zip(self):
    write(source_dir / ".env", "TOKEN=secretsecretsecret")
    with self.assertRaises(PackagingError):
        build_release(source_dir, path("bad.zip"), metadata())
    self.assertFalse(path("bad.zip").exists())
```

- [ ] **Step 5: Implement reproducible packaging**

Use sorted POSIX archive names, ZIP timestamp `(1980, 1, 1, 0, 0, 0)`, mode `0o644` except declared `.sh` launchers at `0o755`, `ZIP_DEFLATED`, and a generated `RELEASE-MANIFEST.json` containing file SHA-256 values, test command, tested platforms, source revision, price, and update-policy end date.

- [ ] **Step 6: Run audit and packaging tests**

Run: `python3 -m unittest foundry.tests.test_audits foundry.tests.test_packager -v`
Expected: all tests PASS and identical archive hashes.

- [ ] **Step 7: Commit audits and packaging**

```bash
git add .gitignore foundry/src/audits.py foundry/src/packager.py foundry/tests/test_audits.py foundry/tests/test_packager.py
git commit -m "feat: add fail-closed product packaging"
```

### Task 3: Build the ignored Hermes Hybrid Operator Kit

**Files:**
- Create (ignored): `private-products/hermes-hybrid-operator-kit/README.md`
- Create (ignored): `private-products/hermes-hybrid-operator-kit/LICENSE.txt`
- Create (ignored): `private-products/hermes-hybrid-operator-kit/THIRD_PARTY_NOTICES.md`
- Create (ignored): `private-products/hermes-hybrid-operator-kit/VERSION`
- Create (ignored): `private-products/hermes-hybrid-operator-kit/config/generate_config.py`
- Create (ignored): `private-products/hermes-hybrid-operator-kit/scripts/preflight.py`
- Create (ignored): `private-products/hermes-hybrid-operator-kit/scripts/install.sh`
- Create (ignored): `private-products/hermes-hybrid-operator-kit/scripts/install.ps1`
- Create (ignored): `private-products/hermes-hybrid-operator-kit/checklists/routing-policy.md`
- Create (ignored): `private-products/hermes-hybrid-operator-kit/checklists/rollback.md`
- Create (ignored): `private-products/hermes-hybrid-operator-kit/drills/*.md`
- Create (ignored): `private-products/hermes-hybrid-operator-kit/tests/test_kit.py`
- Create: `products/hermes-hybrid-operator-kit.json`

**Interfaces:**
- Consumes: explicit provider/model names and an optional Hermes config path.
- Produces: sanitized config fragment; redacted human/JSON preflight report; non-destructive staged installation; product test evidence.

- [ ] **Step 1: Write failing product tests**

```python
def test_generator_never_writes_credentials(self):
    text = render_config(authority_provider="openai-codex", authority_model="gpt-5.6-luna",
                         ollama_model="gpt-oss:120b", llamacpp_model="Ornith-1.0-35B")
    self.assertNotRegex(text, r"(?i)api[_-]?key|token|secret")
    self.assertNotIn("YOUR_", text)

def test_preflight_redacts_sensitive_yaml_values(self):
    report = inspect_config(fixture_config_with_fake_key())
    self.assertNotIn("fake-key-value", json.dumps(report))

def test_install_defaults_to_staging_directory(self):
    result = subprocess.run(["bash", "scripts/install.sh", "--dry-run"], text=True, capture_output=True)
    self.assertEqual(result.returncode, 0)
    self.assertIn("No live Hermes files changed", result.stdout)
```

- [ ] **Step 2: Run the product tests and confirm failure**

Run: `python3 private-products/hermes-hybrid-operator-kit/tests/test_kit.py -v`
Expected: FAIL because the product code does not exist.

- [ ] **Step 3: Implement the smallest operational kit**

`generate_config.py` must accept provider/model flags and write a fragment only to an explicit `--output`. `preflight.py` must check Hermes CLI presence/version, config readability, provider/model registration, localhost endpoint reachability, unresolved placeholders, and fallback policy without printing environment variables or config values. `install.sh` and `install.ps1` must default to dry-run/staging and require an explicit `--apply` for live copy operations.

- [ ] **Step 4: Add bounded documentation and license**

The personal-use license permits one buyer to use and modify the kit, forbids redistribution/resale, disclaims warranties and income outcomes, and identifies no affiliation with Nous Research or OpenAI. `THIRD_PARTY_NOTICES.md` must state that the archive contains no third-party code at release 1.0.0.

- [ ] **Step 5: Run product tests and audits**

Run:

```bash
python3 private-products/hermes-hybrid-operator-kit/tests/test_kit.py -v
python3 -m foundry.src.audits private-products/hermes-hybrid-operator-kit
```

Expected: all product tests PASS; audit prints `0 findings`.

- [ ] **Step 6: Run real Mac canaries**

Run:

```bash
python3 private-products/hermes-hybrid-operator-kit/scripts/preflight.py --json > /tmp/hermes-kit-mac-preflight.json
python3 -m json.tool /tmp/hermes-kit-mac-preflight.json >/dev/null
```

Expected: Hermes 0.20.5 detected, localhost model endpoints reachable, secrets absent, and exit 0.

- [ ] **Step 7: Keep paid source ignored and commit only public metadata**

```bash
git check-ignore private-products/hermes-hybrid-operator-kit/README.md
git add products/hermes-hybrid-operator-kit.json
git commit -m "feat: add public operator kit catalog metadata"
```

### Task 4: Replace the unverified multi-pack storefront with one truthful product

**Files:**
- Modify: `index.html`
- Modify: `order.html`
- Modify: `css/style.css`
- Modify: `js/main.js`
- Modify: `README.md`
- Create: `proof.html`
- Create: `foundry/tests/test_storefront.py`

**Interfaces:**
- Consumes: `products/hermes-hybrid-operator-kit.json` and the release manifest.
- Produces: a static public product page with checkout state `pending` or a real Gumroad URL; proof page with verified hashes and test scope.

- [ ] **Step 1: Write failing storefront assertions**

```python
FORBIDDEN = ["Best Seller", "hundreds of developers", "Lifetime Updates", "enterprise-quality", "★★★★★"]

def test_storefront_contains_only_one_sellable_product(self):
    page = read("index.html")
    self.assertEqual(page.count('data-product-card="true"'), 1)
    for phrase in FORBIDDEN:
        self.assertNotIn(phrase, page)

def test_pending_checkout_is_truthful(self):
    page = read("order.html")
    self.assertIn("Checkout is not open yet", page)
    self.assertNotIn("Request invoice", page)
```

- [ ] **Step 2: Run tests and confirm failure against the current site**

Run: `python3 -m unittest foundry.tests.test_storefront -v`
Expected: FAIL on multiple cards and unsupported claims.

- [ ] **Step 3: Rewrite the page around one outcome**

The hero must say: “Run Hermes locally for routine work. Escalate the work that matters.” The page must explain the $12 launch price, exact inclusions, tested scope, 30-day update window, free architecture repository, checkout status, and non-affiliation. It must not include testimonials, crossed-out prices, urgency, customer counts, or unsupported performance/cost percentages.

- [ ] **Step 4: Make checkout state explicit in one JavaScript object**

```javascript
const PRODUCT = Object.freeze({
  slug: 'hermes-hybrid-operator-kit',
  price: 12,
  checkoutUrl: '',
  status: 'pending'
});
```

When `checkoutUrl` is empty, the CTA must route to `order.html` and say “Checkout setup status.” When populated with an HTTPS Gumroad product URL, the CTA may say “Buy for $12.”

- [ ] **Step 5: Run static tests and local browser check**

Run:

```bash
python3 -m unittest foundry.tests.test_storefront -v
python3 -m http.server 8765 --bind 127.0.0.1
```

Expected: tests PASS; Chrome at `http://127.0.0.1:8765/` shows one product, working mobile navigation, no console errors, and a truthful pending checkout.

- [ ] **Step 6: Commit the storefront correction**

```bash
git add index.html order.html proof.html css/style.css js/main.js README.md foundry/tests/test_storefront.py
git commit -m "fix: make HermesPacks storefront evidence-based"
```

### Task 5: Hermes skill, scheduler wrappers, and idempotent cron installer

**Files:**
- Create: `foundry/prompts/SCOUT.md`
- Create: `foundry/prompts/BUILDER.md`
- Create: `foundry/skill/SKILL.md`
- Create: `foundry/AGENTS.md`
- Create: `foundry/scripts/product_foundry_collect.py`
- Create: `foundry/scripts/product_foundry_monitor.py`
- Create: `foundry/scripts/product_foundry_candidate.py`
- Create: `foundry/scripts/product_foundry_verify.py`
- Create: `foundry/install_jobs.py`
- Create: `foundry/tests/test_install_jobs.py`
- Create: `foundry/tests/test_orchestrator.py`
- Create: `foundry/tests/fixtures/operator-kit-candidate.json`
- Create: `foundry/src/orchestrator.py`

**Interfaces:**
- Consumes: core collector/gate/audit/packager functions and the ignored product source.
- Produces: four uniquely named Hermes jobs, deployed wrappers, run ledger, candidate files, release reports, and silent no-op behavior.

- [ ] **Step 1: Write failing installer/idempotency tests**

```python
def test_reconcile_creates_each_named_job_once(self):
    existing = []
    actions = plan_reconcile(existing, desired_jobs(ROOT))
    self.assertEqual([a.kind for a in actions], ["create", "create", "create", "create"])

def test_second_reconcile_is_noop(self):
    existing = desired_jobs(ROOT)
    self.assertEqual(plan_reconcile(existing, desired_jobs(ROOT)), [])

def test_no_change_monitor_is_byte_stable(self):
    first = monitor_payload(latest_signals())
    second = monitor_payload(latest_signals())
    self.assertEqual(first, second)
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `python3 -m unittest foundry.tests.test_install_jobs foundry.tests.test_orchestrator -v`
Expected: FAIL because installer/orchestrator modules do not exist.

- [ ] **Step 3: Implement the skill and prompts**

The scout prompt must read only the current normalized signal file and ledger, output zero or one candidate JSON, cite signal IDs, and never build. The builder prompt must require `gate.json` with `passed=true`, edit only ignored private product source/public factual metadata, run tests, and never publish, message buyers, access credentials, or change payment settings.

- [ ] **Step 4: Implement desired jobs**

```python
DESIRED = (
    Job("Product Foundry Signal Collector", "10 1 * * *", script="product_foundry_collect.py", no_agent=True, deliver="local"),
    Job("Product Foundry Local Scout", "25 1 * * *", monitor_script="product_foundry_monitor.py",
        provider="llamacpp", model="Ornith-1.0-35B", skill="hermes-product-foundry", deliver="local"),
    Job("Product Foundry Authority Builder", "0 2 * * 0", monitor_script="product_foundry_candidate.py",
        provider="openai-codex", model="gpt-5.6-sol", reasoning_effort="high",
        skill="hermes-product-foundry", deliver="telegram"),
    Job("Product Foundry Verifier", "45 2 * * 0", script="product_foundry_verify.py", no_agent=True, deliver="telegram"),
)
```

The installer copies wrappers to `~/.hermes/scripts/`, the skill to `~/.hermes/skills/hermes-product-foundry/`, and uses `hermes cron create/edit` with explicit workdir/provider/model flags. It resolves jobs by exact name and never edits unrelated jobs.

- [ ] **Step 5: Implement idempotent run state**

`orchestrator.py` must write atomic JSON through a sibling temporary file plus `os.replace`, lock with `fcntl.flock`, use `America/Chicago` dates, and refuse to rebuild an existing successful release for the same candidate/version.

- [ ] **Step 6: Run unit tests and a temporary-home installer dry run**

Run:

```bash
python3 -m unittest foundry.tests.test_install_jobs foundry.tests.test_orchestrator -v
python3 foundry/install_jobs.py --dry-run
```

Expected: tests PASS; dry run lists exactly four create/update actions without changing Hermes.

- [ ] **Step 7: Commit the Hermes integration**

```bash
git add foundry/AGENTS.md foundry/prompts foundry/skill foundry/scripts foundry/install_jobs.py foundry/src/orchestrator.py foundry/tests/test_install_jobs.py foundry/tests/test_orchestrator.py
git commit -m "feat: add Hermes-native product foundry jobs"
```

### Task 6: End-to-end dry run, cross-platform check, release bundle, and deployment

**Files:**
- Create (ignored): `foundry/runs/2026-08-23/*`
- Create (ignored): `dist/hermespacks/hermes-hybrid-operator-kit-1.0.0.zip`
- Modify: `proof.html`
- Modify: `products/hermes-hybrid-operator-kit.json`
- Modify: `js/main.js` only if a real Gumroad URL exists.

**Interfaces:**
- Consumes: every prior task.
- Produces: a fresh verified ZIP, listing bundle, live honest GitHub Pages release, and four healthy Hermes jobs.

- [ ] **Step 1: Run the full local suite**

Run:

```bash
python3 -m unittest discover -s foundry/tests -v
python3 private-products/hermes-hybrid-operator-kit/tests/test_kit.py -v
git diff --check
```

Expected: all tests PASS; no whitespace errors.

- [ ] **Step 2: Run an evidence-backed dry run**

Run:

```bash
python3 -m foundry.src.orchestrator collect --date 2026-08-23
python3 -m foundry.src.orchestrator gate --candidate foundry/tests/fixtures/operator-kit-candidate.json
python3 -m foundry.src.orchestrator package --product hermes-hybrid-operator-kit --version 1.0.0
```

Expected: collection records the live 22-result Gumroad query and 119-sale comparable; gate passes using three independent signals; package writes ZIP plus manifest and listing copy.

- [ ] **Step 3: Verify fail-closed behavior**

Run the gate with the paid-comparable signal removed and the audit with a temporary fake testimonial/secret fixture.
Expected: gate returns nonzero with `need_paid_transactional_evidence`; audit returns nonzero with both finding codes; no ZIP is created.

- [ ] **Step 4: Run the Windows compatibility preflight**

Copy only the product source to a temporary explicit directory on `home-windows`, run PowerShell syntax validation and `preflight.py --json` against the native/WSL Hermes environment, save redacted output back into the local run evidence, then remove only that temporary test directory. Do not claim local-model performance on the Windows GPU.

- [ ] **Step 5: Install and reconcile the four real Hermes jobs**

Run:

```bash
python3 foundry/install_jobs.py
python3 foundry/install_jobs.py --dry-run
hermes cron list
hermes cron status
```

Expected: first run creates/updates exactly four named jobs; second run says no changes; list shows explicit pins and correct schedules; scheduler is running.

- [ ] **Step 6: Trigger collector and verifier once**

Run:

```bash
hermes cron run "Product Foundry Signal Collector"
hermes cron tick
hermes cron run "Product Foundry Verifier"
hermes cron tick
hermes cron runs "Product Foundry Signal Collector" --limit 1
hermes cron runs "Product Foundry Verifier" --limit 1
```

Expected: both executions complete; collector writes normalized signals; verifier reports release hash or stays silent on an unchanged verified release.

- [ ] **Step 7: Prepare Gumroad without publishing**

Use Chrome to open the existing Gumroad seller account, verify the payout-method blocker, and prepare the product name, $12 price, listing copy, and ZIP upload path. Do not enter financial identity data, create persistent API credentials, or submit the final product until the required action-time confirmation and payout setup exist.

- [ ] **Step 8: Commit and push public release metadata/storefront**

```bash
git add proof.html products/hermes-hybrid-operator-kit.json
git diff --cached --check
git commit -m "release: publish operator kit verification metadata"
git push origin main
gh run watch --repo Dennis-Gireesh/automations-store --exit-status
```

Expected: GitHub Pages workflow succeeds; live site contains one verified product, truthful checkout status, and no prohibited claims.

- [ ] **Step 9: Final fresh verification**

Run:

```bash
python3 -m unittest discover -s foundry/tests -v
python3 private-products/hermes-hybrid-operator-kit/tests/test_kit.py -v
python3 -m foundry.src.audits index.html order.html proof.html private-products/hermes-hybrid-operator-kit
git status --short --branch
curl -fsS https://dennis-gireesh.github.io/automations-store/ | python3 -m foundry.src.audits --stdin
```

Expected: all tests PASS, audits return zero findings, worktree is clean except ignored paid/run artifacts, branch tracks `origin/main`, and the live page audit passes.
