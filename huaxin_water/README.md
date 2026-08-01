# Huaxin Water

Huaxin Water is a read-only Home Assistant app for viewing multiple authorized
Tianjin Huaxin Water customer accounts through authenticated Home Assistant
Ingress. It shows customer information, service address, meters, water usage,
payment history, tier usage, balance and arrears.

The app calls only five fixed `GET` endpoint categories. It does not implement
account binding, unbinding, online payment, invoicing, suggestions, repairs or
uploads, and it does not expose a host port or request Home Assistant or
Supervisor management permissions.

## Features

- Multiple configured accounts with stable local aliases and isolated state.
- Service address and all returned meters displayed in the Ingress UI.
- Independent endpoint status, partial-failure handling and last-success cache.
- Atomic `/data/state.json` persistence without copying configured customer
  numbers into the cache.
- Low-frequency polling plus a per-account manual refresh cooldown.
- Per-account annual overview, twelve-month usage/payment trend and cross-year
  comparison derived locally from the bounded history already returned.
- Retained MQTT Discovery devices and aggregate entities for every configured
  account, with LWT availability and no manual Home Assistant YAML.
- Synthetic tests only; no real customer number or upstream response is stored
  in this public repository.

## Install

1. Add `https://github.com/kongweiteng/home-assistant-addons` to the Home
   Assistant app store.
2. Install **Huaxin Water**.
3. Add one or more authorized account aliases and customer numbers.
4. Review the plain-HTTP warning in [DOCS.md](DOCS.md). The current default
   upstream is blocked until `allow_insecure_http` is explicitly enabled.
5. Start the app and open it from the Home Assistant sidebar.

The app prefers a Home Assistant Supervisor MQTT service. If the installed
broker does not register one, configure its connection only in private add-on
options; credentials never enter Discovery, state or the public repository.

This source release has no official relationship with Tianjin Huaxin Water.
The upstream H5 interface is undocumented and may change without notice.
