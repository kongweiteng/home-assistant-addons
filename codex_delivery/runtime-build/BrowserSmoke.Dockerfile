ARG GUEST_SOURCE_FROM
FROM --platform=linux/amd64 ${GUEST_SOURCE_FROM}
COPY browser-smoke-init.sh /guest/root/init
COPY browser_smoke.py /guest/root/opt/poc/browser_smoke.py
RUN chmod 0755 /guest/root/init \
    && cd /guest/root && find . -print0 | cpio --null -o -H newc | gzip -1 > /guest/work-initramfs.gz
