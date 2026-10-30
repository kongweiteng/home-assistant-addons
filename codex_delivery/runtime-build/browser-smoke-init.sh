#!/bin/busybox sh
export PATH=/usr/bin:/bin
mount -t proc proc /proc || poweroff -f
mount -t sysfs sysfs /sys || poweroff -f
mount -t devtmpfs devtmpfs /dev || poweroff -f
mkdir -p /dev/shm /tmp
mount -t tmpfs -o nosuid,nodev,mode=1777,size=64m tmpfs /dev/shm || poweroff -f
mount -t tmpfs -o nosuid,nodev,mode=1777,size=256m tmpfs /tmp || poweroff -f
python3 -I /opt/poc/browser_smoke.py
poweroff -f
