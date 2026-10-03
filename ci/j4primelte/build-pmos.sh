#!/usr/bin/env bash
set -euo pipefail
ROOT=$(git rev-parse --show-toplevel)
# shellcheck source=ci/j4primelte/sources.env
source "$ROOT/ci/j4primelte/sources.env"
PMB="$HOME/j4-pmbootstrap"
PORTS="$HOME/j4-pmaports"
WORK="$HOME/j4-pmos-work"
CFG="$HOME/j4-pmbootstrap.cfg"
IMAGES="$ROOT/artifacts/images"
LOGS="$ROOT/artifacts/logs"
mkdir -p "$IMAGES" "$LOGS"
# The workflow prepares and checks this environment BEFORE compiling Linux.
test -f "$WORK/j4-preflight-pins" || {
    echo 'Run ci/j4primelte/prepare-pmos.sh first'; exit 1;
}
EXPECTED=$(printf '%s\n' "$PMBOOTSTRAP_COMMIT" "$PMAPORTS_COMMIT" "$PMAPORTS_CHANNELS_COMMIT")
test "$(cat "$WORK/j4-preflight-pins")" = "$EXPECTED"
test "$(git -C "$PORTS" rev-parse HEAD)" = "$PMAPORTS_COMMIT"
# Copy the actual compiler outputs, not just the metadata staged by preflight.
cp -a "$ROOT/pmaports/device/downstream/"* "$PORTS/device/downstream/"
cp "$ROOT/ci/j4primelte/sources.env" "$LOGS/sources.env"
printf 'image_repository_commit=%s\n' "$(git -C "$ROOT" rev-parse HEAD)" >> "$LOGS/sources.env"
PM=(python3 "$PMB/pmbootstrap.py" -c "$CFG" --details-to-stdout --assume-yes)
"${PM[@]}" checksum linux-samsung-j4primelte
"${PM[@]}" checksum device-samsung-j4primelte
"${PM[@]}" build --arch armv7 linux-samsung-j4primelte
"${PM[@]}" build --arch armv7 device-samsung-j4primelte
# Public test image, not a secret password. Root USB login is made explicitly
# passwordless in the FINAL IMAGE, after pmbootstrap's root-locking step.
"${PM[@]}" install --password j4debug --no-recommends --no-sshd --no-firewall \
    --single-partition --no-sparse
"${PM[@]}" chroot --rootfs -- apk info -vv > "$LOGS/installed-packages.txt"
"${PM[@]}" export --no-install "$HOME/j4-export"
cp -L "$HOME/j4-export/boot.img" "$IMAGES/boot.img"
cp -L --sparse=always "$HOME/j4-export/samsung-j4primelte.img" "$IMAGES/rootfs.img"
# Every build creates a fresh ext4 with a new UUID. Pin one UUID for all builds
# so a boot.img and a rootfs.img from different runs can still be mixed.
sudo "$(command -v python3)" "$ROOT/ci/j4primelte/fix-root-uuid.py" "$IMAGES"

MOUNT="$HOME/j4-image-mount"
mkdir -p "$MOUNT"
cleanup() {
    if mountpoint -q "$MOUNT"; then sudo umount "$MOUNT"; fi
}
trap cleanup EXIT
sudo mount -o loop "$IMAGES/rootfs.img" "$MOUNT"
sudo "$(command -v python3)" "$ROOT/ci/j4primelte/customize-rootfs.py" \
    "$MOUNT" "$ROOT/ci/j4primelte/rootfs"
sync
sudo umount "$MOUNT"
trap - EXIT

sudo tune2fs -l "$IMAGES/rootfs.img" | tee "$LOGS/rootfs-superblock.txt" >/dev/null
FEATURES=$(grep 'Filesystem features:' "$LOGS/rootfs-superblock.txt")
for unsupported in metadata_csum metadata_csum_seed orphan_file 64bit meta_bg; do
    if grep -qw "$unsupported" <<< "$FEATURES"; then
        echo "Unexpected ext4 feature: $unsupported"; exit 1
    fi
done
python3 "$ROOT/ci/j4primelte/verify-images.py" "$IMAGES" | tee "$LOGS/image-validation.txt"

# Flashing only the kernel is the common case; ship it separately so it does
# not require downloading the whole rootfs archive.
BOOT_ONLY="$ROOT/artifacts/boot-only"
mkdir -p "$BOOT_ONLY"
cp "$IMAGES/boot.img" "$IMAGES/boot-debug.img" "$IMAGES/image-info.json" "$BOOT_ONLY/"
(
    cd "$BOOT_ONLY"
    sha256sum boot.img boot-debug.img > SHA256SUMS
)
cp "$ROOT/README-j4primelte.md" "$IMAGES/README.md"
cp "$LOGS/sources.env" "$LOGS/kernel.config" "$LOGS/installed-packages.txt" "$IMAGES/"
cp "$LOGS/kernel-source.patch" "$LOGS/pmbootstrap.patch" "$LOGS/kernel-build-provenance.json" "$IMAGES/"
(
    cd "$IMAGES"
    sha256sum rootfs.img > ROOTFS-UNCOMPRESSED.sha256
    xz -T2 -2 rootfs.img
    sha256sum boot.img boot-debug.img rootfs.img.xz > SHA256SUMS
)
printf '\nBuilt TEST images; boot, display, and Wi-Fi are NOT hardware-validated.\n'
ls -lh "$IMAGES"
