ARG TCG_FROM
ARG LAUNCHER_FROM
ARG GUEST_FROM=local/codex-work-guest:amd64
FROM ${LAUNCHER_FROM} AS launcher
FROM --platform=linux/amd64 ${GUEST_FROM} AS guest
FROM ${TCG_FROM}
USER root
COPY --from=launcher /isolate /opt/qemu-runtime/bin/isolate
COPY --from=guest /guest/work-initramfs.gz /opt/ha-vm-poc/initramfs.gz
COPY prepare_runtime.py /prepare_runtime.py
ARG RESOURCE_PROFILE=cgroup-v1
RUN RESOURCE_PROFILE=${RESOURCE_PROFILE} RUNTIME_KIND=work-qemu-v1 python3 /prepare_runtime.py
USER 65534:65534
