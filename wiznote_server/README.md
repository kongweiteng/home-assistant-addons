# WizNote Server Home Assistant App

This app packages the official [`wiznote/wizserver`](https://hub.docker.com/r/wiznote/wizserver)
image for Home Assistant OS. It keeps the complete WizNote service state in the
Home Assistant app data directory and uses a cold backup so the embedded MySQL
database is not copied while it is running.

The first release is intentionally marked **experimental** until installation,
OSS storage, backup, and restore have been verified on a real HAOS system.

## Architecture

This release supports **amd64 (x86-64) Home Assistant OS only**, matching the
target Home Assistant hardware. The development Mac can be ARM64; Docker must
still build the add-on for Linux amd64:

```bash
docker build \
  --platform linux/amd64 \
  --build-arg BUILD_VERSION=1.0.0 \
  --build-arg BUILD_ARCH=amd64 \
  -t local/wiznote-ha-addon:1.0.0-amd64 \
  wiznote_server
```

ARM64 HAOS support is deliberately not advertised until that runtime has been
separately built and tested.

## Before the first start

1. Install the app but do not start it yet.
2. In the app configuration, set a strong **Initial administrator password**.
3. Confirm the network ports. TCP `80` is published as host port `8088` by
   default; UDP `9269` is the service port mapped by the upstream deployment.
4. Start the app. The first initialization can take several minutes.
5. Open `http://HOME_ASSISTANT_IP:8088` and sign in as `admin@wiz.cn` using the
   configured initial password.
6. After the first successful login, clear the initial password from the Home
   Assistant app options. WizNote only consumes it during first initialization.

If no initial password is supplied, upstream WizNote uses `123456`. Change it
immediately after logging in.

## Alibaba Cloud OSS

Configure object storage before creating the first real note:

1. In the WizNote administrator interface, open the data-storage settings.
2. Select **Alibaba Cloud OSS**.
3. Enter the bucket, region, AccessKey ID, and AccessKey secret.
4. Set `internal` to `false` because HAOS is not running inside Alibaba Cloud
   ECS private networking.
5. Save the settings and allow WizNote to restart its services.

Use a dedicated RAM user restricted to the selected bucket or prefix. Enable
OSS versioning and server-side encryption. Do not mount OSS with `ossfs` or
replace `/wiz/storage` with an object-storage filesystem.

Official guidance: [Use cloud object storage for WizNote data](https://www.wiz.cn/zh-cn/docker-using-object-storage).

WizNote warns not to switch an existing installation from local storage to OSS
after notes have already been created. Contact WizNote support for an existing
data migration.

## Persistent local state

The Home Assistant app data directory is mounted directly at `/wiz/storage`.
It includes the embedded database, indexes, configuration, logs, and any data
that remains local when OSS is enabled. Treat the entire directory as required
restore state.

## Backups to fnOS/NAS

The app declares `backup: cold`. Home Assistant Supervisor stops the app before
copying its data, and the wrapper gracefully stops PM2 services, nginx, Redis,
and MySQL before the container exits. The app allows up to 60 seconds for this
shutdown so a larger database is not cut off by the default short timeout.

Recommended schedule:

- Daily encrypted partial backup containing this app, stored on fnOS network
  storage.
- Weekly encrypted full Home Assistant backup, also stored on fnOS.
- OSS bucket versioning for note objects; NAS backups protect the local service
  database and configuration.

Do not rsync `/wiz/storage` while the app is running.

## Restore order

1. Restore the WizNote app backup in Home Assistant.
2. Confirm that the OSS bucket and credentials are still available.
3. Start the app and verify administrator login.
4. Create, edit, upload, search, and synchronize a test note before declaring
   the restore complete.

## Security and network exposure

- Keep port `8088` on the trusted LAN, VPN, or Tailscale network.
- Use an HTTPS reverse proxy before exposing WizNote outside the LAN.
- Do not publish the app directly to the internet over plain HTTP.
- Home Assistant Ingress is not enabled in this first release because the
  upstream web application has not yet been verified under an ingress path
  prefix.

## Upstream image status

The official amd64 `latest` image was last pushed in 2022. This repository pins
its image digest so a rebuild does not silently change the runtime. Updating the
upstream image requires a new app version, build validation, backup, and restore
test.
