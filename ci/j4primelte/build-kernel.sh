#!/usr/bin/env bash
set -euo pipefail
ROOT=$(git rev-parse --show-toplevel)
# shellcheck source=ci/j4primelte/sources.env
source "$ROOT/ci/j4primelte/sources.env"
KERNEL="$HOME/j4-kernel"
TC="$HOME/j4-toolchain"
LOGS="$ROOT/artifacts/logs"
mkdir -p "$LOGS"
cp "$ROOT/ci/j4primelte/sources.env" "$LOGS/sources.env"

checkout_pin() {
    local url=$1 sha=$2 dest=$3
    git init "$dest"
    git -C "$dest" remote add origin "$url"
    git -C "$dest" fetch --depth=1 origin "$sha"
    git -C "$dest" checkout --detach FETCH_HEAD
    test "$(git -C "$dest" rev-parse HEAD)" = "$sha"
}
checkout_pin "$KERNEL_URL" "$KERNEL_COMMIT" "$KERNEL"
checkout_pin "$TOOLCHAIN_URL" "$TOOLCHAIN_COMMIT" "$TC"
CROSS="$TC/bin/arm-linux-androideabi-"
"${CROSS}gcc" --version
cd "$KERNEL"

# Bluetooth: this kernel enables CONFIG_BT but no HCI transport at all, so the
# WCNSS (WCN3620) Bluetooth core has no device. Stage drivers/bluetooth/hci_smd.c,
# which carries HCI over the APPS_RIVA_BT_CMD / APPS_RIVA_BT_ACL SMD channels.
python3 "$ROOT/ci/j4primelte/kernel-bt.py" "$KERNEL"

# This pinned source already removed DTC's duplicate lexer yylloc definition.
# Do not attempt to reapply an obsolete compiler-compatibility patch.

# Explicitly use ARM/j4primelte. Do not fall back to a generic SoC or ARM64 config.
MAKE=(make O=out ARCH=arm "CROSS_COMPILE=$CROSS" "CC=${CROSS}gcc" PYTHON=python2)
"${MAKE[@]}" j4primelte_defconfig
for flag in DEVTMPFS DEVTMPFS_MOUNT SYSVIPC DEVPTS_MULTIPLE_INSTANCES \
    BLK_DEV_INITRD RD_GZIP RD_XZ CGROUPS TMPFS TMPFS_POSIX_ACL \
    BINFMT_ELF BINFMT_SCRIPT EXT4_FS LBDAF INPUT_EVDEV \
    VT VT_CONSOLE DUMMY_CONSOLE UNIX98_PTYS IKCONFIG IKCONFIG_PROC \
    SECCOMP SECCOMP_FILTER DEBUG_FS PROC_FS SYSFS \
    NETFILTER NETFILTER_XTABLES IP_NF_IPTABLES IP_NF_FILTER \
    BT_HCI_SMD USB_GADGET USB_G_ANDROID USB_MSM_OTG; do
    scripts/config --file out/.config --enable "$flag"
done
# Keep Samsung's MDSS panel driver, but avoid attaching fbcon during initial
# headless bring-up. No unverified MDP3/MDP5 framebuffer patches are applied.
for flag in ANDROID_PARANOID_NETWORK SEC_RESTRICT_ROOTING SECURITY_DEFEX \
    PFT KINETO_GAN USE_VFB SAMSUNG_TUI TZDEV FRAMEBUFFER_CONSOLE \
    SECURITY_SELINUX MODULE_SIG DM_VERITY; do
    scripts/config --file out/.config --disable "$flag"
done
scripts/config --file out/.config --set-str LOCALVERSION '-pmos-j4-debug'
scripts/config --file out/.config --disable LOCALVERSION_AUTO
"${MAKE[@]}" olddefconfig

for flag in SEC_J4PRIMELTE_PROJECT MACH_J4PRIMELTE_SEA_OPEN BUILD_ARM_APPENDED_DTB_IMAGE \
    DEVTMPFS SYSVIPC DEVPTS_MULTIPLE_INSTANCES BLK_DEV_INITRD RD_GZIP \
    USB_G_ANDROID USB_MSM_OTG EXT4_FS IP_NF_IPTABLES IP_NF_FILTER \
    BT_HCI_SMD; do
    grep -qx "CONFIG_${flag}=y" out/.config || { echo "Required option missing: $flag"; exit 1; }
done
if grep -qx 'CONFIG_ANDROID_PARANOID_NETWORK=y' out/.config; then
    echo 'Android network permission checks still enabled'; exit 1
fi
cp out/.config "$LOGS/kernel.config"
# Keep the staged Bluetooth driver with the other build provenance.
cp "$ROOT/ci/j4primelte/kernel/hci_smd.c" "$LOGS/hci_smd.c"
git diff > "$LOGS/kernel-source.patch"
export KBUILD_BUILD_USER=pmos-ci KBUILD_BUILD_HOST=github-actions
export KBUILD_BUILD_TIMESTAMP="$(git show -s --format=%cD HEAD)"
"${MAKE[@]}" -j"$(nproc)"
# The vendor boot Makefile discovers DTBs at parse time. Re-enter it after
# dtbs are built so a fresh parallel build cannot omit the appended trees.
"${MAKE[@]}" -j"$(nproc)" dtbs
"${MAKE[@]}" zImage-dtb
test -s out/arch/arm/boot/zImage-dtb
find out/arch/arm/boot -name '*j4primelte-sea*.dtb' -print | tee "$LOGS/dtbs.txt"
test -s "$LOGS/dtbs.txt"
python3 - <<'PY'
from pathlib import Path
folder = Path('out/arch/arm/boot')
z = (folder / 'zImage').read_bytes()
combined = (folder / 'zImage-dtb').read_bytes()
assert combined.startswith(z), 'Appended image does not start with zImage'
assert combined[len(z):len(z)+4] == bytes.fromhex('d00dfeed'), 'Appended DTB missing'
PY

STAGING="$HOME/j4-kernel-modules"
mkdir -p "$STAGING/lib/modules"
if grep -qx 'CONFIG_MODULES=y' out/.config; then
    "${MAKE[@]}" "INSTALL_MOD_PATH=$STAGING" modules_install
fi
find "$STAGING/lib/modules" -type l \( -name build -o -name source \) -delete
DEST="$ROOT/pmaports/device/downstream/linux-samsung-j4primelte"
cp out/arch/arm/boot/zImage-dtb "$DEST/"
cp out/include/config/kernel.release "$DEST/"
cp out/.config "$DEST/kernel.config"
tar -czf "$DEST/kernel-modules.tar.gz" -C "$STAGING/lib" modules
cp "$ROOT/ci/j4primelte/sources.env" "$LOGS/sources.env"
printf 'repository_commit=%s\n' "$(git -C "$ROOT" rev-parse HEAD)" >> "$LOGS/sources.env"
printf 'kernel_release=%s\n' "$(cat "$DEST/kernel.release")" >> "$LOGS/sources.env"
sha256sum "$DEST/zImage-dtb" "$DEST/kernel.config" > "$LOGS/kernel-sha256.txt"
