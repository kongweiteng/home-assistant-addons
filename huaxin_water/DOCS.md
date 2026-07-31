# Configuration and operation

## Options

```yaml
accounts:
  - id: home
    customer_no: "000000000001"
base_url: http://www.huaxinshuiwu.com/api
allow_insecure_http: false
poll_interval_minutes: 360
request_timeout_seconds: 15
stale_after_minutes: 1440
manual_refresh_cooldown_seconds: 60
```

- `accounts[].id` is a unique local alias containing lowercase letters,
  numbers, `_` or `-`.
- `accounts[].customer_no` is kept as a string. Configure only accounts you own
  or are authorized to manage. A maximum of 20 accounts is accepted.
- `base_url` defaults to the observed upstream API root. A private HTTPS proxy
  may be used instead, but it must preserve the five supported paths.
- `allow_insecure_http` is `false` by default. The observed upstream currently
  has no working HTTPS listener, so use of its default HTTP URL requires an
  explicit acknowledgement. Customer numbers and returned personal data then
  travel over plaintext HTTP outside Home Assistant.
- Polling is at least hourly. Manual refresh is limited per account.

The example customer number is synthetic. Do not copy real options, caches,
logs or backups into the public source repository.

## Supported read-only data

- Customer summary, service address and meter details.
- Water-usage records.
- Payment records.
- Tier usage.
- Balance and arrears summary.

No arbitrary URL or customer-number input is available in the Ingress page.
The client has a fixed path allowlist and uses `GET` only. History responses are
bounded to 500 records per category, meters to 50 and tiers to 20; truncation is
reported as a contract issue instead of allowing unbounded cache or UI growth.

## State and recovery

The last successful normalized response for each account and endpoint is saved
atomically in `/data/state.json`. The cache stores a keyed account reference,
not the configured customer number. Personal information such as name and
service address remains in the cached response and is therefore included in
Home Assistant backups; protect backups accordingly.

When a refresh fails:

- endpoints with no previous success become `error`;
- endpoints with previous data become `stale` and retain that data;
- other accounts and endpoints are not cleared;
- communication failure is never converted to zero usage or zero balance.

Stop the app before restoring or manually replacing its data. A cold Home
Assistant backup provides a consistent rollback point. Removing the app's data
deletes its cached history but never changes the upstream water account.

## Privacy and logs

Runtime logs include only the account alias, the final four customer-number
digits, endpoint category, status/error class and duration. They do not include
complete request URLs, response bodies, names, addresses, phone numbers or
financial values.

## Validation boundary

Repository tests use synthetic fixtures. A successful local test or image build
does not prove that the undocumented upstream still works or that the app has
been accepted on a production Home Assistant OS instance.
