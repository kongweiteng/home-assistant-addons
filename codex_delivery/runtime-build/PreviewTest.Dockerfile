ARG RUNTIME_FROM
FROM ${RUNTIME_FROM}
USER root
COPY codex_delivery /app/codex_delivery
COPY tests /app/tests
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app:/app/tests
USER 65534:65534
ENTRYPOINT ["python3", "-m", "unittest"]
