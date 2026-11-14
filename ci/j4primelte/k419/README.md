# J4+ 4.19 port — Redmi 5A/Riva reference

This is a new kernel-port track. It supersedes further K49 touchscreen-hang
experiments as the active direction, while leaving those earlier artifacts and
notes intact for reference. The goal remains a BOOT-only J4+ kernel with the
standalone diagnostic initramfs; do not build, mount, or switch to an installed
pmOS root filesystem.

## Pinned references

`sources.env` pins both generations so the Android 15 and 16 trees can be
compared reproducibly:

| Reference | Branch | Pinned revision | Kernel Makefile version |
| --- | --- | --- | --- |
| Redmi 5A/Riva Android 15 kernel | `15-dev` | `2dd4523b88849f0143f80f11f761461e7dff08eb` | 4.19.325 |
| Redmi 5A/Riva Android 16 kernel | `16-dev` | `0d30fa74be642e0613b02c0bfd15c9935e31e906` | 4.19.325 |
| Riva/Rolex Android 15 device tree | `15-dev` | `5913d14dd364930732fb689a90564a4cf5cda1e4` | — |
| Riva/Rolex Android 16 device tree | `16-dev` | `eefb49194920850619e89cc968297a11db03def9` | — |

Repositories:

* Kernel: <https://github.com/iusmac/kernel_rova>
* Device tree: <https://github.com/iusmac/device_rova>

Both device-tree branches select a 4.19 kernel and an MSM8937-family defconfig;
the kernel source contains MSM8917/Riva DTS and common Qualcomm platform code.
The 16-dev kernel Makefile itself reports Linux 4.19.325. Its local-version
suffix is not the base kernel version.

## Port boundary

The common MSM8917/8937-family code is the reference, not the Riva board
configuration. Riva DTS, Xiaomi regulators/panel/touch wiring, boot arguments,
boot-image layout, and partition assumptions must not be copied as J4+ board
data. The J4+ needs its Samsung-specific device tree and board setup, based on
its own hardware evidence and existing J4+ sources. `import-j4-dts.py` imports
the pinned Rev06 board DTS and its panel descriptions from the J4+ Linux 4.9
source, redirects the SoC includes to the 4.19 tree's `vendor-legacy/qcom`
files, disables the nonessential `sec_ts` node, and omits Samsung MCD, BLSP1
QUP4, modem, camera, sensor, and NFC descriptions for initial bring-up. These
peripherals are not needed to reach the standalone initramfs/RNDIS test; some
legacy I2C/clock/remoteproc labels differ from the Riva 4.19 SoC DTS. These are
compatibility trims for the first port build, not conclusions about the earlier
K49 reset or a final hardware configuration.
In particular, Redmi's Android build uses a 2,048-byte boot page and a
67,108,864-byte BOOT partition;
this project must retain the J4+ BOOT limit of 33,554,432 bytes and use the
J4+-appropriate boot container/header and DT payload.

## Build status and BOOT image

`build-dtb-smoke.sh` is the fast DTB-only check. `build-boot-image.sh` is the
end-to-end Linux 4.19.325 image builder: it configures the Android 16 Riva
`vendor/msm8937-perf_defconfig`, applies a narrowly scoped fix for a malformed
ICE diagnostic string in the pinned source, disables the GCC-11-incompatible
`FORTIFY_SOURCE` checks, optional IPA accelerator, and unpaired KernelSU
manual hooks for this bring-up build, enables and verifies the
initramfs/systemd and USB diagnostic options, builds
`Image.gz` plus the Samsung Rev06 DTB, packages
the DTB as Qualcomm QCDT v3, and repacks the pinned J4+ Android v0 boot header.
It replaces the donor's kernel and ramdisk; it does not retain the donor
pmOS-initramfs or its root UUID command line.

The ramdisk is a standalone Ubuntu 22.04 arm64 systemd 249 diagnostic initramfs
with an emergency shell and optional RNDIS/USB-ACM access. The command line is
filtered to remove inherited `root=`, `init=`, and pmOS root-selection tokens.
The package step rejects an image larger than 33,554,432 bytes and records its
SHA-256. GitHub Actions uploads the image with build, config, DTB, and header
verification reports. A successful build proves packaging consistency only;
it is not evidence that the kernel boots on hardware. Do not flash without a
separate review and backup of the original BOOT partition.
