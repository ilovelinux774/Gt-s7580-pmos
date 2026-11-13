#!/usr/bin/env bash
# Fail fast if a candidate 5.10 tree is not the tree the pin claims.
#
#   verify-tree.sh <tree> <expected-version> <probe-path> [defconfig]
#
# Prints a short report and exits non-zero on any mismatch. Nothing here
# requires a compiler, so it can run before the expensive part of a build.
set -euo pipefail

tree=${1:?usage: verify-tree.sh <tree> <expected-version> <probe-path> [defconfig]}
want_version=${2:?expected version, e.g. 5.10}
probe=${3:?probe path relative to tree root}
defconfig=${4:-}

fail() { printf 'VERIFY FAIL: %s\n' "$*" >&2; exit 1; }

[[ -d $tree ]] || fail "tree directory does not exist: $tree"
[[ -f $tree/Makefile ]] || fail "no Makefile at $tree/Makefile -> not a kernel tree"

# --- version ---------------------------------------------------------------
read_make() { sed -n "s/^$1 *= *//p" "$tree/Makefile" | head -n1; }
major=$(read_make VERSION)
patch=$(read_make PATCHLEVEL)
sub=$(read_make SUBLEVEL)
got_version="${major}.${patch}"
[[ $got_version == "$want_version" ]] \
	|| fail "tree is Linux ${got_version}.${sub}, pin expects ${want_version}"
echo "version: ${got_version}.${sub}"

# --- probe ----------------------------------------------------------------
[[ -e $tree/$probe ]] || fail "probe path missing: $probe (wrong tree or wrong branch)"
echo "probe: $probe present"

# --- architecture reality check -------------------------------------------
# An arm32 5.10 for this SoC would need arch/arm/mach-qcom plus 8917/8937
# device trees. Report what is actually there instead of assuming.
arm32=no
if [[ -d $tree/arch/arm/mach-qcom ]] || [[ -d $tree/arch/arm/mach-msm ]]; then
	arm32=yes
fi
arm32_dts=$(find "$tree/arch/arm/boot/dts" -maxdepth 2 \
	-iname '*8917*' -o -maxdepth 2 -iname '*8937*' 2>/dev/null | wc -l)
echo "arch/arm support: mach=$( [[ $arm32 == yes ]] && echo yes || echo no ) dts_matches=${arm32_dts}"
if [[ $arm32 == no ]] && (( arm32_dts == 0 )); then
	echo "note: this tree cannot build a 32-bit kernel for msm8917/8937."
	echo "      That is expected (and fine): CONFIG_COMPAT runs an armv7 rootfs on it."
fi

# --- SoC support ----------------------------------------------------------
# A generic arm64 tree builds a kernel that has never heard of this phone. Make
# that verdict explicit, because it is the difference between "our pin is
# wrong" and "this tree can never work here".
soc_hits=$(find "$tree/arch/arm64" \( -path '*/boot/dts/*' -o -path '*/configs/*' \) \
	\( -iname '*8917*' -o -iname '*8937*' -o -iname '*scuba*' -o -iname '*khaje*' -o -iname '*sdm429*' \) \
	2>/dev/null | wc -l)
echo "SoC support hits (arch/arm64 dts+configs): $soc_hits"
if (( soc_hits == 0 )); then
	cat >&2 <<-'EOF'
	VERDICT: this tree has no msm8917/msm8937 board support.
	         It will compile, but it will not know this phone, its panel, its
	         touchscreen or its regulators. That is not a pin problem: the tree
	         itself is the wrong base.
	         Point J4_K510_KERNEL_URL/BRANCH/COMMIT (or the tarball inputs) at a
	         5.10 tree that actually carries this SoC.
	EOF
	[[ ${J4_REQUIRE_SOC:-1} == 1 ]] && exit 1
fi

# --- board defconfigs -----------------------------------------------------
configs_dir=$tree/arch/arm64/configs
[[ -d $configs_dir ]] || fail "no arch/arm64/configs"
mapfile -t candidates < <(
	find "$configs_dir" -type f \
		\( -iname '*j4*' -o -iname '*scuba*' -o -iname '*khaje*' \
		   -o -iname '*msm8917*' -o -iname '*msm8937*' -o -iname '*sdm429*' \) \
		-printf '%P\n' | sort
)
echo "defconfig candidates: ${#candidates[@]}"
for c in "${candidates[@]}"; do echo "  $c"; done

if [[ -n $defconfig ]]; then
	[[ -f $configs_dir/$defconfig ]] || fail "requested defconfig not found: $defconfig"
	echo "defconfig: $defconfig (pinned)"
else
	if (( ${#candidates[@]} == 0 )); then
		fail "no board defconfig found in arch/arm64/configs; pin J4_K510_DEFCONFIG or pick another tree"
	fi
	if (( ${#candidates[@]} > 1 )); then
		fail "ambiguous: ${#candidates[@]} candidate defconfigs, pin J4_K510_DEFCONFIG"
	fi
	echo "defconfig: ${candidates[0]} (discovered)"
fi

# --- device tree ----------------------------------------------------------
dts=$(find "$tree/arch/arm64/boot/dts" -iname '*j4primelte*' 2>/dev/null | head -n5)
if [[ -n $dts ]]; then
	echo "device tree:"
	printf '  %s\n' $dts
else
	echo "device tree: none for j4primelte (the board DTBs must be brought over)"
fi

echo "VERIFY OK"
