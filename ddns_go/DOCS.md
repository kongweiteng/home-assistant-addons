# DDNS-GO

## Purpose

DDNS-GO detects the current public IPv4 or IPv6 address and updates a DNS record. In this Home Assistant environment it is intended to update an Aliyun DNS record such as `ha.example.com`.

DDNS only updates DNS. It does not make Home Assistant safe to expose directly to the internet. Do not publish Home Assistant port `8123` or the DDNS-GO management port `9876` directly to the internet.

## Installation

1. Add `https://github.com/kongweiteng/home-assistant-addons` under **Settings > Apps > Install app > Repositories**.
2. Refresh the app store and install **DDNS-GO**.
3. Enable **Start on boot** and **Watchdog**.
4. Start the app and open its Web UI from the local network.

## Aliyun DNS configuration

1. Create a dedicated Aliyun RAM user for DDNS. Do not use the Alibaba Cloud account owner AccessKey.
2. Grant only the AliDNS API permissions needed to query and update DNS records.
3. In DDNS-GO select **Aliyun** as the DNS provider.
4. Enter the dedicated RAM user's AccessKey ID and AccessKey Secret.
5. Add the domain record, for example `ha.example.com`.
6. Configure an IPv4 `A` record first. Configure an IPv6 `AAAA` record only after IPv6 routing and firewall behavior have been verified.
7. Save, then confirm in the DDNS-GO log that the detected public IP matches the DNS record.

## Security

- Configure a strong DDNS-GO Web UI username and password immediately.
- Keep **禁止公网访问 / Deny WAN access** enabled.
- Keep port `9876` limited to the trusted LAN.
- Store Aliyun AccessKey credentials only inside the add-on configuration data and the local credential inventory; never commit them to this repository.
- Prefer VPN, a VPC tunnel, or a carefully configured HTTPS reverse proxy for remote Home Assistant access.

## App options

- `frequency`: Public-IP check interval in seconds. Default: `300`; minimum: `10`.
- `custom_dns`: Optional DNS resolver in `host:port` form. Leave empty to use the system resolver.

The persistent DDNS-GO configuration is stored in the add-on's private configuration directory as `ddns-go.yaml` and is included in Home Assistant backups.
