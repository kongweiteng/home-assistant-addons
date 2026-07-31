ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*

COPY run.sh /
COPY operations_broker /opt/ha-operations-broker/operations_broker

RUN chmod 0755 /run.sh

ENV PYTHONPATH=/opt/ha-operations-broker \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

CMD ["/run.sh"]
