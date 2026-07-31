# Changelog

## 0.2.0

- Add per-account year selection, annual usage/billing/payment metrics, a
  twelve-month visual trend and a cross-year summary table.
- Aggregate multiple meters and payments in the same month without adding any
  upstream request.
- Keep missing dates and values explicit instead of presenting them as zero,
  with visible cached/stale and unparsed-record notices.

## 0.1.0

- Add read-only multi-account configuration and fixed upstream GET allowlist.
- Add a predictable non-redirecting HTTP transport for the observed upstream.
- Add normalization for customer/address, meters, usage, payments, tiers and
  balance/arrears responses with mixed numeric types.
- Add atomic last-success cache, stale/partial-failure states, polling and
  cooldown-protected manual refresh.
- Add an Ingress-only responsive dashboard and bounded JSON API.
