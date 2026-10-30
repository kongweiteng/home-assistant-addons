ARG GUEST_SOURCE_FROM
FROM --platform=linux/amd64 ${GUEST_SOURCE_FROM}
COPY browser-guest-init.sh /guest/root/init
COPY browser_guest.py browser_driver.cjs /guest/root/opt/poc/
RUN chmod 0755 /guest/root/init \
    && cd /guest/root && find . -print0 | cpio --null -o -H newc | gzip -1 > /guest/work-initramfs.gz
