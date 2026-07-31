# ESLink Gas

ESLink Gas is an unofficial, read-only Home Assistant app for monitoring one or
more authorized gas accounts served by the ESLink mobile service hall. It uses
a private persisted Chromium profile because the observed account endpoint is
available only inside an authenticated browser session.

## Features

- Multiple account aliases, user numbers and names in private Add-on options.
- Read-only balance, meter count, meter status, meter type, price category and
  purchase-command status.
- Optional personal details in authenticated Ingress; MQTT entities never
  publish the customer name, address, mobile number or complete user number.
- Explicit `auth_required`, `degraded`, `stale` and `unavailable` states instead
  of replacing failures with zero.
- MQTT Discovery sensors for balance, meter count, meter status, data status,
  last successful update and connectivity.
- No recharge, payment, account binding, valve control or other write flow.

## Security boundary

The current upstream pages use plain HTTP. The app fails closed until
`allow_insecure_http` is explicitly enabled. The service-hall URL contains a
temporary token and is stored only in Home Assistant's private Add-on options.
Browser cookies and normalized cache data live under the app's private `/data`
directory and are included in cold backups.

This source release has no official relationship with ESLink or a gas utility.
The undocumented upstream page and response contract may change at any time.

See [DOCS.md](DOCS.md) for configuration, entity and reauthorization details.
