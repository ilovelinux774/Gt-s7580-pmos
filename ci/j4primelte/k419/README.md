# J4+ Linux 4.19.325 with Arch Linux ARM

This track builds an experimental Linux 4.19.325 kernel and boot image for the
Samsung Galaxy J4+ (SM-J415F, Rev06), plus an Arch Linux ARM aarch64 rootfs
archive. The boot image contains a small root-discovery initramfs; the full OS
lives on a separate ext4 filesystem labeled `ARCHROOT` and is not packed into
the 32 MiB BOOT partition.

## What CI produces

The `J4+ Linux 4.19 Arch Linux ARM BOOT image` workflow now produces:

* `j4primelte-k419-arch-boot.img` — kernel, J4+ QCDT, and root-switch initramfs;
* `rootfs.img` — a 2,400 MiB ext4 filesystem labeled `ARCHROOT`, populated
  from the official rootfs and checked to retain at least 5% free space;
* `ArchLinuxARM-aarch64-rootfs.tar.gz` — the upstream aarch64 archive, checked
  for AArch64 ELF executables;
* the root-switch initramfs, checksums, config, source and image reports;
* the former systemd diagnostic initramfs as a separate troubleshooting artifact.

The boot command line selects `root=LABEL=ARCHROOT rootfstype=ext4 rootwait rw`.
The initramfs waits for that filesystem, mounts it read-write, and switches to
`/sbin/init`. The kernel config requires built-in MMC, the J4+ Qualcomm MMC
host driver, and ext4. The rootfs source is the upstream rolling
`ArchLinuxARM-aarch64-latest.tar.gz`; CI records the exact downloaded archive's
SHA-256, but the upstream URL itself is not version-pinned.

A successful CI build validates source/build inputs, image structure and BOOT
size only. It does not prove hardware boot success.

## Storage and safety

The complete rootfs cannot fit in the 32 MiB BOOT image. It must be unpacked to
an ext4 partition, and that filesystem must be labeled `ARCHROOT`. The user
provided this read-only recovery output for `/system`:

* `/dev/block/by-name/system` resolves to `/dev/block/mmcblk0p47`;
* size: `2,617,245,696` bytes;
* filesystem: ext4, currently labeled `pmOS_root` with UUID
  `86e7262b-b628-4b89-8a6e-e96ef9b1aab6`;
* the user reports that a backup has already been made.

The generated `rootfs.img` is 2,400 MiB (2,516,582,400 bytes), which is
smaller than the reported 2,496 MiB partition by 96 MiB. CI populates the image
and refuses it if less than 5% of its filesystem remains free. This check does
not replace re-checking the live `/system` mapping and size immediately before
installation. Replacing `/system` removes the current pmOS/Android system files
and may make that system unbootable. CI only produces an image artifact; it
does not flash or write to the phone.

Only after the size check and backup should the filesystem be relabeled
`ARCHROOT` and the archive be extracted as root while preserving numeric
ownership, modes, symlinks and device nodes. Never run a destructive command
against a guessed `/dev/block/mmcblk0pN`.

The Arch Linux ARM rootfs archive is upstream's generic image; review its
first-login credentials and enabled services before connecting the phone to a
network. Change any default password immediately. Keep a recovery/backup path
available: the image has not yet been validated on physical J4+ hardware.

## Pinned kernel and board references

`sources.env` pins both Redmi 5A/Riva kernel generations and device-tree
references for reproducible comparison. The common MSM8917/8937-family code is
a reference, not the J4+ board configuration. Riva DTS, regulators, panel and
touch wiring, boot arguments, boot layout, and partition assumptions are not
copied as J4+ data. The J4+ Rev06 DTS is imported from its own Linux 4.9 source,
with compatibility trims documented in `import-j4-dts.py`.

The image uses the J4+ v0 boot header, a Qualcomm QCDT v3 DT payload, the J4+
4 KiB page size, and the 33,554,432-byte BOOT limit. It replaces the donor
kernel, ramdisk and command line; it does not reuse the donor's pmOS root UUID
or pmOS initramfs.

## Build locally

`build-dtb-smoke.sh` is the quick DTB-only check. The full CI workflow fetches
and validates the pinned kernel, downloads the Arch Linux ARM rootfs, builds
the root-discovery initramfs, compiles the kernel/DTB, and repacks the J4+ boot
image. The rootfs archive can also be downloaded separately from the workflow
artifact; its matching `.sha256` file is included.
