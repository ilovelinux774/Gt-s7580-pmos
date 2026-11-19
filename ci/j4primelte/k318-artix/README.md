# J4+ 3.18.120 + Artix OpenRC experiment

This is a separate experimental BOOT/rootfs pair after the 7.1.3/systemd
candidate failed to reach a confirmed kernel boot.

## What the image uses

* The exact ARM64 Linux `3.18.120-j4-medusa64-debug+` kernel payload with its
  appended J4 Rev06 DTB, from the Medusa image previously reported to start.
* The Medusa v0 Android boot-header page size and load addresses, retained by
  `pack-medusa-openrc-boot.sh`.
* The authenticated Arch Linux ARM aarch64 rootfs as the ARM userspace seed.
* Artix's OpenRC 0.44 fork, pinned by commit in `sources.env`, built for
  AArch64 with the Artix package layout; `/sbin/init` and `/usr/bin/init`
  resolve to `openrc-init`. A serial `agetty` is configured on `ttyMSM0` for
  first-boot diagnosis.

Artix's OpenRC package recipe currently targets x86_64. The J4+ is AArch64, so
this experimental image builds that same OpenRC source natively on an AArch64
runner and layers it onto the Arch Linux ARM rootfs. It is therefore an
**Artix/OpenRC ARM adaptation**, not an official Artix ARM release or a complete
systemd-to-Artix package swap. Systemd packages are left installed for library
compatibility, but are not selected as PID 1. Do not treat this as a supported
distro image.

## Build and artifacts

The `J4+ Linux 3.18 Artix/OpenRC` workflow emits separate `boot.img` and
`rootfs.img` artifacts. The boot initramfs discovers `LABEL=ARCHROOT`, mounts
ext4, and switches to `/sbin/init` (OpenRC). The rootfs image replaces `/system`
when installed there; verify the live partition mapping and keep a full backup.
CI validates build inputs, the boot container, the appended J4 Rev06 DTB, the
root label, and sizes. It does **not** establish that this image boots on the
handset.

For a first test, flash BOOT only and retain a known-good recovery image. The
rootfs must be written as an ext4 block filesystem labeled `ARCHROOT`; copying
`rootfs.img` as an ordinary file is not sufficient. A serial capture on the
phone's 1.8 V UART is the useful console because fbcon is intentionally not
relied on.
