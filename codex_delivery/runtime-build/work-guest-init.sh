#!/bin/busybox sh
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PYTHONDONTWRITEBYTECODE=1
mount -t proc proc /proc || poweroff -f
mount -t sysfs sysfs /sys || poweroff -f
mount -t devtmpfs devtmpfs /dev || poweroff -f
modprobe virtio_pci
modprobe virtio_blk
modprobe virtio_console
modprobe crc32c_generic
modprobe ext4
mkdir -p /bundle /inputs /workspace
mount -t ext4 -o ro,nosuid,nodev,noexec /dev/vda /bundle || poweroff -f
mount -o bind /bundle/files /inputs || poweroff -f
mount -t ext4 -o rw,nosuid,nodev /dev/vdb /workspace || poweroff -f
python3 -I /opt/poc/work_guest.py
sync
umount /workspace
poweroff -f
exit 1
