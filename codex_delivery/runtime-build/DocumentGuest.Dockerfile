# Trusted local builder only. GUEST_SOURCE_FROM must be an exact image digest.
ARG GUEST_SOURCE_FROM
FROM ${GUEST_SOURCE_FROM}
USER root
ARG DEBIAN_MIRROR=https://deb.debian.org/debian
RUN sed -i "s#https://mirrors.tuna.tsinghua.edu.cn/debian#${DEBIAN_MIRROR}#g; s#http://deb.debian.org/debian#${DEBIAN_MIRROR}#g" /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=0 -o Acquire::https::Timeout=20 update \
    && apt-get install -y --no-install-recommends python3-pil python3-openpyxl python3-reportlab fonts-noto-cjk fonts-wqy-zenhei fonts-dejavu-core nodejs \
    && rm -rf /var/lib/apt/lists/*
COPY prepare_document_guest.py /prepare_document_guest.py
RUN python3 -I -B /prepare_document_guest.py \
    && cd /guest/root && find . -print0 | cpio --null -o -H newc | gzip -1 > /guest/initramfs.gz
