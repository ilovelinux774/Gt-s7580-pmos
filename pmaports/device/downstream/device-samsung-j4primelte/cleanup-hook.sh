#!/bin/sh
# Runs only once the pmOS rootfs is mounted. Do not write Android partitions.
[ -e /sysroot/etc/os-release ] || exit 0
grep -q 'ID=postmarketos' /sysroot/etc/os-release || exit 0
mount -o remount,rw /sysroot || exit 0
mkdir -p /sysroot/var/log/j4
cp /pmOS_init.log /sysroot/var/log/j4/initramfs.log 2>/dev/null || true
dmesg > /sysroot/var/log/j4/dmesg-before-switch-root.txt
cat /proc/cmdline > /sysroot/var/log/j4/cmdline.txt
sync
