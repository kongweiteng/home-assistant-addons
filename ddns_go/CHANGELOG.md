# Changelog

## 6.17.3

- Keep `curl` in the runtime image because `bashio::config` needs it to read
  application options from the Home Assistant Supervisor API.
- Fix the empty update frequency that caused DDNS-GO to exit with
  `invalid value "" for flag -f` and restart continuously.

## 6.17.2

- Initial version in the Kongweiteng Home Assistant Add-ons repository.
- Package upstream DDNS-GO 6.17.2 for `amd64` and `aarch64`.
- Verify upstream release archives with pinned SHA-256 checksums.
- Persist configuration in the Home Assistant add-on private configuration directory.
