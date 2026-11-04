J4+ SM-J415F — postmarketOS AArch64 rootfs test image

This is a ROOTFS IMAGE ONLY. It contains an AArch64 (64-bit ARM) postmarketOS
v25.12 console userspace for a J4 rootfs-boot experiment. It does not contain a
kernel or BOOT image, and it has not been flashed or boot-tested on the phone.

The earlier device test reached a shell using an ARM64 kernel and ARM64
initramfs. A separate attempt to use the ARMv7 rootfs with that ARM64 kernel
failed. This image changes the userspace to AArch64; it is a test candidate,
not a proven fix.

Contents and limits:
- Alpine/musl AArch64 userspace; /sbin/init is BusyBox/OpenRC.
- Root filesystem UUID: 86e7262b-b628-4b89-8a6e-e96ef9b1aab6, matching the
  existing Medusa64 debug boot command line.
- ext4 uses a conservative feature set for the downstream 3.18 kernel.
- USB-only debug service is configured for RNDIS at 172.16.42.1, with telnet
  and Dropbear listeners requested only on that address and non-USB ingress
  blocked on TCP ports 22 and 23.
- The root account password is empty for this USB-debug image. Keep it isolated
  to the intended USB link; do not expose the device to untrusted networks.
- No Medusa64 kernel modules are included in this first image. Wi-Fi, audio,
  Bluetooth, GPU acceleration, LXQt and touchscreen behavior are not validated
  or configured here.

Do not treat the rootfs as a normal install yet. Installing it over the
existing rootfs would erase the current LXQt, Wi-Fi and audio state. No
partition, BOOT image or /vendor contents were changed to create this file.
The next meaningful acceptance test is an owner-run boot with the matching
ARM64 kernel/ARM64 initramfs followed by this rootfs, recording pstore/serial or
RNDIS output. Until that happens, device boot status remains UNKNOWN.
