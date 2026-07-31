# ESLink Gas documentation

## Before installation

Only configure accounts you own or are explicitly authorized to manage. The
app loads two fixed ESLink hosts and calls only the observed read-only
`userInfoQuery` endpoint. It does not automate the recharge button or call any
payment, binding, reporting, valve or command endpoint.

The current upstream uses HTTP, so the service-hall token, account number and
returned personal data are not protected by TLS between Home Assistant and the
upstream service. Set `allow_insecure_http: true` only after accepting that
risk. A trusted HTTPS reverse proxy cannot be selected in version 0.1.0 because
the browser session and cookies are bound to the fixed upstream hosts.

## Configuration

Open the gas service hall in desktop WeChat and copy a fresh overview URL. The
URL normally contains a `token` and an `opid`. Treat the complete URL like a
password and paste it into `portal_url` without publishing it.

The desktop WeChat page rendering successfully is useful evidence that the
service-hall SPA can initialize outside a phone. It is not by itself proof that
the backend session is valid: the Add-on waits for a renewed `SESSION` cookie
or a settled, fully rendered service-hall page, then verifies the session by
loading the read-only meter page and query.

```yaml
accounts:
  - id: home
    user_no: "000000000000"
    user_name: "示例用户"
portal_url: "http://cloudselfhelp-mobile.eslink.cc/#/index?token=REPLACE_ME&opid=REPLACE_ME"
allow_insecure_http: true
poll_interval_minutes: 30
page_timeout_seconds: 25
stale_after_minutes: 180
include_personal_details: false
```

- `accounts[].id` is the stable local alias used in HA entity IDs.
- `accounts[].user_no` remains a string and is never logged in full.
- `accounts[].user_name` is needed by the observed recharge-page bootstrap and
  is masked as a password field in the Add-on configuration UI.
- `portal_url` establishes or renews the private browser session. If the
  service returns `auth_required`, copy a newly opened service-hall URL and
  replace this option.
- `include_personal_details` controls the Ingress cache and display only. MQTT
  Discovery stays low-sensitivity regardless of this option.

Multiple accounts can share one service-hall URL when they are authorized in
the same WeChat service-hall identity. If the utility requires independent
identities, run separate Add-on instances in a future version; version 0.1.0
uses one browser profile per app installation.

## Home Assistant entities

Each configured alias becomes one HA device with:

- balance (`CNY`);
- meter count;
- primary meter status;
- data status (`ok`, `no_meter`, `degraded`, `stale`, `auth_required`, or
  `unavailable`);
- last successful update;
- connectivity.

The authenticated Ingress page additionally shows the masked user number and
all returned meters. When `include_personal_details` is enabled it also shows
the customer name, service address and mobile number. These values may enter
the Add-on backup, but are not published as MQTT entity state or attributes.

## Failure and recovery

- `auth_required`: the portal token or persisted browser session is no longer
  accepted. Update `portal_url` with a fresh URL and restart the app.
- `degraded`: the current request failed but a recent successful value remains.
- `stale`: cached data is older than `stale_after_minutes`.
- `unavailable`: no successful data is available.

Stopping or uninstalling the app stops polling. A rollback to an earlier
version should preserve `/data`; delete app data only when you intentionally
want to remove stored cookies and cached personal information.
