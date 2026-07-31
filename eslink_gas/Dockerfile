ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-debian:bookworm
FROM ${BUILD_FROM}

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        chromium \
        chromium-driver \
        python3 \
        python3-pip \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m pip install --no-cache-dir --break-system-packages \
        paho-mqtt==2.1.0 \
        selenium==4.35.0

COPY run.sh /
COPY eslink_gas /opt/eslink-gas/eslink_gas

RUN chmod 0755 /run.sh

ENV PYTHONPATH=/opt/eslink-gas \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

CMD ["/run.sh"]
