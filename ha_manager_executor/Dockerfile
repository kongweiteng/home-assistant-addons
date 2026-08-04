ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*

COPY run.sh /
COPY ha_manager_executor /opt/ha-manager-executor/ha_manager_executor

RUN chmod 0755 /run.sh

ENV PYTHONPATH=/opt/ha-manager-executor \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

CMD ["/run.sh"]
