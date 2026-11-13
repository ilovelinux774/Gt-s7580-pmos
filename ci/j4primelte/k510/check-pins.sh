#!/usr/bin/env bash
# Validate sources.env itself before anything expensive depends on it.
#
#   check-pins.sh <sources.env>
#
# A duplicated assignment in a sourced file is invisible: bash silently uses the
# last one, so a stale empty value can quietly undo a correct pin. That happened
# here once; this script is why it cannot happen silently again.
set -euo pipefail

file=${1:?usage: check-pins.sh <sources.env>}
[[ -f $file ]] || { echo "no such file: $file" >&2; exit 1; }

fail() { printf 'PIN FAIL: %s\n' "$*" >&2; exit 1; }

# --- duplicated assignments ------------------------------------------------
# Only uncommented NAME= lines count; comments may mention a name freely.
dupes=$(grep -E '^[[:space:]]*[A-Z_][A-Z0-9_]*=' "$file" \
	| sed -E 's/^[[:space:]]*([A-Z_][A-Z0-9_]*)=.*/\1/' | sort | uniq -d)
if [[ -n $dupes ]]; then
	printf 'PIN FAIL: assigned more than once (the later value silently wins):\n' >&2
	printf '  %s\n' $dupes >&2
	exit 1
fi

# shellcheck disable=SC1090
source "$file"

# --- required values -------------------------------------------------------
for var in J4_K510_KERNEL_URL J4_K510_KERNEL_BRANCH J4_K510_KERNEL_VERSION \
           J4_K510_KERNEL_PROBE J4_K510_CROSS_COMPILE J4_K510_DONOR_URL \
           J4_K510_DONOR_SHA256; do
	[[ -n ${!var:-} ]] || fail "$var is empty"
done

# --- shapes ---------------------------------------------------------------
[[ $J4_K510_KERNEL_VERSION =~ ^[0-9]+\.[0-9]+$ ]] \
	|| fail "J4_K510_KERNEL_VERSION must look like 5.10, got '$J4_K510_KERNEL_VERSION'"
[[ $J4_K510_DONOR_SHA256 =~ ^[0-9a-f]{64}$ ]] \
	|| fail "J4_K510_DONOR_SHA256 must be 64 hex characters"
[[ ${J4_K510_KERNEL_COMMIT:-} =~ ^([0-9a-f]{40})?$ ]] \
	|| fail "J4_K510_KERNEL_COMMIT must be empty or a full 40-character sha"

# The GT-S7580 boot image at the repository root must never be the donor: it is
# a parser fixture for a different phone.
case $J4_K510_DONOR_URL in
	*boot.img) echo "note: donor URL ends in boot.img; the sha256 check is what protects this" ;;
esac
[[ $J4_K510_DONOR_SHA256 != 0000000000000000000000000000000000000000000000000000000000000000 ]] \
	|| fail "donor sha256 is a placeholder"

# --- fragments, if any ----------------------------------------------------
if [[ -n ${J4_K510_FRAGMENT:-} ]]; then
	root=$(cd -- "$(dirname -- "$0")/../../.." && pwd)
	for f in $J4_K510_FRAGMENT; do
		[[ -f $root/$f ]] || fail "fragment listed in sources.env does not exist: $f"
	done
fi

printf 'PIN OK: %s (%s @ %s, defconfig "%s")\n' \
	"$J4_K510_KERNEL_URL" "$J4_K510_KERNEL_BRANCH" \
	"${J4_K510_KERNEL_COMMIT:-unpinned}" "${J4_K510_DEFCONFIG:-discover}"
