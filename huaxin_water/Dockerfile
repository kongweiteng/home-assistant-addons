ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-debian:bookworm
FROM ${BUILD_FROM}

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates python3 \
    && rm -rf /var/lib/apt/lists/*

COPY run.sh /
COPY huaxin_water /opt/huaxin-water/huaxin_water

RUN chmod 0755 /run.sh

ENV PYTHONPATH=/opt/huaxin-water \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

CMD ["/run.sh"]
