#!/usr/bin/env bash
# Build the device-specific 4.9 kernel and audit it for systemd 249 + USB debug.
set -euo pipefail

tree=${1:?usage: build-kernel.sh <kernel-tree> <out-dir>}
out=${2:?output directory}
: "${CROSS_COMPILE:=aarch64-linux-gnu-}"
jobs=${JOBS:-$(nproc)}
defconfig=j4primelte-4.9_sea_defconfig
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
touchscreen_test="$script_dir/disable-sec-ts.py"
expected_commit=5713b6504118bcee2ef2aa7823c3446142e2166b

[[ -f "$tree/Makefile" ]] || { echo "not a kernel source tree: $tree" >&2; exit 1; }
version=$(sed -n 's/^VERSION *= *//p' "$tree/Makefile" | head -1)
patchlevel=$(sed -n 's/^PATCHLEVEL *= *//p' "$tree/Makefile" | head -1)
sublevel=$(sed -n 's/^SUBLEVEL *= *//p' "$tree/Makefile" | head -1)
[[ "$version.$patchlevel.$sublevel" == 4.9.237 ]] || {
  echo "expected pinned Linux 4.9.237, found $version.$patchlevel.$sublevel" >&2
  exit 1
}
if git -C "$tree" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  actual=$(git -C "$tree" rev-parse HEAD)
  [[ "$actual" == "$expected_commit" ]] || {
    echo "source pin mismatch: expected $expected_commit, got $actual" >&2
    exit 1
  }
fi
[[ -x "$(command -v "${CROSS_COMPILE}gcc" || true)" ]] || {
  echo "missing cross compiler: ${CROSS_COMPILE}gcc" >&2; exit 1;
}
[[ -f "$tree/arch/arm64/configs/$defconfig" ]] || {
  echo "missing J4+ defconfig: $defconfig" >&2; exit 1;
}
[[ -f "$touchscreen_test" ]] || { echo "missing touchscreen-isolation helper: $touchscreen_test" >&2; exit 1; }

mkdir -p "$out"
export ARCH=arm64
export CROSS_COMPILE
export KBUILD_BUILD_USER=arena-ci
export KBUILD_BUILD_HOST=j4primelte
export KBUILD_BUILD_VERSION=1
export SOURCE_DATE_EPOCH=0
export KBUILD_BUILD_TIMESTAMP='Thu, 01 Jan 1970 00:00:00 +0000'
export LC_ALL=C

echo '=== apply controlled touchscreen-isolation DTS test'
python3 "$touchscreen_test" "$tree/arch/arm64/boot/dts/samsung/msm8917-sec-j4primelte-sea-open-r06.dtsi"

make_args=(ARCH=arm64 "CROSS_COMPILE=$CROSS_COMPILE" \
  'HOSTCFLAGS=-O2 -Wall -Wmissing-prototypes -Wstrict-prototypes -fomit-frame-pointer -std=gnu89 -fcommon')

echo "=== configure $defconfig (Linux 4.9.237)"
make -C "$tree" O="$out" "${make_args[@]}" "$defconfig"
cfg="$tree/scripts/config"
[[ -x "$cfg" ]] || { echo "kernel scripts/config is missing" >&2; exit 1; }

# systemd v249's required kernel options (its README baseline is Linux >=3.13).
# Keep cgroups in legacy mode: this 4.9 kernel does not provide the newer v2
# controller set required by the postmarketOS systemd v262 rootfs.
required=(DEVTMPFS CGROUPS INOTIFY_USER SIGNALFD TIMERFD EPOLL UNIX NET SYSFS PROC_FS
  FHANDLE CRYPTO_USER_API CRYPTO_USER_API_HASH CRYPTO_HMAC CRYPTO_SHA256)
recommended=(DEVTMPFS_MOUNT TMPFS TMPFS_XATTR TMPFS_POSIX_ACL SECCOMP SECCOMP_FILTER
  AUTOFS4_FS NET_NS USER_NS SENSORS CONFIGFS_FS USB_GADGET USB_CONFIGFS USB_CONFIGFS_RNDIS
  USB_CONFIGFS_ACM USB_MSM_OTG FB FRAMEBUFFER_CONSOLE VT VT_CONSOLE BLK_DEV_INITRD RD_XZ)
for symbol in "${required[@]}" "${recommended[@]}"; do
  "$cfg" --file "$out/.config" --enable "$symbol" 2>/dev/null || true
done
for symbol in ANDROID_PARANOID_NETWORK SYSFS_DEPRECATED FW_LOADER_USER_HELPER \
  USB_G_ANDROID USB_ANDROID_SAMSUNG_COMPOSITE; do
  "$cfg" --file "$out/.config" --disable "$symbol" 2>/dev/null || true
done
"$cfg" --file "$out/.config" --set-str LOCALVERSION '-j4-k49-systemd-debug-no-touch'
make -C "$tree" O="$out" "${make_args[@]}" olddefconfig

for symbol in "${required[@]}"; do
  if ! grep -qx "CONFIG_${symbol}=y" "$out/.config"; then
    echo "required for systemd 249: CONFIG_${symbol} is not enabled after olddefconfig" >&2
    exit 1
  fi
done
for symbol in USB_GADGET USB_CONFIGFS USB_CONFIGFS_RNDIS USB_CONFIGFS_ACM USB_MSM_OTG \
  FRAMEBUFFER_CONSOLE VT VT_CONSOLE SENSORS; do
  if ! grep -qx "CONFIG_${symbol}=y" "$out/.config"; then
    echo "required for RNDIS debug: CONFIG_${symbol} is not enabled after olddefconfig" >&2
    exit 1
  fi
done
for symbol in USB_G_ANDROID USB_ANDROID_SAMSUNG_COMPOSITE ANDROID_PARANOID_NETWORK; do
  if grep -qx "CONFIG_${symbol}=y" "$out/.config"; then
    echo "incompatible Android gadget/network option remains enabled: CONFIG_${symbol}" >&2
    exit 1
  fi
done

if [[ ${CONFIG_ONLY:-0} == 1 ]]; then
  cp "$out/.config" "$out/config.full"
  echo 'config-only validation passed; compile skipped'
  exit 0
fi

echo '=== build kernel and DTBs'
make -C "$tree" O="$out" "${make_args[@]}" -j"$jobs"
make -C "$tree" O="$out" "${make_args[@]}" dtbs -j"$jobs"

kernel="$out/arch/arm64/boot/Image.gz"
dtb="$out/arch/arm64/boot/dts/samsung/msm8917-sec-j4primelte-sea-open-r06.dtb"
[[ -s "$kernel" ]] || { echo "missing compressed kernel: $kernel" >&2; exit 1; }
[[ -s "$dtb" ]] || { echo "missing J4+ Rev06 DTB: $dtb" >&2; exit 1; }
cp "$kernel" "$out/kernel.payload"
cp "$dtb" "$out/j4primelte-rev06.dtb"
cp "$out/.config" "$out/config.full"
sha256sum "$out/kernel.payload" "$out/j4primelte-rev06.dtb" "$out/config.full" > "$out/SHA256SUMS"
printf 'kernel=Image.gz (%s bytes)\ndtb=J4+ Rev06 (%s bytes)\n' \
  "$(stat -c %s "$out/kernel.payload")" "$(stat -c %s "$out/j4primelte-rev06.dtb")"
