#!/usr/bin/env bash
# Build a standalone, conservative-ext4 AArch64 J4 rootfs image from a pmbootstrap tree.
set -euo pipefail
HERE=$(cd -- "$(dirname -- "$0")" && pwd)
SRC=${1:?usage: make-aarch64-rootfs-image.sh ROOTFS_TREE OUTPUT_DIR}
OUT=${2:?usage: make-aarch64-rootfs-image.sh ROOTFS_TREE OUTPUT_DIR}
MARGIN_MB=${J4_ROOTFS_MARGIN_MB:-256}
UUID=86e7262b-b628-4b89-8a6e-e96ef9b1aab6
IMAGE="$OUT/j4primelte-aarch64-rootfs.img"
MOUNT="$OUT/mnt-rootfs"
mkdir -p "$OUT" "$MOUNT"

if [[ $EUID -ne 0 ]]; then
    echo 'Run as root (for ext4 mount and metadata preservation).' >&2
    exit 1
fi
if ! [[ "$MARGIN_MB" =~ ^[0-9]+$ ]] || (( MARGIN_MB < 128 || MARGIN_MB > 2048 )); then
    echo 'J4_ROOTFS_MARGIN_MB must be 128..2048' >&2
    exit 1
fi
if [[ ! -d "$SRC" || "$SRC" == / ]]; then
    echo 'Refusing non-directory or host-root input.' >&2
    exit 1
fi
if mountpoint -q "$SRC"; then
    echo 'Rootfs staging tree itself must not be a mountpoint.' >&2
    exit 1
fi
python3 "$HERE/prepare-aarch64-rootfs.py" "$SRC"

# Reserve 25% for package upgrades and the requested extra margin. Keep sizes
# 4-KiB aligned, then create a single ext4 filesystem (not a partition table).
USED=$(du -sx --block-size=1 "$SRC" | awk '{print $1}')
SIZE=$(( (USED * 125 / 100 + MARGIN_MB * 1024 * 1024 + 4095) / 4096 * 4096 ))
BLOCKS=$((SIZE / 4096))
truncate -s "$SIZE" "$IMAGE"
mkfs.ext4 -F -q -b 4096 -m 0 -i 8192 \
    -O '^metadata_csum,^metadata_csum_seed,^orphan_file,^64bit,^meta_bg' \
    -L pmOS_root -U "$UUID" "$IMAGE" "$BLOCKS"
mount -o loop "$IMAGE" "$MOUNT"
cleanup() {
    if mountpoint -q "$MOUNT"; then umount "$MOUNT"; fi
}
trap cleanup EXIT
rsync -aHAX --numeric-ids --delete \
    --exclude='/dev/***' --exclude='/proc/***' --exclude='/sys/***' \
    --exclude='/run/***' --exclude='/tmp/***' \
    "$SRC/" "$MOUNT/"
mkdir -p "$MOUNT/dev" "$MOUNT/proc" "$MOUNT/sys" "$MOUNT/run" "$MOUNT/tmp"
chmod 1777 "$MOUNT/tmp"
sync
umount "$MOUNT"
trap - EXIT

E2FSCHECK=$(e2fsck -fn "$IMAGE" 2>&1) || {
    printf '%s\n' "$E2FSCHECK" >&2
    echo 'e2fsck failed; refusing to publish the image.' >&2
    exit 1
}
FEATURES=$(tune2fs -l "$IMAGE" | sed -n 's/^Filesystem features:[[:space:]]*//p')
ACTUAL_UUID=$(tune2fs -l "$IMAGE" | sed -n 's/^Filesystem UUID:[[:space:]]*//p')
[[ "$ACTUAL_UUID" == "$UUID" ]] || { echo 'Root UUID mismatch' >&2; exit 1; }
for bad in metadata_csum metadata_csum_seed orphan_file 64bit meta_bg; do
    if grep -qw "$bad" <<<"$FEATURES"; then
        echo "Unsupported ext4 feature remains: $bad" >&2
        exit 1
    fi
done
printf 'ROOTFS_IMAGE_OK bytes=%s uuid=%s features=%s\n' "$(stat -c %s "$IMAGE")" "$ACTUAL_UUID" "$FEATURES"
sha256sum "$IMAGE" > "$OUT/ROOTFS.sha256"
