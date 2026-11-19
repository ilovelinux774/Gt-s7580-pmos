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
  resolve to `openrc-init`.
* A boot-enabled `j4-rndis` OpenRC service that activates RNDIS using the
  kernel's legacy `/sys/class/android_usb/android0/{functions,enable}` API,
  configures the phone as `172.16.42.1/24`, then starts SSH bound only to that
  USB-network address. This is specifically for the 3.18 Samsung composite
  gadget; it does not use USB configfs.

Artix's OpenRC package recipe currently targets x86_64. The J4+ is AArch64, so
this experimental image builds that same OpenRC source natively on an AArch64
runner and layers it onto the Arch Linux ARM rootfs. It is therefore an
**Artix/OpenRC ARM adaptation**, not an official Artix ARM release or a complete
systemd-to-Artix package swap. Systemd packages are left installed for library
compatibility, but are not selected as PID 1. Do not treat this as a supported
distro image.

## Build and artifacts

The `J4+ Linux 3.18 Artix/OpenRC` workflow emits separate `boot.img` and
`rootfs.img` artifacts. The dedicated initramfs uses static BusyBox plus a small statically linked ext4
label scanner to discover `LABEL=ARCHROOT`, mount ext4, and switch to `/sbin/init`
(OpenRC). The rootfs image replaces `/system`
when installed there; verify the live partition mapping and keep a full backup.
CI validates build inputs, the boot container, the appended J4 Rev06 DTB, the
root label, and sizes. It does **not** establish that this image boots on the
handset.

## USB RNDIS access

Flash the matching BOOT and `rootfs.img` pair (retain a known-good recovery
image first). The rootfs must be written as an ext4 block filesystem labeled
`ARCHROOT`; copying `rootfs.img` as an ordinary file is not sufficient. The
RNDIS service is started by OpenRC during boot. Use a known data-capable USB
cable connected directly to the host, and select the phone's USB data mode if
the recovery/boot UI offers one.

On a Linux host, after the handset has booted, check `lsusb` and `ip link` for
a newly enumerated USB device/network interface. Configure that newly appeared
host interface (replace `enx...` with its actual name):

```sh
sudo ip address replace 172.16.42.2/24 dev enx...
sudo ip link set enx... up
ssh alarm@172.16.42.1
```

The SSH server accepts only the existing `alarm` account and is bound to the
USB address; use the account password set in the rootfs. If using the stock
Arch Linux ARM credentials, change the default password immediately once
logged in (`passwd`). Do not expose this temporary diagnostic image to an
untrusted network. The phone has no DHCP server on this point-to-point link,
so assign the host address manually. If the SSH connection is refused, inspect
the host's `dmesg`/`lsusb` and wait a minute after boot: the gadget must first
enumerate and RNDIS create a host network interface.

If `lsusb` still shows no phone at all, the problem is earlier than IP
configuration: cable, USB role/PHY, gadget driver registration, or the phone
not reaching OpenRC. This change cannot force a physically unenumerated USB
controller to connect. Since no shell/display is currently confirmed, report
whether a new USB device appears and the exact `ip link` interface name; do not
assume a successful CI build proves on-device operation.
