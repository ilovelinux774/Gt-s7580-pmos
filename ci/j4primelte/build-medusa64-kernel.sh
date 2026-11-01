#!/usr/bin/env bash
# Build an untested downstream ARM64 kernel for the recorded J415F Rev06.
# It builds a kernel only: it never accesses a phone, rootfs or vendor partition.
set -euo pipefail
HERE=$(cd -- "$(dirname -- "$0")" && pwd)
# shellcheck source=ci/j4primelte/medusa64-sources.env
source "$HERE/medusa64-sources.env"
KERNEL=${J4_MEDUSA64_KERNEL_TREE:?Set J4_MEDUSA64_KERNEL_TREE to the pinned 64-bit source checkout}
TOOLCHAIN=${J4_MEDUSA64_TOOLCHAIN_TREE:?Set J4_MEDUSA64_TOOLCHAIN_TREE to the pinned GCC 4.9 checkout}
OUT=${J4_MEDUSA64_OUT:?Set J4_MEDUSA64_OUT to an output directory}
TOUCH_Q=${J4_MEDUSA64_TOUCH_Q:?Set J4_MEDUSA64_TOUCH_Q to the pinned Q touchscreen firmware ihex}
JOBS=${J4_MEDUSA64_JOBS:-2}
if ! [[ "$JOBS" =~ ^[1-9][0-9]?$ ]] || (( JOBS > 16 )); then echo 'Invalid job count' >&2; exit 1; fi
[[ $(git -C "$KERNEL" rev-parse HEAD) == "$J4_MEDUSA64_KERNEL_COMMIT" ]] || { echo 'Wrong Medusa ARM64 source commit' >&2; exit 1; }
[[ $(git -C "$TOOLCHAIN" rev-parse HEAD) == "$J4_MEDUSA64_TOOLCHAIN_COMMIT" ]] || { echo 'Wrong Medusa ARM64 toolchain commit' >&2; exit 1; }
CROSS="$TOOLCHAIN/bin/aarch64-linux-android-"
test -x "${CROSS}gcc"
[[ $("${CROSS}gcc" -dumpmachine) == aarch64-linux-android ]] || { echo 'Compiler is not the expected AArch64 target' >&2; exit 1; }
printf '%s  %s\n' "$J4_MEDUSA64_TOUCH_Q_IHEX_SHA256" "$TOUCH_Q" | sha256sum -c -
command -v python2 >/dev/null || { echo 'python2 is required by this old vendor tree' >&2; exit 1; }
python3 "$HERE/medusa64-stage.py" --kernel-tree "$KERNEL" --touch-q-ihex "$TOUCH_Q"
mkdir -p "$OUT" "$OUT/firmware" "$OUT/j4-gpu-firmware"
GPU_TMP=$(mktemp -d "${TMPDIR:-/tmp}/j4-gpufw.XXXXXXXX")
trap 'rm -rf -- "$GPU_TMP"; if [[ -n "${CONFIG_OUT:-}" ]]; then rm -rf -- "$CONFIG_OUT"; fi' EXIT
# Download the exact upstream binary firmware and its redistribution notices.
# Gitiles format=TEXT is base64; verify every decoded file before staging it.
for spec in 'qcom/a300_pm4.fw:a300_pm4.fw' 'qcom/a300_pfp.fw:a300_pfp.fw' 'LICENSE.qcom:LICENSE.qcom.txt' 'qcom/NOTICE.txt:NOTICE.qcom.txt'; do
    remote=${spec%%:*}
    name=${spec#*:}
    wget -q --tries=3 --timeout=30 -O "$GPU_TMP/$name.b64" "$J4_MEDUSA64_GPU_FW_BASE/$remote?format=TEXT"
    base64 --decode "$GPU_TMP/$name.b64" > "$GPU_TMP/$name"
done
printf '%s  %s\n' "$J4_MEDUSA64_GPU_PM4_SHA256" "$GPU_TMP/a300_pm4.fw" "$J4_MEDUSA64_GPU_PFP_SHA256" "$GPU_TMP/a300_pfp.fw" "$J4_MEDUSA64_GPU_LICENSE_SHA256" "$GPU_TMP/LICENSE.qcom.txt" "$J4_MEDUSA64_GPU_NOTICE_SHA256" "$GPU_TMP/NOTICE.qcom.txt" | sha256sum -c -
for name in a300_pm4.fw a300_pfp.fw; do
    if [[ -e "$KERNEL/firmware/$name" ]] && ! cmp -s "$GPU_TMP/$name" "$KERNEL/firmware/$name"; then echo "Unexpected existing kernel firmware: $name" >&2; exit 1; fi
    # O= builds need both the source-tree incbin and the output-tree dependency.
    install -m 0644 "$GPU_TMP/$name" "$KERNEL/firmware/$name"
    install -m 0644 "$GPU_TMP/$name" "$OUT/firmware/$name"
    install -m 0644 "$GPU_TMP/$name" "$OUT/j4-gpu-firmware/$name"
done
install -m 0644 "$GPU_TMP/LICENSE.qcom.txt" "$GPU_TMP/NOTICE.qcom.txt" "$OUT/j4-gpu-firmware/"
export KBUILD_BUILD_USER=pmos-ci KBUILD_BUILD_HOST=github-actions KBUILD_BUILD_VERSION=1
export KBUILD_BUILD_TIMESTAMP="$J4_MEDUSA64_BUILD_TIMESTAMP" SOURCE_DATE_EPOCH="$J4_MEDUSA64_SOURCE_DATE_EPOCH"
# New GCC hosts need -fcommon to link this old kernel's shipped dtc parser.
# CC bypasses Qualcomm's obsolete python2 warning wrapper, matching the
# existing working ARM32 downstream build; it is still the pinned GCC 4.9.
MAKE=(make -C "$KERNEL" O="$OUT" ARCH=arm64 "CROSS_COMPILE=$CROSS" "CC=${CROSS}gcc" PYTHON=python2 "HOSTCFLAGS=-O2 -Wall -Wmissing-prototypes -Wstrict-prototypes -fomit-frame-pointer -std=gnu89 -fcommon")
# Generate the desired configuration independently before copying it into the
# output tree. Kbuild may still rebuild objects when this old tree is rerun.
CONFIG_OUT=$(mktemp -d "${TMPDIR:-/tmp}/j4-medusa64-config.XXXXXXXX")
CONFIG_MAKE=(make -C "$KERNEL" O="$CONFIG_OUT" ARCH=arm64 "CROSS_COMPILE=$CROSS" "CC=${CROSS}gcc" PYTHON=python2 "HOSTCFLAGS=-O2 -Wall -Wmissing-prototypes -Wstrict-prototypes -fomit-frame-pointer -std=gnu89 -fcommon")
"${CONFIG_MAKE[@]}" j4primelte_defconfig
CFG="$KERNEL/scripts/config"
for sym in COMPAT SYSVIPC DEVPTS_MULTIPLE_INSTANCES DEVTMPFS DEVTMPFS_MOUNT BLK_DEV_INITRD RD_GZIP RD_XZ CGROUPS TMPFS TMPFS_POSIX_ACL BINFMT_ELF BINFMT_SCRIPT EXT4_FS LBDAF INPUT_EVDEV VT VT_CONSOLE DUMMY_CONSOLE UNIX98_PTYS IKCONFIG IKCONFIG_PROC SECCOMP SECCOMP_FILTER DEBUG_FS PROC_FS SYSFS NETFILTER NETFILTER_XTABLES IP_NF_IPTABLES IP_NF_FILTER BT_HCI_SMD USB_GADGET USB_G_ANDROID USB_MSM_OTG BUILD_ARM64_APPENDED_DTB_IMAGE BUILD_ARM64_KERNEL_COMPRESSION_GZIP; do
    "$CFG" --file "$CONFIG_OUT/.config" --enable "$sym"
done
for sym in ANDROID_PARANOID_NETWORK SEC_RESTRICT_ROOTING SECURITY_DEFEX PFT KINETO_GAN USE_VFB SAMSUNG_TUI TZDEV FRAMEBUFFER_CONSOLE SECURITY_SELINUX MODULE_SIG DM_VERITY RANDOMIZE_BASE LOCALVERSION_AUTO; do
    "$CFG" --file "$CONFIG_OUT/.config" --disable "$sym"
done
"$CFG" --file "$CONFIG_OUT/.config" --set-str LOCALVERSION '-j4-medusa64-gpufw-debug'
"$CFG" --file "$CONFIG_OUT/.config" --set-str EXTRA_FIRMWARE 'a300_pm4.fw a300_pfp.fw'
"$CFG" --file "$CONFIG_OUT/.config" --set-str EXTRA_FIRMWARE_DIR 'firmware'
"${CONFIG_MAKE[@]}" olddefconfig
if ! cmp -s "$CONFIG_OUT/.config" "$OUT/.config"; then cp "$CONFIG_OUT/.config" "$OUT/.config"; fi
"${MAKE[@]}" olddefconfig
for sym in COMPAT COMPAT_BINFMT_ELF BUILD_ARM64_APPENDED_DTB_IMAGE BUILD_ARM64_KERNEL_COMPRESSION_GZIP SEC_J4PRIMELTE_PROJECT MACH_J4PRIMELTE_SEA_OPEN SYSVIPC DEVPTS_MULTIPLE_INSTANCES DEVTMPFS DEVTMPFS_MOUNT BLK_DEV_INITRD RD_GZIP BINFMT_ELF BINFMT_SCRIPT EXT4_FS USB_G_ANDROID USB_MSM_OTG PRONTO_WLAN BT_HCI_SMD MSM_KGSL FW_LOADER FIRMWARE_IN_KERNEL TOUCHSCREEN_SEC_INCELL_TS; do
    grep -qx "CONFIG_${sym}=y" "$OUT/.config" || { echo "Required config CONFIG_${sym}=y did not survive olddefconfig" >&2; exit 1; }
done
for sym in ANDROID_PARANOID_NETWORK SECURITY_SELINUX DM_VERITY FRAMEBUFFER_CONSOLE; do
    ! grep -qx "CONFIG_${sym}=y" "$OUT/.config" || { echo "Unwanted CONFIG_${sym}=y" >&2; exit 1; }
done
grep -qx 'CONFIG_EXTRA_FIRMWARE="a300_pm4.fw a300_pfp.fw"' "$OUT/.config" || { echo 'Expected A300 firmware list did not survive olddefconfig' >&2; exit 1; }
grep -qx 'CONFIG_EXTRA_FIRMWARE_DIR="firmware"' "$OUT/.config" || { echo 'Unexpected external firmware build directory' >&2; exit 1; }
"${MAKE[@]}" -j"$JOBS" Image.gz-dtb
test -s "$OUT/arch/arm64/boot/Image.gz"
test -s "$OUT/arch/arm64/boot/Image.gz-dtb"
test -s "$OUT/arch/arm64/boot/dts/samsung/msm8917/msm8917-sec-j4primelte-sea-open-r06.dtb"
printf '\nMedusa 64 compiled; selecting the recorded Rev06 DTB remains a separate packaging step.\n'
sha256sum "$OUT/arch/arm64/boot/Image.gz" "$OUT/arch/arm64/boot/dts/samsung/msm8917/msm8917-sec-j4primelte-sea-open-r06.dtb" "$OUT/.config"
