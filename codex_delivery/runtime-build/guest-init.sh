#!/bin/busybox sh
# All writes are inside this private guest or its new disposable task disk.
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PYTHONDONTWRITEBYTECODE=1
mount -t proc proc /proc || poweroff -f
mount -t sysfs sysfs /sys || poweroff -f
mount -t devtmpfs devtmpfs /dev || poweroff -f
modprobe virtio_pci
modprobe virtio_blk
modprobe crc32c_generic
modprobe ext4
mount -t ext4 /dev/vda /work || { echo POC_MOUNT_FAILED; poweroff -f; }
python3 /opt/poc/task.py
task_status=$?
echo "POC_TASK_STATUS=$task_status"
# The separate memory child is the only deliberate guest OOM victim.
python3 /opt/poc/memory.py
memory_status=$?
echo "POC_MEMORY_STATUS=$memory_status"
sync
umount /work
echo POC_GUEST_DONE
poweroff -f
exit 1
