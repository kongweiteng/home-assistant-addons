# DrawIO Home Assistant Add-on

Run the browser-based [DrawIO](https://www.drawio.com/) diagram editor inside Home Assistant and open it through Home Assistant Ingress.

## Features

- Uses the official [`jgraph/drawio`](https://github.com/jgraph/docker-drawio) container image.
- Supports Home Assistant OS on `amd64` and `aarch64`.
- Uses Home Assistant Ingress, so no DrawIO port is published on the host.
- Requires no application username, password or API token.
- Pins a platform-specific image digest for reproducible builds.

## Installation

1. Add `https://github.com/kongweiteng/home-assistant-addons` under **Settings > Apps > Install app > Repositories**.
2. Refresh the app store and open **DrawIO**.
3. Install and start the app.
4. Select **Open Web UI** to launch DrawIO through Home Assistant.

The initial package is marked experimental until the supported architectures and the Ingress workflow have been validated on real Home Assistant OS installations.

## Document storage

This add-on provides the DrawIO web application; it is not a server-side diagram repository.

- Files opened from or downloaded to your computer remain on that computer unless you copy them elsewhere.
- Browser local storage belongs to the browser profile and must not be treated as a durable backup.
- Home Assistant backups do not automatically include diagrams kept only in browser local storage or in a computer's Downloads folder.
- Save important diagrams as `.drawio` files and copy them to a separately backed-up location, such as fnOS/NAS storage or a version-controlled documentation directory.

## Network and authentication

The add-on exposes port `8080` only to Home Assistant Ingress. It does not publish a direct host port. Access through the Home Assistant sidebar is protected by the current Home Assistant session.

Do not expose the container directly to the internet. If external access is required, enter Home Assistant through the existing VPN or authenticated remote-access boundary.

## Upstream and version pinning

- DrawIO container source: [`jgraph/docker-drawio`](https://github.com/jgraph/docker-drawio), Apache License 2.0.
- Packaging reference: [`waxgourd-ha/waxgourd-addons/drawio`](https://github.com/waxgourd-ha/waxgourd-addons/tree/85be13431ff3f35b2141679b132b03749a4d5d88/drawio).
- Upstream version: `30.3.14`.
- Multi-architecture index digest: `sha256:af111fdd16d6081d37440dfa1f0d1c8b6c3047d41521ae7eb2860397dc3bd2a3`.
- `amd64` manifest: `sha256:bcc1359c7e4b509aff0733fe36450ec885462f962dff8c5d5f94b2fbf5635dcc`.
- `aarch64` manifest: `sha256:cc0ba7da584ea925e0d8be4c48bb89db9584e865a0c92478ad3ffd9caa7ddf28`.

When upgrading, update `config.yaml`, both digests in `build.yaml`, this version record and `CHANGELOG.md` together, then build-check both architectures.
