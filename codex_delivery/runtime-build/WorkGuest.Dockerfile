ARG GUEST_FROM=local/codex-work-guest-source:amd64
FROM --platform=linux/amd64 ${GUEST_FROM}
COPY work-guest-init.sh /guest/root/init
COPY work_guest.py /guest/root/opt/poc/work_guest.py
RUN chmod 0755 /guest/root/init \
    && cd /guest/root && find . -print0 | cpio --null -o -H newc | gzip -1 > /guest/work-initramfs.gz
