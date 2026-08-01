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
mqtt_host: ""
mqtt_port: 1883
mqtt_username: ""
mqtt_password: ""
mqtt_ssl: false
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
- MQTT connection settings may stay empty when the installed broker registers a
  Supervisor `mqtt` service. Brokers such as EMQX that do not register that
  service require a reachable host, port and their private credentials here.
  Supervisor-provided settings take precedence when available.

The example customer number is synthetic. Do not copy real options, caches,
logs or backups into the public source repository.

## Supported read-only data

- Customer summary, service address and meter details.
- Water-usage records.
- Payment records.
- Tier usage.
- Balance and arrears summary.

The currently verified five read-only upstream endpoints do not require a
login, verification code or Cookie. The app does not invent an authentication
flow. If the upstream starts requiring authentication, the affected account is
suspended and the authorization design must be reviewed before adding a
controlled session flow.

## Year and month statistics

The **Statistics** tab derives an annual overview, a fixed twelve-month trend
and a cross-year summary from the current account's existing usage and payment
history. It does not call another upstream endpoint.

- Usage and receivable charges from multiple meters are summed by billing
  month.
- Payments are summed by their payment month and are not guessed to belong to
  a specific water bill.
- Missing months, unparseable dates and missing numeric values remain unknown;
  they are not displayed as zero.
- Cached, stale or incomplete source endpoints remain visible as a data-quality
  warning on the statistics page.

The annual average uses only months that contain a valid usage value. History
is still bounded by the existing 500-record limit per endpoint. Year-over-year
usage is the percentage change from the immediately preceding calendar year;
it remains unknown when either total is missing or the preceding total is zero.

No arbitrary URL or customer-number input is available in the Ingress page.
The client has a fixed path allowlist and uses `GET` only. History responses are
bounded to 500 records per category, meters to 50 and tiers to 20; truncation is
reported as a contract issue instead of allowing unbounded cache or UI growth.

## MQTT Discovery

The app publishes Home Assistant MQTT Discovery automatically. It first uses a
Supervisor-provided `mqtt` service; otherwise it uses the broker connection
stored in private add-on options. Credentials are never included in Discovery,
state, logs or the public repository.

- Discovery prefix: `homeassistant`.
- Global availability topic: `huaxin_water/status`.
- Per-account JSON state: `huaxin_water/<account_id>/state`.
- Per-account device identifier: `huaxin_water_<account_id>`.
- Discovery, state and availability use QoS 1 and retain.
- The direct MQTT connection sets a retained `offline` LWT and publishes
  retained `online` after connecting.
- Home Assistant's `homeassistant/status=online` birth message causes Discovery
  and the latest state to be republished.

Each account creates sensors for balance, arrears, latest billing-period charge
and usage, current-year charge and usage, latest meter reading, meter count,
payment status, billing period, update time and data status, plus a connectivity
binary sensor. Monetary sensors use the Home Assistant standard currency unit
`CNY`; water sensors use `m³`.

The latest billing period is the newest parseable month that contains a water
record. Payment status is derived only from arrears: positive means `欠费`, zero
means `无欠费`, and a missing value means `未知`.

MQTT payloads never include names, addresses, full or masked customer numbers,
meter registration numbers, payment history or water-record arrays. A small
`/data/mqtt-topics.json` registry stores topic names only so removed accounts
and retired entities can have their retained Discovery/state topics cleared.

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
