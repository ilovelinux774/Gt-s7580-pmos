# Root filesystem for a 3.18 kernel: ARMtix, and the correction that makes it possible

This note is about the userspace, not the kernel. It replaces an earlier version
of itself that was wrong in a way that mattered, so it starts with that.

## What the earlier version claimed, and why it was wrong

The previous note said:

> Arch Linux ARM's glibc is built with a kernel floor newer than 3.18. A
> dynamically linked glibc binary aborts at startup with `Fatal: kernel too
> old`. systemd, bash, pacman — everything Arch ships — is glibc, so none of it
> can be PID 1 here. musl is configured at runtime, not against a kernel floor,
> and runs on 3.18.

The conclusion drawn from it was an image with Alpine/musl at `/alpine` and Arch
at `/`, with `/sbin/init` symlinked across trees. That is a lot of machinery to
work around a check that is not triggered.

The mechanism the note described is real; the numbers were not.

* glibc's startup check reads the `.note.ABI-tag` note (`NT_GNU_ABI_TAG`) that
  glibc's own build writes into every binary it links. If the note's version is
  newer than `uname`, `_dl_start` prints `Fatal: kernel too old` and exits
  before `main`. It really is a build-time constant with no runtime override.
* The note's value is `--enable-kernel`, **except that it can never be lower
  than the architecture minimum**. For aarch64 that minimum is **3.7.0**, set in
  glibc's `sysdeps/unix/sysv/linux/aarch64/configure.ac`
  (`arch_minimum_kernel=3.7.0`); for arm32 it is 3.2.0.
* Arch Linux ARM's `core/glibc` PKGBUILD — which ARMtix inherits — passes **no
  `--enable-kernel` at all**. Its aarch64 binaries therefore carry `ABI: 3.7.0`.

3.7.0 is below this phone's **3.18.120**, so the binaries Arch-derived
userspaces ship are allowed to start. The `Fatal: kernel too old` case appears
when a distribution *raises* the floor: upstream Arch Linux builds its glibc
with `--enable-kernel=4.4`, and a tree built against that really would die here.

You do not have to take any of this on trust. `ci/j4primelte/audit-rootfs.py`
reads the note out of the image's own binaries:

```sh
python3 ci/j4primelte/audit-rootfs.py rootfs-tree/ --kernel 3.18.120 --json-out audit.json
```

It walks every ELF in the tree (not a hand-written list of paths), prints each
one's interpreter, library kind, architecture and ABI floor, and fails when
anything on the boot path carries a floor above 3.18.120. The JSON report names
every offending file. The image build runs this and refuses to produce an image
that fails it.

RHEL's own glibc spec documents the same reasoning from the other direction: it
pins aarch64 at `--enable-kernel=3.10.0` precisely because "Gemini PDA has
3.18.x" is in the set of machines it supports.

## What this image therefore is

One ext4 filesystem, label **`ARTIXROOT`**, holding **ARMtix** — Artix Linux
built for aarch64 — with **OpenRC**, straight from the ARMtix release tarball:

| path | what it is |
|---|---|
| `/usr/bin/openrc-init` | PID 1 (ARMtix's OpenRC fork) |
| `/sbin/init` | ARMtix's own symlink, untouched |
| `/etc/init.d/j4-rndis` | this project's USB RNDIS + SSH service |
| `/usr/libexec/j4-rndis` | the script behind it |

No Alpine. No second userspace. No cross-tree symlink for the initramfs to trip
over. Artix is not a compromise here: it *is* the target, and the glibc analysis
above says it can run.

The initramfs hook tries `/usr/bin/openrc-init`, then `/sbin/openrc-init`, then
`/sbin/init`, then `/usr/bin/init`. It does not look for systemd: ARMtix does not
ship it, and an image that had it would be a different system with different
assumptions about `/run` and cgroups.

## What the build changes in the tarball

`build-rootfs.sh` extracts the tarball and touches as little as possible:

* removes `usr/lib/modules/*` and `boot/*` — the ARMtix images carry generic
  aarch64 kernels and a full firmware/kernel set this phone will never boot, and
  they alone are several hundred megabytes. `J4_KEEP_KERNELS=1` keeps them;
* creates `dev`, `proc`, `sys`, `run`, `tmp`, `var/log` if the tarball lacks
  them, and re-creates `/dev/null` and `/dev/console` when extraction had no
  root privileges to make them;
* installs the `j4-rndis` service and enables it in the `default` runlevel;
* enables `agetty.ttyMSM0` at 115200 when ARMtix ships that init script, so a
  serial cable sees a login prompt;
* writes `/etc/fstab` (nothing in it points at a partition this project must not
  write to).

It does not patch packages, does not run `pacman`, does not chroot, and does not
need an aarch64 interpreter on the build host.

## Filesystem features

The kernel is 3.18. Its ext4 has no idea what checksummed metadata is, so an
image created with modern `mke2fs` defaults fails to mount — and the initramfs
reports `ERROR: unable to mount root partition!` while the image looks healthy.

`build-rootfs.sh` passes an explicit feature list:

```
^metadata_csum,^metadata_csum_seed,^meta_bg,^orphan_file,^fast_commit,^quota,
^ea_inode,^large_dir,^casefold,^encrypt,^bigalloc
```

then reads the features back with `dumpe2fs` and fails if any of them — or
`iversion`/`dirdata`, which this `mke2fs` will not toggle — survived. `64bit`
stays on deliberately: 64-bit block numbers predate 3.18, and removing the
feature needs a `resize2fs` pass that is not worth the risk on a 2.4 GiB image.

The image is populated with `mke2fs -d <tree>`, not a loop mount: owners, modes,
symlinks, hard links and device nodes all survive, and no runner needs
`CAP_SYS_ADMIN`.

## Where the root filesystem goes

The image is a **bare ext4 filesystem**, not a partition table. Write it to a
partition (the phone's `system` / `userdata` partition, whichever is unused and
large enough) and the partition itself carries `LABEL=ARTIXROOT`. The initramfs
finds it with `blkid -s LABEL`, exactly as the pmOS init does for its own
`pmOS_root`.

The recorded `/system` size is 2,617,245,696 bytes
(`pins.env: J4_SYSTEM_PARTITION_BYTES`); the default image is 2400 MiB, leaving
~96 MiB of slack. `build-rootfs.sh` refuses to build larger than the recorded
partition, and refuses to emit an image with less than 5 % free space.

**This image replaces whatever is on that partition.** It does not touch the
pmOS rootfs the owner is keeping, and it does not touch BOOT. Boot a known-good
BOOT image and reflash that partition to undo it.

## Honest list of what is still unproven

The audit proves the userspace *can* start. It cannot prove that it does.

* **`switch_root` on this kernel is untested.** It needs `mount --move` of
  `/dev`, `/proc`, `/sys` to behave; 3.18 here has `CONFIG_DEVTMPFS=y` and
  `CONFIG_DEVTMPFS_MOUNT=y`, which is the usual prerequisite.
* **OpenRC as PID 1 needs `/dev/console`, a writable `/run` and working
  cgroups.** The initramfs establishes the first two; nothing in CI can prove
  the handover.
* **Nothing has run on the phone in this configuration yet.** The first flash
  should be `boot-debug.img`, which stops in the proven initramfs shell.

## Reproducing the image

CI does this; locally it needs root for the owner/mode/device-node reproduction
and the ext4 tooling (`e2fsprogs`):

```sh
file=$(sed -n "s/^ARTIX_IMAGE_FILE='\(.*\)'/\1/p" ci/j4primelte/pins.env)
curl -LO "https://armtix.artixlinux.org/images/$file"   # or one of the mirrors
sudo bash ci/j4primelte/build-rootfs.sh "$file" rootfs.img 2400
```

The three image mirrors (`ARTIX_IMAGE_MIRRORS` in `pins.env`) are tried in
order, because they do not all answer at the same time. Measured on
2026-11-21 from a GitHub-hosted runner: the primary answered `403`, the second
mirror failed DNS, the third did not resolve at all — while the image index and
its `sha256sums` were still readable through another path. Whichever mirror
answers must match `ARTIX_IMAGE_SHA256`.

So the tarball download is the one step of this project that depends on a third
party being reachable. If every mirror refuses, the `rootfs` job fails at that
step, names the URLs it tried and the HTTP status each returned, and leaves the
`boot` job and its artifacts untouched. To build anyway, host the tarball
somewhere the runner can reach — a release asset of this repository is the
obvious place, since the reference BOOT image is already published there — and
dispatch the workflow with:

```
artix_image_url    = <url of the copy>
artix_image_sha256 = 250abf136930f5c7d4ba1a6d2b9b56f6b32acb072e05ccb4b6ab7aa23b834699
```

The hash is checked in that path too, so a copy is as trustworthy as the
original: it is the same bytes or the build stops.

The tarball is ~1 GiB compressed and is not committed. CI checks it against
`ARTIX_IMAGE_SHA256` in `pins.env` and records the hash it saw in the job
summary; the tarball is a rolling release, so bumping the pin is a deliberate
act, not an automatic one.
