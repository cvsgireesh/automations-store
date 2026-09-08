# HermesPacks storefront

This is a dependency-free static storefront for one verified, pre-publication product: the Hermes Hybrid Operator Kit.

## Current state

- Launch price: $12
- Checkout: pending; no purchase link is published
- Updates: the current correction window ends on September 22, 2026
- Verification: `proof.html` records the archive hash and exact canary limits; the public verification JSON lives under `products/`

## Product scope

The kit describes a Generator, redacted preflight, safe installers, routing and rollback checklists, and four failure drills. It does not make production-performance, customer, cost-saving, or availability claims.

The [free architecture repository](https://github.com/cvsgireesh/hermes-hybrid-agent-router) is separate from this product. Independent product with no relationship to Nous Research or OpenAI.

## Local check

Run the public tests from the repository root:

```text
python3 -m unittest foundry.tests.test_storefront -v
```

For a local browser check, serve the repository root with any static HTTP server and open `index.html`.
