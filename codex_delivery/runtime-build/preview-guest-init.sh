#!/bin/busybox sh
export PATH=/usr/bin:/bin
export PYTHONDONTWRITEBYTECODE=1
mount -t proc -o hidepid=2 proc /proc || poweroff -f
mount -t sysfs sysfs /sys || poweroff -f
mount -t devtmpfs devtmpfs /dev || poweroff -f
modprobe virtio_pci
modprobe virtio_blk
modprobe virtio_console
modprobe crc32c_generic
modprobe ext4
mkdir -p /bundle /inputs /workspace /tmp /dev/shm
mount -t ext4 -o ro,nosuid,nodev,noexec /dev/vda /bundle || poweroff -f
mount -o bind /bundle/files /inputs || poweroff -f
mount -t ext4 -o rw,nosuid,nodev /dev/vdb /workspace || poweroff -f
mount -t tmpfs -o nosuid,nodev,mode=1777,size=256m tmpfs /tmp || poweroff -f
mount -t tmpfs -o nosuid,nodev,mode=1777,size=64m tmpfs /dev/shm || poweroff -f
/bin/busybox ip link set lo up || poweroff -f
python3 -I -B /opt/poc/preview_guest.py
sync
umount /workspace
poweroff -f
