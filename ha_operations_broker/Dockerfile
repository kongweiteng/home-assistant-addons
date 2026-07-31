ARG BUILD_FROM
FROM ${BUILD_FROM}

ARG FIDO2_VERSION="2.2.1"
ARG FIDO2_SHA256="ed397da981b9ab133da6ead7309e41f924b566b749956129efe286fae097749f"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        python3 \
        python3-cryptography \
        python3-venv \
    && python3 -m venv --system-site-packages /opt/ha-operations-broker/venv \
    && curl -fsSL \
        "https://files.pythonhosted.org/packages/71/84/198d99c3312557ef6121cf78c38281efe9b3bc88cba0e2c05446f38a024d/fido2-${FIDO2_VERSION}-py3-none-any.whl" \
        -o "/tmp/fido2-${FIDO2_VERSION}-py3-none-any.whl" \
    && echo "${FIDO2_SHA256}  /tmp/fido2-${FIDO2_VERSION}-py3-none-any.whl" | sha256sum -c - \
    && /opt/ha-operations-broker/venv/bin/pip install --no-cache-dir --no-deps "/tmp/fido2-${FIDO2_VERSION}-py3-none-any.whl" \
    && rm -f "/tmp/fido2-${FIDO2_VERSION}-py3-none-any.whl" \
    && rm -rf /var/lib/apt/lists/*

COPY run.sh /
COPY operations_broker /opt/ha-operations-broker/operations_broker

RUN chmod 0755 /run.sh

ENV PYTHONPATH=/opt/ha-operations-broker \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

CMD ["/run.sh"]
