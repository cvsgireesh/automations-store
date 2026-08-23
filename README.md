# HermesPacks storefront

This is a dependency-free static storefront for one pre-release product: the Hermes Hybrid Operator Kit.

## Current state

- Launch price: $12
- Checkout: pending; no purchase link is published
- Updates: a 30-day update window begins with the first packaged release
- Verification: `proof.html` records the current pending test scope and will publish the archive hash with the first packaged release

## Product scope

The kit describes a Generator, redacted preflight, safe installers, routing and rollback checklists, and four failure drills. It does not make production-performance, customer, cost-saving, or availability claims.

The free architecture repository is separate from this product. HermesPacks is not affiliated with Hermes Agent, its maintainers, or its contributors.

## Local check

Run the public tests from the repository root:

```text
python3 -m unittest foundry.tests.test_storefront -v
```

For a local browser check, serve the repository root with any static HTTP server and open `index.html`.
