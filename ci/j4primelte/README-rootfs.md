# Root filesystem for a 3.18 kernel: what works, what does not, and why

This note exists so the next person does not have to rediscover it from a black
screen. It is about the userspace, not the kernel.

## The problem in one paragraph

Arch Linux ARM aarch64 ships glibc. glibc's `--enable-kernel` sets a **minimum
kernel version baked into the binary at build time**; if the running kernel is
older, `_dl_start` prints `Fatal: kernel too old` and exits. Current Arch Linux
ARM is built for a kernel well above 3.7, and this phone's kernel is
**3.18.120**. That means `/usr/lib/systemd/systemd`, `/usr/bin/bash`, `pacman`,
`ssh`, `ip` — every dynamically linked Arch binary — aborts before `main()`.
Nothing in userspace can raise the floor: it is not a runtime check that a
configuration file can disable.

musl does not work this way. It targets the syscalls the kernel provides, and
Alpine's musl userspace runs on kernels this old. **So the boot path has to be
musl, and the only realistically available musl aarch64 userspace is Alpine.**

## What this image therefore is

One ext4 filesystem, label `ARCHROOT`, with two userspaces on it:

| path | contents | runs on 3.18? |
|---|---|---|
| `/` | Arch Linux ARM aarch64 (glibc) | no, for dynamic binaries |
| `/alpine` | Alpine aarch64 (musl) + OpenRC | yes |
| `/sbin/init`, `/usr/bin/openrc-init` | symlinks into `/alpine` | yes |

The initramfs hook prefers `/usr/lib/systemd/systemd`, then
`/usr/bin/openrc-init`. `build-rootfs.sh` **moves Arch's systemd binary aside**
(`/alpine/systemd.arch`) so the hook cannot pick the one candidate that is
guaranteed to fail. `audit-rootfs.py` re-checks the final tree and fails the
build if the boot path is glibc.

Arch stays at `/` on purpose: its package database, `/usr/lib` and its tooling
remain intact for when a newer kernel exists. The two trees do not share
libraries, so there is no ABI conflict; the cost is roughly 600 MB of the
partition.

## Honest list of what is still unproven

* **No musl userspace has ever run on this kernel.** The claim that it will is
  based on musl's design, on Alpine's published kernel support, and on the fact
  that the *initramfs* on this phone already runs a static musl BusyBox
  successfully (the reference ramdisk links against `ld-musl-armhf.so.1`). That
  last point is real evidence for musl on 3.18, but for a *static* binary.
* **`switch_root` on this kernel is untested.** It needs `mount --move` of
  `/dev`, `/proc`, `/sys` to behave; the 3.18 kernel here has
  `CONFIG_DEVTMPFS=y` and `CONFIG_DEVTMPFS_MOUNT=y`, which is the usual
  prerequisite.
* **OpenRC as PID 1 under musl needs `/dev/console` and cgroups** to exist the
  way it expects. The initramfs establishes both; nothing in CI can prove the
  handover.

## Where the root filesystem goes

The image is a **bare ext4 filesystem**, not a partition table: write it to a
partition (the phone's `system` / `userdata` partition, whichever is unused and
large enough) and the partition itself carries `LABEL=ARCHROOT`. The initramfs
finds it with `blkid -s LABEL`, exactly like the pmOS init does for its own
`pmOS_root`.

The recorded `/system` size on this handset is 2,617,245,696 bytes
(`pins.env: J4_SYSTEM_PARTITION_BYTES`); the default image is 2400 MiB, leaving
~96 MiB of slack. `build-rootfs.sh` refuses to build larger than the recorded
partition, and refuses to emit an image with less than 5 % free space.

**This image replaces whatever is on that partition.** It does not touch the
pmOS rootfs the owner is keeping, and it does not touch BOOT. Boot a known-good
BOOT image and reflash that partition to undo it.

## Filesystem features

`build-rootfs.sh` clears `metadata_csum`, `metadata_csum_seed`, `meta_bg`,
`orphan_file`, `fast_commit` and `64bit`, then verifies with `dumpe2fs` and
fails if any survived. 3.18's ext4 has no idea what `metadata_csum` is: it will
not mount the filesystem at all, and the initramfs will report
`ERROR: unable to mount root partition!` while looking perfectly healthy.

## Reproducing the image

CI does this; locally it needs root for the loop mount:

```sh
sudo apt-get install -y e2fsprogs
curl -LO "$(grep ARCHLINUXARM_ROOTFS_URL ci/j4primelte/pins.env | cut -d"'" -f2)"
curl -LO "https://dl-cdn.alpinelinux.org/alpine/latest-stable/releases/aarch64/alpine-minirootfs-latest-aarch64.tar.gz"
sudo ALPINE_BOOTSTRAP=$PWD/alpine-minirootfs-latest-aarch64.tar.gz \
     bash ci/j4primelte/build-rootfs.sh \
     ArchLinuxARM-aarch64-latest.tar.gz rootfs.img 2400
```

Neither tarball is committed: together they are well over 200 MB, and the
repository is not the place for them. CI downloads them fresh and records their
hashes in the job summary.
