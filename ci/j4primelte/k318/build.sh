#!/usr/bin/env bash
set -euo pipefail

HERE=$(cd -- "$(dirname -- "$0")" && pwd)
: "${KERNEL_TREE:?set KERNEL_TREE to a clean checkout of the pinned source}"
: "${CROSS_COMPILE:?set CROSS_COMPILE to the full cross-compiler prefix}"
OUT=${OUT:-"$PWD/out/j4primelte-k318"}
JOBS=${JOBS:-4}
EXPECTED=4fcc1090e5e07e7ccf877793e1bfe151641424bf
ACTUAL=$(git -C "$KERNEL_TREE" rev-parse HEAD)
if [[ "$ACTUAL" != "$EXPECTED" ]]; then
  echo "Expected kernel commit $EXPECTED, got $ACTUAL" >&2
  exit 1
fi
if ! git -C "$KERNEL_TREE" diff --quiet || ! git -C "$KERNEL_TREE" diff --cached --quiet; then
  echo "KERNEL_TREE must be clean before applying the prototype patches" >&2
  exit 1
fi

for patch in "$HERE"/patches/*.patch; do
  git -C "$KERNEL_TREE" apply --check "$patch"
  git -C "$KERNEL_TREE" apply "$patch"
done

# The vendor touch driver's CONFIG_FB notifier calls sec_ts_input_open(), which
# is only defined under USE_OPEN_CLOSE; with CONFIG_FB removed the call site is
# compiled out and the definition becomes an unused-function warning, which this
# tree's gcc-wrapper.py turns into a hard error. See README "Headless build".

mkdir -p "$OUT"
cp "$HERE/build2.config" "$OUT/.config"
"$KERNEL_TREE/scripts/config" --file "$OUT/.config" \
  --enable MEMBARRIER --enable CPUSETS --enable FHANDLE --enable MEMCG \
  --disable FB --disable FB_MSM --disable FB_MSM_MDSS_COMMON \
  --disable FB_MSM_MDSS --disable FB_MSM_MDSS_WRITEBACK \
  --disable FB_MSM_MDSS_XLOG_DEBUG --disable FB_MSM_MDSS_SAMSUNG \
  --disable MSM_DBA --disable MSM_DBA_ADV7533 \
  --disable BACKLIGHT_LCD_SUPPORT --disable LCD_CLASS_DEVICE \
  --disable BACKLIGHT_CLASS_DEVICE --disable BACKLIGHT_GENERIC \
  --disable SUPPORT_WHITERGB \
  --disable TOUCHSCREEN_SEC_INCELL_TS \
  --set-str EXTRA_FIRMWARE ''
make -C "$KERNEL_TREE" O="$OUT" ARCH=arm64 \
  CROSS_COMPILE="$CROSS_COMPILE" olddefconfig
PATH="${BUILD_PATH:-$PATH}" make -C "$KERNEL_TREE" O="$OUT" \
  ARCH=arm64 CROSS_COMPILE="$CROSS_COMPILE" -j"$JOBS" Image.gz dtbs

printf 'Kernel: %s\n' "$OUT/arch/arm64/boot/Image.gz"
printf 'J4+ DTB: %s\n' \
  "$OUT/arch/arm64/boot/dts/samsung/msm8917/msm8917-sec-j4primelte-sea-open-r06.dtb"
sha256sum "$OUT/arch/arm64/boot/Image.gz" \
  "$OUT/arch/arm64/boot/dts/samsung/msm8917/msm8917-sec-j4primelte-sea-open-r06.dtb"
