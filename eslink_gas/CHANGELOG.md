# Changelog

## 0.1.1

- Use a Debian package mirror build argument selected for reliable HAOS builds
  in constrained networks; package authenticity remains enforced by Debian's
  signed repository metadata.

## 0.1.0

- Add read-only multi-account ESLink gas balance and meter-status collection.
- Add a persisted private Chromium session with fixed-host and third-party
  request restrictions.
- Add explicit authentication, degraded, stale and unavailable semantics.
- Add authenticated Ingress and low-sensitivity MQTT Discovery entities.
- Keep recharge, payment, binding and all other write operations out of scope.
