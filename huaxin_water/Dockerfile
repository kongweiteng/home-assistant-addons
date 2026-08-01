ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-debian:bookworm
FROM ${BUILD_FROM}

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates python3 python3-pip \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m pip install --no-cache-dir --break-system-packages paho-mqtt==2.1.0

COPY run.sh /
COPY huaxin_water /opt/huaxin-water/huaxin_water

RUN chmod 0755 /run.sh

ENV PYTHONPATH=/opt/huaxin-water \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

CMD ["/run.sh"]
