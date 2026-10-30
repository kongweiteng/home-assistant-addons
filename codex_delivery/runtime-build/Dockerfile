# Credential-free isolated runtime builder and synthetic qualification guest.
# This is not an installable Add-on or a production execution service.
# Supply pinned official HA Debian base digests from the verified local cache.
ARG GUEST_BASE=ghcr.io/home-assistant/amd64-base-debian@sha256:84023298f975360b4350fe76118a1a5b117ff8f7ca5249d6398381659051aba3
ARG RUNTIME_BASE=ghcr.io/home-assistant/amd64-base-debian@sha256:84023298f975360b4350fe76118a1a5b117ff8f7ca5249d6398381659051aba3
ARG GUEST_PLATFORM=linux/amd64
FROM --platform=${GUEST_PLATFORM} ${GUEST_BASE} AS guest
USER root
RUN sed -i 's|http://deb.debian.org|https://mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends python3 busybox-static cpio kmod >/dev/null \
    && mkdir -p /guest/kernel-package \
    && cd /guest && apt-get download "$(apt-cache depends linux-image-amd64 | awk '/Depends:/{print $2; exit}')" \
    && dpkg-deb -x /guest/linux-image-*.deb /guest/kernel-package
COPY prepare_guest.py /prepare_guest.py
RUN python3 /prepare_guest.py
COPY guest-init.sh /guest/root/init
COPY guest-task.py /guest/root/opt/poc/task.py
COPY guest-memory.py /guest/root/opt/poc/memory.py
RUN chmod 0755 /guest/root/init \
    && cd /guest/root && find . -print0 | cpio --null -o -H newc | gzip -1 > /guest/initramfs.gz

FROM ${RUNTIME_BASE}
USER root
RUN sed -i 's|http://deb.debian.org|https://mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends qemu-system-x86 python3 e2fsprogs >/dev/null \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /opt/ha-vm-poc
COPY --from=guest /guest/kernel /guest/initramfs.gz /opt/ha-vm-poc/
COPY launcher.py /opt/ha-vm-poc/launcher.py
USER 65534:65534
ENV PATH=/usr/sbin:/usr/bin:/sbin:/bin
ENTRYPOINT ["python3", "/opt/ha-vm-poc/launcher.py"]
