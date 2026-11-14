#!/usr/bin/env bash
# Configure the Riva 4.19 source and compile only the imported J4+ Rev06 DTB.
set -euo pipefail

tree=${1:?usage: build-dtb-smoke.sh <kernel-tree> <output-dir>}
out=${2:?output directory}

[[ -f "$tree/Makefile" ]] || { echo "not a kernel source tree: $tree" >&2; exit 1; }
version=$(sed -n 's/^VERSION *= *//p' "$tree/Makefile" | head -1)
patchlevel=$(sed -n 's/^PATCHLEVEL *= *//p' "$tree/Makefile" | head -1)
sublevel=$(sed -n 's/^SUBLEVEL *= *//p' "$tree/Makefile" | head -1)
[[ "$version.$patchlevel.$sublevel" == 4.19.325 ]] || {
  echo "expected Linux 4.19.325, found $version.$patchlevel.$sublevel" >&2
  exit 1
}

mkdir -p "$out"
make -C "$tree" O="$out" ARCH=arm64 LLVM=1 vendor/msm8937-perf_defconfig
python3 "$(dirname "${BASH_SOURCE[0]}")/import-j4-dts.py" "$tree"
make -C "$tree" O="$out" ARCH=arm64 LLVM=1 -j"${JOBS:-$(nproc)}" dtbs

dtb="$out/arch/arm64/boot/dts/samsung/msm8917-sec-j4primelte-sea-open-r06.dtb"
[[ -s "$dtb" ]] || { echo "J4+ Rev06 DTB was not produced: $dtb" >&2; exit 1; }
cp "$dtb" "$out/j4primelte-rev06.dtb"
sha256sum "$out/j4primelte-rev06.dtb" > "$out/j4primelte-rev06.dtb.sha256"
printf 'J4+ Rev06 DTB built (%s bytes)\n' "$(stat -c %s "$out/j4primelte-rev06.dtb")"
cat "$out/j4primelte-rev06.dtb.sha256"
