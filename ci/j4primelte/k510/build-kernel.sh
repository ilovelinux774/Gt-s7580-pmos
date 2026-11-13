#!/usr/bin/env bash
# Configure and build a 5.10 kernel for the J4+, then audit the result.
#
#   build-kernel.sh <kernel-tree> <out-dir> <toolchain-dir> <defconfig>
#
# Environment knobs (all optional):
#   J4_K510_JOBS        parallel make jobs (default: nproc)
#   J4_K510_LOCALVERSION  -j4-k510-... suffix, default -j4-k510
#   J4_K510_CC          override the C compiler (e.g. clang)
#   J4_K510_KEEP_TREE   set to 1 to skip the "already built" guard
#
# The script never invents a defconfig and never continues after an error. It
# writes build/config.full by asking the kernel itself (savedefconfig would
# hide the very symbols config-audit.py checks for), then runs the audit in
# "hard" mode, which must pass; "systemd" mode is reported as a gate too unless
# J4_K510_ALLOW_AUDIT_FAIL=1.
set -euo pipefail

tree=${1:?usage: build-kernel.sh <kernel-tree> <out-dir> <toolchain-dir> <defconfig>}
out=${2:?out dir}
toolchain=${3:?toolchain dir}
defconfig=${4:?defconfig name, relative to arch/arm64/configs}

HERE=$(cd -- "$(dirname -- "$0")" && pwd)
jobs=${J4_K510_JOBS:-$(nproc)}
localversion=${J4_K510_LOCALVERSION:--j4-k510}
# The cross prefix comes from sources.env (J4_K510_CROSS_COMPILE); the
# toolchain directory may be /usr when the host compiler is used.
prefix=${J4_K510_CROSS_COMPILE:-aarch64-linux-gnu-}
cross=$toolchain/bin/$prefix
[[ -x ${cross}gcc ]] || cross=$(command -v "${prefix}gcc" || true)
[[ -n $cross ]] && cross=${cross%gcc}

[[ -f $tree/Makefile ]] || { echo "not a kernel tree: $tree" >&2; exit 1; }
[[ -n $cross && -x ${cross}gcc ]] || { echo "no compiler for prefix $prefix" >&2; exit 1; }
echo "compiler: ${cross}gcc ($(${cross}gcc -dumpversion 2>/dev/null))"
[[ -f $tree/arch/arm64/configs/$defconfig ]] \
	|| { echo "defconfig not found: arch/arm64/configs/$defconfig" >&2; exit 1; }
# Space-separated list; later files win, which is how an overrides fragment
# re-points something the first one got wrong.
fragment=${J4_K510_FRAGMENT:-}
if [[ -n $fragment ]]; then
	for f in $fragment; do
		[[ -f $f ]] || { echo "fragment not found: $f" >&2; exit 1; }
		echo "fragment: $f"
	done
fi
mkdir -p "$out"

# --- reproducibility -------------------------------------------------------
export KBUILD_BUILD_USER=pmos-ci
export KBUILD_BUILD_HOST=github-actions
export KBUILD_BUILD_VERSION=1
export SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-0}
export KBUILD_BUILD_TIMESTAMP=${KBUILD_BUILD_TIMESTAMP:-"Thu, 01 Jan 1970 00:00:00 +0000"}
export LC_ALL=C

# Host flags are NOT overridden for modern trees. Forcing the 3.18-era
# "-std=gnu89 -fcommon" onto a 7.x tree breaks kconfig itself:
#   scripts/include/hashtable.h: error: 'for' loop initial declarations are only
#   allowed in C99 or C11 mode
# Only trees older than 5.0 get the legacy flags, because only they need them.
kver_major=$(sed -n 's/^VERSION *= *//p' "$tree/Makefile" | head -n1)
make_args=(ARCH=arm64 "CROSS_COMPILE=$cross" PYTHON=python3)
if (( kver_major < 5 )); then
	HOSTCFLAGS="-O2 -Wall -Wmissing-prototypes -Wstrict-prototypes -fomit-frame-pointer -std=gnu89 -fcommon"
	make_args+=("HOSTCFLAGS=$HOSTCFLAGS")
	echo "legacy host flags enabled (tree is $kver_major.x)"
fi
if [[ -n ${J4_K510_CC:-} ]]; then
	make_args+=("CC=$J4_K510_CC")
else
	make_args+=("CC=${cross}gcc")
fi

echo "=== configure: $defconfig"
make -C "$tree" O="$out" "${make_args[@]}" "$defconfig"

# --- the symbols systemd actually needs ------------------------------------
# Vendor defconfigs are written for Android. Rather than shipping a long
# fragment that half-applies, ask the kernel's own config tool to set exactly
# these and let it tell us about the ones that do not exist in this tree.
cfg="$tree/scripts/config"
want_y=(
	DEVTMPFS DEVTMPFS_MOUNT FHANDLE TMPFS TMPFS_POSIX_ACL TMPFS_XATTR
	SECCOMP SECCOMP_FILTER NAMESPACES INOTIFY_USER SIGNALFD TIMERFD EPOLL
	UNIX SYSFS PROC_FS NET MEMBARRIER
	CGROUPS CGROUP_SCHED CPUSETS MEMCG CGROUP_FREEZER CGROUP_CPUACCT CGROUP_BPF
	AUTOFS_FS BLK_DEV_INITRD RD_GZIP EXT4_FS VT UNIX98_PTYS DEVPTS_MULTIPLE_INSTANCES
	SYSVIPC
)
want_compat=(COMPAT COMPAT_32BIT_TIME)
# Explicitly off: Android-only policy that breaks a Linux userspace.
want_n=(ANDROID_PARANOID_NETWORK MODULE_SIG DM_VERITY_SECONDARY_KEY)

if [[ -x $cfg ]] || [[ -f $cfg ]]; then
	for sym in "${want_y[@]}" "${want_compat[@]}"; do
		"$cfg" --file "$out/.config" --enable "$sym" 2>/dev/null \
			|| echo "  (symbol not present in this tree: CONFIG_$sym)"
	done
	for sym in "${want_n[@]}"; do
		"$cfg" --file "$out/.config" --disable "$sym" 2>/dev/null || true
	done
	"$cfg" --file "$out/.config" --set-str LOCALVERSION "$localversion"
else
	echo "warning: $cfg missing; shipping the defconfig unmodified" >&2
fi

# A fragment is applied through scripts/config, not by concatenating text: a
# vendor tree happily ignores unknown symbols, so a typo has to be reported.
if [[ -n $fragment ]]; then
	# Accept both "+ SYMBOL" (the format in ci/j4primelte/mainline/) and
	# "+SYMBOL"; an unknown symbol is reported, never silently dropped.
	for f in $fragment; do
	while read -r op sym rest; do
		[[ -z ${op:-} || $op == \#* || $op == \!* ]] && continue
		if [[ $op != [+-@] ]]; then
			sym=${op#?}
			op=${op:0:1}
		fi
		case $op in
			+) "$cfg" --file "$out/.config" --enable "$sym" 2>/dev/null \
				|| echo "  fragment: CONFIG_$sym is not in this tree" ;;
			-) "$cfg" --file "$out/.config" --disable "$sym" 2>/dev/null || true ;;
			@) "$cfg" --file "$out/.config" --set-str "$sym" "$rest" 2>/dev/null \
				|| echo "  fragment: CONFIG_$sym is not in this tree" ;;
		esac
	done < "$f"
	done
fi

# Re-resolve dependencies now, so the .config we audit is the one we build.
make -C "$tree" O="$out" "${make_args[@]}" olddefconfig

if [[ ${J4_K510_CONFIG_ONLY:-0} == 1 ]]; then
	echo "=== config-only requested; stopping before compile"
	cp "$out/.config" "$out/config.full"
	echo "=== audit (systemd)"
	python3 "$HERE/config-audit.py" "$out/config.full" --mode systemd || true
	exit 0
fi

echo "=== build"
make -C "$tree" O="$out" "${make_args[@]}" -j"$jobs"
# Mainline builds dtbs in the default target; vendor trees often do not.
make -C "$tree" O="$out" "${make_args[@]}" dtbs -j"$jobs" \
	|| echo "note: dtbs target failed (see the log above)"

echo "=== collect"
art="$out/arch/arm64/boot"
for candidate in Image.gz-dtb Image.gz Image; do
	if [[ -f $art/$candidate ]]; then
		echo "kernel image: $candidate ($(stat -c%s "$art/$candidate") bytes)"
		cp "$art/$candidate" "$out/kernel.payload"
		break
	fi
done
# Android boot images for arm64 carry the DTB appended to the kernel payload
# (the Image.gz-dtb convention). Samsung's loader expects a DTB it can find:
# with none appended, the kernel has no idea what board it is on, and on this
# phone that is the difference between "boots" and "hangs".
#
# Preferred source is the tree's own DTB. The pinned tree has no j4primelte
# device tree, so fall back to the board DTS pinned in sources.env, compiled
# with the tree's own headers (cpp) and dtc.
board_dts=${J4_K510_BOARD_DTS:-}
dtb=$(find "$out/arch/arm64/boot/dts" -iname '*j4primelte*.dtb' 2>/dev/null | head -n1) || true

if [[ -z $dtb && -n $board_dts ]]; then
	[[ -f $board_dts ]] || { echo "board DTS not found: $board_dts" >&2; exit 1; }
	command -v dtc >/dev/null || {
		echo "dtc is required to build $board_dts but is not installed" >&2
		exit 1
	}
	inc=(-I "$tree/include" -I "$tree/arch/arm64/boot/dts/qcom")
	echo "=== board dtb: cpp"
	cpp -nostdinc -undef -x assembler-with-cpp "${inc[@]}" "$board_dts" -o "$out/j4-board.dts" \
		|| { echo "FAILED PHASE: board dtb (cpp) on $board_dts" >&2; exit 1; }
	echo "=== board dtb: dtc"
	dtc -I dts -O dtb -o "$out/j4-board.dtb" "$out/j4-board.dts" 2> "$out/dtc.log" \
		|| { echo "FAILED PHASE: board dtb (dtc)" >&2; cat "$out/dtc.log" >&2; exit 1; }
	dtb="$out/j4-board.dtb"
	echo "board DTB: $(stat -c%s "$dtb") bytes"
fi

if [[ -n $dtb && -f $out/kernel.payload ]]; then
	if [[ $(head -c2 "$out/kernel.payload" | od -An -tx1 | tr -d ' \n') == 1f8b ]]; then
		cat "$out/kernel.payload" "$dtb" > "$out/kernel.payload.dtb"
		echo "appended $dtb to the gzip payload (Image.gz-dtb convention)"
	else
		cat "$out/kernel.payload" "$dtb" > "$out/kernel.payload.dtb"
		echo "appended $dtb to the raw Image payload"
	fi
	mv "$out/kernel.payload.dtb" "$out/kernel.payload"
	cp "$dtb" "$out/j4-board.dtb"
else
	cat >&2 <<-'EOF'
	ERROR: the boot payload would carry no device tree.
	       A bare Image/Image.gz with no DTB does not describe this board, and
	       flashing it only produces a boot that hangs with no output.
	       Pin J4_K510_BOARD_DTS in sources.env (or use a tree that ships a
	       j4primelte device tree).
	EOF
	exit 1
fi

if [[ ! -f $out/kernel.payload ]]; then
	# Some vendor trees only produce vmlinux plus a raw Image under a
	# different name; fail with a listing instead of guessing.
	echo "no Image/Image.gz found under $art; contents:" >&2
	ls -la "$art" >&2 || true
	exit 1
fi
cp "$out/.config" "$out/config.full"

echo "=== audit (hard)"
python3 "$HERE/config-audit.py" "$out/config.full" --mode hard

echo "=== audit (systemd)"
if ! python3 "$HERE/config-audit.py" "$out/config.full" --mode systemd; then
	if [[ ${J4_K510_ALLOW_AUDIT_FAIL:-0} == 1 ]]; then
		echo "WARNING: systemd audit failed but J4_K510_ALLOW_AUDIT_FAIL=1" >&2
	else
		echo "systemd audit failed: this kernel would boot but systemd/rootfs would not" >&2
		exit 1
	fi
fi

# A machine-readable summary for the workflow to attach to the artifact.
python3 "$HERE/config-audit.py" "$out/config.full" --mode systemd --json \
	> "$out/config-audit.json" || true
echo "=== done"
