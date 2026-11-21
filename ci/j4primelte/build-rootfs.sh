#!/usr/bin/env bash
# Build the ext4 root filesystem the J4+ boots into from a 3.18 kernel.
#
#   sudo ALPINE_BOOTSTRAP=alpine-minirootfs.tar.gz \
#        bash ci/j4primelte/build-rootfs.sh ArchLinuxARM-aarch64.tar.gz rootfs.img [size-MiB]
#
# Two things make this different from writing the Arch ARM tarball to a
# partition:
#
# 1. The filesystem has to be mountable by Linux 3.18. Modern e2fsprogs defaults
#    add metadata_csum, which 3.18 refuses outright. The feature set is checked,
#    cleared and then re-checked, and the build fails if anything unsafe remains.
#
# 2. Arch Linux ARM's glibc needs a newer kernel than 3.18 for dynamically
#    linked binaries; its /usr/bin/bash would abort with "Fatal: kernel too
#    old". The image therefore also carries an Alpine/musl tree, which is
#    configured at runtime rather than against a kernel-floor, and the boot
#    path is pointed at it. Arch stays at / for its packages and tooling.
#    See ci/j4primelte/README-rootfs.md.
set -euo pipefail

if [[ $# -lt 2 || $# -gt 3 ]]; then
  echo "usage: [ALPINE_BOOTSTRAP=...] $0 <ArchLinuxARM-aarch64.tar.gz> <rootfs.img> [size-MiB]" >&2
  exit 2
fi
if [[ $EUID -ne 0 ]]; then
  echo "run as root: original ownership and modes have to survive into the image" >&2
  exit 1
fi

HERE=$(cd -- "$(dirname -- "$0")" && pwd)
# shellcheck disable=SC1091
source "$HERE/pins.env"

archive=$(realpath "$1")
out=$(realpath -m "$2")
size_mib=${3:-$ARCHLINUXARM_ROOTFS_IMAGE_MIB}
label=$ARCHLINUXARM_ROOT_LABEL
bootstrap=${ALPINE_BOOTSTRAP:-}

[[ -s $archive ]] || { echo "missing Arch Linux ARM tarball: $archive" >&2; exit 1; }
[[ $size_mib =~ ^[0-9]+$ ]] || { echo "size must be a MiB integer" >&2; exit 1; }
for tool in truncate mkfs.ext4 tune2fs dumpe2fs losetup mount umount tar e2fsck python3; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 1; }
done

image_bytes=$((size_mib * 1024 * 1024))
if (( image_bytes > J4_SYSTEM_PARTITION_BYTES )); then
  echo "rootfs ($image_bytes B) does not fit the recorded system partition" \
       "($J4_SYSTEM_PARTITION_BYTES B)" >&2
  exit 1
fi

root=$(mktemp -d "${TMPDIR:-/tmp}/j4-root.XXXXXX")
mnt=$(mktemp -d "${TMPDIR:-/tmp}/j4-mnt.XXXXXX")
loopdev=''
mounted=0
cleanup() {
  if (( mounted )); then umount "$mnt" || true; fi
  if [[ -n $loopdev ]]; then losetup -d "$loopdev" || true; fi
  rm -rf "$root" "$mnt"
}
trap cleanup EXIT

echo "== extracting Arch Linux ARM =="
tar --numeric-owner --same-owner --xattrs --acls --selinux --sparse -xpf "$archive" -C "$root"
[[ -x "$root/usr/bin/bash" ]] || { echo "tarball has no /usr/bin/bash" >&2; exit 1; }

echo "== boot-path audit (before) =="
# Informational here: a fresh Arch tarball is glibc-only by definition, and
# that is exactly why the Alpine boot path below exists. The hard gate runs
# against the finished tree instead.
python3 "$HERE/audit-rootfs.py" "$root" --allow-glibc-boot || true

if [[ -n $bootstrap ]]; then
  [[ -s $bootstrap ]] || { echo "missing Alpine bootstrap: $bootstrap" >&2; exit 1; }
  echo "== installing the Alpine/musl boot path =="
  mkdir -p "$root/alpine"
  tar --numeric-owner -xzf "$bootstrap" -C "$root/alpine"
  [[ -x "$root/alpine/sbin/apk" ]] || { echo "bootstrap has no /sbin/apk" >&2; exit 1; }
  mkdir -p "$root/alpine"/{dev,proc,sys,run,tmp,var/log}
  chmod 1777 "$root/alpine/tmp"

  # The hook in the initramfs prefers /usr/bin/openrc-init, then /sbin/init.
  # Anything already at those paths is Arch's own and is preserved, not deleted.
  for entry in sbin/init usr/bin/openrc-init; do
    mkdir -p "$root/$(dirname "$entry")"
    if [[ -e "$root/$entry" && ! -L "$root/$entry" ]]; then
      mv "$root/$entry" "$root/alpine/$(basename "$entry").arch"
      echo "  preserved Arch /$entry as /alpine/$(basename "$entry").arch"
    fi
    ln -sfn "/alpine/$entry" "$root/$entry"
    echo "  /$entry -> /alpine/$entry"
  done

  # Arch ships systemd, and the hook tries systemd before OpenRC. Leaving it
  # there would pick the one glibc binary that cannot run. Move it aside rather
  # than delete it: Arch's own package database still expects the file.
  if [[ -e "$root/usr/lib/systemd/systemd" && ! -L "$root/usr/lib/systemd/systemd" ]]; then
    mv "$root/usr/lib/systemd/systemd" "$root/alpine/systemd.arch"
    echo "  moved Arch systemd aside (the hook would have chosen it over OpenRC)"
  fi

  # The services have to exist inside the tree that runs.
  install -D -m 0755 "$HERE/rootfs/j4-rndis" "$root/alpine/usr/libexec/j4-rndis"
  install -D -m 0755 "$HERE/rootfs/j4-rndis.initd" "$root/alpine/etc/init.d/j4-rndis"
  mkdir -p "$root/alpine/etc/runlevels/default"
  ln -sfn /etc/init.d/j4-rndis "$root/alpine/etc/runlevels/default/j4-rndis"
  install -D -m 0644 "$HERE/rootfs/fstab" "$root/etc/fstab"
else
  echo "== no ALPINE_BOOTSTRAP: the boot path is whatever the tarball shipped =="
  echo "   Arch Linux ARM glibc is not expected to start on a 3.18 kernel."
fi

echo "== boot-path audit (after) =="
python3 "$HERE/audit-rootfs.py" "$root" || {
  echo "refusing to build an image whose PID 1 cannot start on this kernel" >&2
  exit 1
}

echo "== creating the ext4 image =="
mkdir -p "$(dirname "$out")"
truncate -s "$image_bytes" "$out"
# Features a 3.18 kernel will refuse to mount. This is the single list: it is
# passed to mkfs, cleared again afterwards in case the host's mke2fs defaults
# ignored it, and then used as the final gate below. "64bit" is deliberately
# NOT here: ext4 64-bit block numbers exist since 2.6.28 and 3.18 handles them.
# (Removing that feature also needs a full resize2fs pass, which is not worth
# the risk.)
UNSUPPORTED_FEATURES=(metadata_csum metadata_csum_seed meta_bg orphan_file fast_commit)
# resize_inode and meta_bg are mutually exclusive, and ^meta_bg already keeps
# it off, so only the features that need no companion option are filtered here.
mkfs.ext4 -F -b 4096 -m 1 -L "$label" \
  -O ^metadata_csum,^metadata_csum_seed,^orphan_file,^fast_commit "$out" >/dev/null
for feature in "${UNSUPPORTED_FEATURES[@]}"; do
  if dumpe2fs -h "$out" 2>/dev/null | grep -qw "$feature"; then
    echo "  clearing feature a 3.18 kernel cannot mount: $feature"
    tune2fs -O "^$feature" "$out" >/dev/null || true
  fi
done

loopdev=$(losetup --find --show "$out")
mount "$loopdev" "$mnt"
mounted=1
cp -a "$root"/. "$mnt"/
read -r _ _ _ available _ < <(df -P -B1 "$mnt" | tail -n 1)
minimum_free=$((image_bytes / 20))
if (( available < minimum_free )); then
  echo "image leaves too little room: $available < $minimum_free" >&2
  exit 1
fi
sync
umount "$mnt"
mounted=0
losetup -d "$loopdev"
loopdev=''

echo "== verifying the image =="
e2fsck -fn "$out" >/dev/null || { echo "e2fsck rejected the image" >&2; exit 1; }
features=$(dumpe2fs -h "$out" 2>/dev/null | sed -n 's/^Filesystem features:[[:space:]]*//p')
echo "features: $features"
for bad in "${UNSUPPORTED_FEATURES[@]}"; do
  if grep -qw "$bad" <<<"$features"; then
    echo "3.18 cannot mount this image: $bad is set" >&2
    exit 1
  fi
done
echo "all ${#UNSUPPORTED_FEATURES[@]} unsafe features are absent"
echo "label: $(dumpe2fs -h "$out" 2>/dev/null | sed -n 's/^Filesystem volume name:[[:space:]]*//p')"
echo "free space: $available bytes of $image_bytes"
sha256sum "$out" > "$out.sha256"
cat "$out.sha256"
echo "build-rootfs: ok"
