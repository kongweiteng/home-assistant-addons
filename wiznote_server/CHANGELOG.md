# Changelog

## [1.0.0] - 2026-07-29

### Added

- Package the official `wiznote/wizserver` image as a Home Assistant app.
- Persist the complete `/wiz/storage` directory in Home Assistant app data.
- Stop the app during Home Assistant backups and gracefully shut down WizNote,
  MySQL, Redis, nginx, and PM2-managed services before the container exits.
- Support an initial administrator password and configurable IANA time zone.
- Expose the web interface on host port `8088` by default and UDP discovery on
  port `9269`.
- Target the user's x86-64 Home Assistant OS host with an amd64-only image.

### Notes

- This first release is marked experimental until it completes a real HAOS
  install, first-run, OSS configuration, cold-backup, and restore test.
- The official amd64 upstream image is pinned by digest for reproducible builds.
