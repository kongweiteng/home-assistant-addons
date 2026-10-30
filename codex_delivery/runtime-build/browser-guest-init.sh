#!/bin/busybox sh
export PATH=/usr/bin:/bin
mount -t proc -o hidepid=2 proc /proc || poweroff -f
mount -t sysfs sysfs /sys || poweroff -f
mount -t devtmpfs devtmpfs /dev || poweroff -f
modprobe virtio_pci
modprobe virtio_blk
modprobe virtio_console
modprobe crc32c_generic
modprobe ext4
mkdir -p /bundle /dev/shm /tmp
mount -t ext4 -o ro,nosuid,nodev,noexec /dev/vda /bundle || poweroff -f
mount -t tmpfs -o nosuid,nodev,mode=1777,size=64m tmpfs /dev/shm || poweroff -f
mount -t tmpfs -o nosuid,nodev,mode=1777,size=256m tmpfs /tmp || poweroff -f
python3 -I /opt/poc/browser_guest.py
poweroff -f
