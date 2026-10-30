# Trusted offline browser guest builder; supply a verified document guest digest.
ARG GUEST_SOURCE_FROM
FROM --platform=linux/amd64 ${GUEST_SOURCE_FROM}
USER root
ARG DEBIAN_MIRROR=https://deb.debian.org/debian
RUN sed -i "s#https://mirrors.tuna.tsinghua.edu.cn/debian#${DEBIAN_MIRROR}#g; s#http://deb.debian.org/debian#${DEBIAN_MIRROR}#g" /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=0 -o Acquire::https::Timeout=20 update \
    && apt-get install -y --no-install-recommends chromium \
    && rm -rf /var/lib/apt/lists/*
COPY prepare_browser_guest.py /prepare_browser_guest.py
RUN python3 -I -B /prepare_browser_guest.py \
    && cd /guest/root && find . -print0 | cpio --null -o -H newc | gzip -1 > /guest/initramfs.gz
