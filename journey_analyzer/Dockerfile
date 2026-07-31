ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m pip install --no-cache-dir --break-system-packages paho-mqtt==2.1.0

COPY run.sh /
COPY journey_analyzer /opt/journey-analyzer/journey_analyzer

RUN chmod 0755 /run.sh

ENV PYTHONPATH=/opt/journey-analyzer \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

CMD ["/run.sh"]
