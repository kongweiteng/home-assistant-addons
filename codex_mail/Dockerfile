ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-debian:bookworm
FROM ${BUILD_FROM}

ARG DEBIAN_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/debian

RUN find /etc/apt -type f -exec \
        sed -i "s#http://deb.debian.org/debian#${DEBIAN_MIRROR}#g; s#https://deb.debian.org/debian#${DEBIAN_MIRROR}#g" {} + \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates python3 \
    && rm -rf /var/lib/apt/lists/*

COPY run.sh /
COPY codex_mail /opt/codex-mail/codex_mail

RUN chmod 0755 /run.sh

ENV PYTHONPATH=/opt/codex-mail \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

CMD ["/run.sh"]
