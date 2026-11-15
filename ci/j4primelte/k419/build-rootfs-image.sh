#!/usr/bin/env bash
# Populate a labeled ext4 image with the official Arch Linux ARM rootfs.
set -euo pipefail

if [[ $# -lt 2 || $# -gt 3 ]]; then
  echo "usage: $0 <ArchLinuxARM-aarch64.tar.gz> <output-rootfs.img> [size-MiB]" >&2
  exit 2
fi
[[ $EUID -eq 0 ]] || { echo "run as root (CI uses sudo) to preserve rootfs ownership and metadata" >&2; exit 1; }

HERE=$(cd -- "$(dirname -- "$0")" && pwd)
# shellcheck disable=SC1091
source "$HERE/sources.env"
archive=$(realpath "$1")
out=$(realpath -m "$2")
size_mib=${3:-${ARCHLINUXARM_ROOTFS_IMAGE_MIB:?missing ARCHLINUXARM_ROOTFS_IMAGE_MIB}}
[[ $size_mib =~ ^[0-9]+$ ]] && ((size_mib > 0)) || {
  echo "image size must be a positive integer MiB value" >&2
  exit 2
}
[[ -s $archive ]] || { echo "rootfs archive is missing or empty: $archive" >&2; exit 1; }
for tool in truncate mkfs.ext4 tune2fs losetup mount umount tar df e2fsck; do
  command -v "$tool" >/dev/null || { echo "required tool is missing: $tool" >&2; exit 1; }
done
mkdir -p "$(dirname "$out")"
root=$(mktemp -d "${TMPDIR:-/tmp}/j4-arch-root.XXXXXX")
mountpoint=$(mktemp -d "${TMPDIR:-/tmp}/j4-arch-mount.XXXXXX")
loopdev=''
mounted=0
cleanup() {
  if ((mounted)); then umount "$mountpoint" || true; fi
  if [[ -n $loopdev ]]; then losetup -d "$loopdev" || true; fi
  rm -rf "$root" "$mountpoint"
}
trap cleanup EXIT

# Extract as root, preserving ownership and any archived ACLs/xattrs.
tar --numeric-owner --same-owner --xattrs --acls --selinux --sparse \
  -xpf "$archive" -C "$root"
[[ -x "$root/sbin/init" ]] || { echo "rootfs archive has no executable /sbin/init" >&2; exit 1; }

image_bytes=$((size_mib * 1024 * 1024))
if (( image_bytes > J4_ARCH_SYSTEM_PARTITION_BYTES )); then
  echo "rootfs image ($image_bytes bytes) exceeds the recorded /system size ($J4_ARCH_SYSTEM_PARTITION_BYTES bytes)" >&2
  exit 1
fi
partition_slack=$((J4_ARCH_SYSTEM_PARTITION_BYTES - image_bytes))
truncate -s "$image_bytes" "$out"
mkfs.ext4 -F -b 4096 -m 0 -L "$ARCHLINUXARM_ROOT_LABEL" "$out" >/dev/null

# The image is intended for the J4+ 4.19 kernel. Clear newer ext4 features
# from the host mkfs defaults when present before mounting/populating it.
features=$(tune2fs -l "$out" | sed -n 's/^Filesystem features:[[:space:]]*//p')
for feature in orphan_file metadata_csum_seed fast_commit; do
  if grep -qw "$feature" <<< "$features"; then
    tune2fs -O "^$feature" "$out" >/dev/null
  fi
done

loopdev=$(losetup --find --show "$out")
mount "$loopdev" "$mountpoint"
mounted=1
cp -a "$root"/. "$mountpoint"/

# Leave room for first boot logs, pacman metadata and updates. Refuse to emit
# an image that is nearly full or cannot fit the extracted userspace.
read -r _ total used available _ < <(df -P -B1 "$mountpoint" | tail -n 1)
minimum_free=$((image_bytes / 20))
if (( available < minimum_free )); then
  echo "Arch rootfs leaves too little free space: $available bytes available, need at least $minimum_free" >&2
  exit 1
fi
sync
umount "$mountpoint"
mounted=0
losetup -d "$loopdev"
loopdev=''
e2fsck -fn "$out" >/dev/null

printf 'rootfs image: %s\nfilesystem: ext4 label=%s\nimage size: %s bytes (%s MiB)\nslack vs recorded /system size: %s bytes\nfree space after extraction: %s bytes\n' \
  "$out" "$ARCHLINUXARM_ROOT_LABEL" "$image_bytes" "$size_mib" "$partition_slack" "$available"
sha256sum "$out" > "$out.sha256"
cat "$out.sha256"
