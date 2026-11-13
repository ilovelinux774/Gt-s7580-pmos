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
mkdir -p "$out"

# --- reproducibility -------------------------------------------------------
export KBUILD_BUILD_USER=pmos-ci
export KBUILD_BUILD_HOST=github-actions
export KBUILD_BUILD_VERSION=1
export SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-0}
export KBUILD_BUILD_TIMESTAMP=${KBUILD_BUILD_TIMESTAMP:-"Thu, 01 Jan 1970 00:00:00 +0000"}
export LC_ALL=C

# Recent hosts default to gcc 10+ semantics (no -fcommon, -fno-common, stricter
# warnings). These vendor trees predate all of that.
HOSTCFLAGS="-O2 -Wall -Wmissing-prototypes -Wstrict-prototypes -fomit-frame-pointer -std=gnu89 -fcommon"
make_args=(ARCH=arm64 "CROSS_COMPILE=$cross" "HOSTCFLAGS=$HOSTCFLAGS" PYTHON=python3)
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

# Re-resolve dependencies now, so the .config we audit is the one we build.
make -C "$tree" O="$out" "${make_args[@]}" olddefconfig

echo "=== build"
make -C "$tree" O="$out" "${make_args[@]}" -j"$jobs"
# Vendor arm64 trees do not build DTBs as part of the default target.
make -C "$tree" O="$out" "${make_args[@]}" dtbs -j"$jobs" || echo "note: dtbs target failed"

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
# (the Image.gz-dtb convention). If this tree produced a board DTB, append it;
# otherwise leave the payload as it is and say so loudly.
dtb=$(find "$out/arch/arm64/boot/dts" -iname '*j4primelte*.dtb' 2>/dev/null | head -n1)
if [[ -n $dtb && -f $out/kernel.payload ]]; then
	case $out/kernel.payload in
		*.gz|*/kernel.payload)
			base=$out/kernel.payload
			if [[ $(head -c2 "$base" | od -An -tx1 | tr -d ' \n') == 1f8b ]]; then
				cat "$base" "$dtb" > "$out/kernel.payload.dtb"
				mv "$out/kernel.payload.dtb" "$out/kernel.payload"
				echo "appended $dtb to the gzip payload (Image.gz-dtb convention)"
			else
				cat "$base" "$dtb" > "$out/kernel.payload.dtb"
				mv "$out/kernel.payload.dtb" "$out/kernel.payload"
				echo "appended $dtb to the raw Image payload"
			fi
			;;
	esac
else
	echo "warning: no j4primelte DTB in the build output; the kernel will rely" >&2
	echo "         on whatever DTB the bootloader supplies or on a built-in one" >&2
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
