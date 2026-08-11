ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-aiohttp

COPY run.sh /
COPY codex_runner_relay /opt/codex-runner-relay/codex_runner_relay

RUN chmod 0755 /run.sh

ENV PYTHONPATH=/opt/codex-runner-relay \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

CMD ["/run.sh"]
