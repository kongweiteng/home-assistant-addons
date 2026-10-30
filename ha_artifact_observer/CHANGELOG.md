# Changelog

## 0.0.5 — 2026-10-31

- Correct the actual Supervisor build path: remove digest-form `build.yaml`
  entries, which Supervisor `2026.10.5` rejected. The failed `0.0.4` install
  fell back to Alpine and could not run Debian package installation; it did not
  produce an installed version or start the observer.
- Pin both Debian bases directly in separate Dockerfile stages and select the
  final stage using BuildKit's automatic Docker `TARGETARCH`. Ignore
  Supervisor's `BUILD_FROM` override so it cannot silently replace Debian.
- Add build-contract regressions for supported architecture selection,
  immutable base pins and removal of the incompatible build configuration.
  Prior local image success is not HA build acceptance; the corrected version
  requires new image and real-instance verification.

## 0.0.4 — 2026-10-31

- Deliver the completed standalone artifact observer without unfinished
  Controller, Delivery, preview-library or Registry dependencies. Main installs
  only health and exact-target artifact observations; preview remains 404.
- Require fixed instance/target registrations, separate bearer identities and
  explicit deployment authorization for the high-trust Docker socket. Public
  metadata keeps socket/API permissions and both observation flags off.
- Fence actual container/image identities through three fixed Docker GETs;
  preserve typed ImageID/RepoDigests and fail closed on unavailable or changed
  facts. Filter private engine/configuration data and exception text.
- Bound worker/observation concurrency and network budgets. Add an absolute
  five-second request-line/header deadline and reject duplicate GET lengths and
  all transfer/content encodings, including empty values.
- Include real local Unix/TCP regressions and opt-in isolated image checks.
  Local results are separate from image/architecture and actual HA acceptance;
  no platform-wide execution or deployment completion is claimed.
