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
checkout_pin() {
    local url=$1 sha=$2 dest=$3
    git init "$dest"
    git -C "$dest" remote add origin "$url"
    git -C "$dest" fetch --depth=1 origin "$sha"
    git -C "$dest" checkout --detach FETCH_HEAD
    test "$(git -C "$dest" rev-parse HEAD)" = "$sha"
}
checkout_pin "$PMBOOTSTRAP_URL" "$PMBOOTSTRAP_COMMIT" "$PMB"
checkout_pin "$PMAPORTS_URL" "$PMAPORTS_COMMIT" "$PORTS"
mkdir -p "$PORTS/device/downstream"
cp -a "$ROOT/pmaports/device/downstream/"* "$PORTS/device/downstream/"
python3 "$ROOT/ci/j4primelte/patch-pmbootstrap.py" "$PMB"
git -C "$PMB" diff > "$LOGS/pmbootstrap.patch"

cat > "$CFG" <<EOF
[pmbootstrap]
aports = $PORTS
work = $WORK
device = samsung-j4primelte
ui = console
user = user
hostname = j4-debug
service_manager = openrc
timezone = Europe/Istanbul
locale = C.UTF-8
extra_space = 128
build_pkgs_on_install = False
ssh_keys = False
[providers]
[mirrors]
alpine = https://dl-cdn.alpinelinux.org/alpine/
pmaports = https://mirror.postmarketos.org/postmarketos/
EOF
# Initialize a new CI work directory without interactive init or ignored errors.
mkdir -p "$WORK/cache_git"
python3 - "$PMB" "$WORK" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import pmb.config
(Path(sys.argv[2]) / 'version').write_text(f'{pmb.config.work_version}\n')
PY
PM=(python3 "$PMB/pmbootstrap.py" -c "$CFG" --details-to-stdout --assume-yes)
"${PM[@]}" work_migrate
"${PM[@]}" status > "$LOGS/pmbootstrap-status.txt"
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

sudo tune2fs -l "$IMAGES/rootfs.img" > "$LOGS/rootfs-superblock.txt"
FEATURES=$(grep 'Filesystem features:' "$LOGS/rootfs-superblock.txt")
for unsupported in metadata_csum metadata_csum_seed orphan_file 64bit meta_bg; do
    if grep -qw "$unsupported" <<< "$FEATURES"; then
        echo "Unexpected ext4 feature: $unsupported"; exit 1
    fi
done
python3 "$ROOT/ci/j4primelte/verify-images.py" "$IMAGES" | tee "$LOGS/image-validation.txt"
cp "$ROOT/README-j4primelte.md" "$IMAGES/README.md"
cp "$LOGS/sources.env" "$LOGS/kernel.config" "$LOGS/installed-packages.txt" "$IMAGES/"
cp "$LOGS/kernel-source.patch" "$LOGS/pmbootstrap.patch" "$IMAGES/"
(
    cd "$IMAGES"
    sha256sum rootfs.img > ROOTFS-UNCOMPRESSED.sha256
    xz -T2 -2 rootfs.img
    sha256sum boot.img boot-debug.img rootfs.img.xz > SHA256SUMS
)
printf '\nBuilt TEST images; boot, display, and Wi-Fi are NOT hardware-validated.\n'
ls -lh "$IMAGES"
