ARG TCG_FROM
ARG LAUNCHER_FROM
FROM ${LAUNCHER_FROM} AS launcher
FROM ${TCG_FROM}
USER root
COPY --from=launcher /isolate /opt/qemu-runtime/bin/isolate
COPY prepare_runtime.py /prepare_runtime.py
RUN python3 /prepare_runtime.py
COPY qualify-isolated-qemu.py /qualify-isolated-qemu.py
USER 65534:65534
ENTRYPOINT ["python3", "-I", "/qualify-isolated-qemu.py"]
