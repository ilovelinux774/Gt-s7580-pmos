#!/usr/bin/env bash
# Build a J4-oriented postmarketOS AArch64 console/USB-debug rootfs.
# This produces a rootfs image only; it never builds or flashes a kernel/BOOT.
set -euo pipefail
ROOT=$(git rev-parse --show-toplevel)
source "$ROOT/ci/j4primelte/sources.env"
BASE=${J4_AARCH64_BUILD_ROOT:-$HOME/j4-aarch64-rootfs-build}
PMB="$BASE/pmbootstrap"
PORTS="$BASE/pmaports"
WORK="$BASE/work"
CFG="$BASE/pmbootstrap.cfg"
OUT="$BASE/output"
mkdir -p "$BASE" "$OUT" "$WORK/cache_git"

checkout_pin() {
    local url=$1 sha=$2 dest=$3
    if [[ -e "$dest/.git" ]]; then
        [[ $(git -C "$dest" rev-parse HEAD) == "$sha" ]] || {
            echo "Unexpected existing checkout: $dest" >&2; exit 1;
        }
        return
    fi
    git init "$dest"
    git -C "$dest" remote add origin "$url"
    git -C "$dest" fetch --depth=1 origin "$sha"
    git -C "$dest" checkout --detach FETCH_HEAD
    [[ $(git -C "$dest" rev-parse HEAD) == "$sha" ]]
}
checkout_pin "$PMBOOTSTRAP_URL" "$PMBOOTSTRAP_COMMIT" "$PMB"
checkout_pin "$PMAPORTS_URL" "$PMAPORTS_COMMIT" "$PORTS"
git -C "$PORTS" fetch --depth=1 origin \
    "$PMAPORTS_CHANNELS_COMMIT:refs/remotes/origin/main"
[[ $(git -C "$PORTS" rev-parse HEAD) == "$PMAPORTS_COMMIT" ]]
[[ $(git -C "$PORTS" rev-parse refs/remotes/origin/main) == "$PMAPORTS_CHANNELS_COMMIT" ]]
python3 "$ROOT/ci/j4primelte/patch-pmbootstrap.py" "$PMB"

cat > "$CFG" <<EOF
[pmbootstrap]
aports = $PORTS
work = $WORK
device = qemu-aarch64
ui = console
user = user
hostname = j4-aarch64
timezone = Europe/Istanbul
locale = C.UTF-8
service_manager = openrc
extra_space = 256
build_pkgs_on_install = False
ssh_keys = False
[providers]
[mirrors]
alpine = https://dl-cdn.alpinelinux.org/alpine/
pmaports = https://mirror.postmarketos.org/postmarketos/
EOF
python3 - "$PMB" "$WORK" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import pmb.config
work = Path(sys.argv[2])
(work / "cache_git").mkdir(parents=True, exist_ok=True)
(work / "version").write_text(f"{pmb.config.work_version}\n")
PY
PM=(python3 "$PMB/pmbootstrap.py" -c "$CFG" --details-to-stdout --assume-yes)
"${PM[@]}" work_migrate
"${PM[@]}" apkbuild_parse device-qemu-aarch64 > "$OUT/device-package-parse.json"
"${PM[@]}" install --password j4temp --no-recommends --no-sshd \
    --no-firewall --single-partition --no-sparse \
    --add dropbear,dropbear-openrc,busybox-extras,iproute2,iptables,iptables-legacy \
    2>&1 | tee "$OUT/pmbootstrap-install.log"

STAGE="$WORK/chroot_rootfs_qemu-aarch64"
# Install the J4 identity before purging QEMU packages so postmarketOS package
# triggers always see deviceinfo and do not fail during cleanup.
sudo install -D -m 0644 "$ROOT/ci/j4primelte/aarch64-rootfs/deviceinfo" \
    "$STAGE/usr/share/deviceinfo/deviceinfo"
"${PM[@]}" chroot --rootfs -- apk del --no-interactive \
    device-qemu-aarch64-kernel-stable linux-stable device-qemu-aarch64 \
    postmarketos-qemu-common systemd-boot linux-firmware-none \
    2>&1 | tee "$OUT/remove-qemu-kernel.log"
"${PM[@]}" chroot --rootfs -- apk info -v | sort > "$OUT/installed-packages.txt"
"${PM[@]}" shutdown

sudo python3 "$ROOT/ci/j4primelte/prepare-aarch64-rootfs.py" "$STAGE"
sudo "$ROOT/ci/j4primelte/make-aarch64-rootfs-image.sh" "$STAGE" "$OUT"
xz -T2 -2 -k "$OUT/j4primelte-aarch64-rootfs.img"
sha256sum "$OUT/j4primelte-aarch64-rootfs.img.xz" > "$OUT/ROOTFS-XZ.sha256"
cp "$ROOT/ci/j4primelte/aarch64-rootfs/README-FIRST.txt" "$OUT/README-FIRST.txt"
cat > "$OUT/BUILD-INFO.txt" <<EOF
J4+ SM-J415F AArch64 rootfs test image
postmarketOS channel: v25.12
pmbootstrap commit: $PMBOOTSTRAP_COMMIT
pmaports commit: $PMAPORTS_COMMIT
pmaports channels metadata commit: $PMAPORTS_CHANNELS_COMMIT
Architecture: aarch64 (ELF64 AArch64, musl)
Init: Alpine/OpenRC /sbin/init
Kernel: not included; pair only with the separately delivered Medusa64 kernel/BOOT
Expected kernel release for optional modules: 3.18.120-j4-medusa64-gpufw-debug+
Kernel modules: none included in this initial rootfs test image
Root filesystem UUID: 86e7262b-b628-4b89-8a6e-e96ef9b1aab6
Filesystem: ext4 with legacy-kernel-safe feature set
USB debug: 172.16.42.1, Dropbear and telnet; non-USB ingress to ports 22/23 is blocked
UI: console only; no LXQt, Wi-Fi, Bluetooth, audio, or GPU userspace setup
Device test: not performed; this artifact has not been flashed or booted on a phone
EOF
(
    cd "$OUT"
    sha256sum j4primelte-aarch64-rootfs.img.xz README-FIRST.txt BUILD-INFO.txt installed-packages.txt > SHA256SUMS
)
printf 'AARCH64_ROOTFS_BUILD_OK\n'
ls -lh "$OUT"/j4primelte-aarch64-rootfs.img "$OUT"/j4primelte-aarch64-rootfs.img.xz
