ARG BROWSER_SOURCE_FROM
FROM --platform=linux/amd64 ${BROWSER_SOURCE_FROM}
COPY preview-guest-init.sh /guest/root/init
COPY preview_guest.py preview_driver.cjs work_guest.py /guest/root/opt/poc/
RUN chmod 0755 /guest/root/init \
    && cd /guest/root && find . -print0 | cpio --null -o -H newc | gzip -1 > /guest/work-initramfs.gz
