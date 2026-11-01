#!/usr/bin/env bash
# Retired: this quick path reconstructed a J415F HLOS BOOT image that failed
# on the phone and reused a DTB missing the Rev06 firmware reservations.
# Its historical implementation remains in repository commit 90e1b3a.
# A new experiment must build and verify the updated J4 DTS instead.
set -euo pipefail

if [ "${1:-}" = '-n' ] || [ "${1:-}" = '--dry-run' ]; then
    bash -n "${BASH_SOURCE[0]}"
    echo 'Historical HLOS reconstruction retired; syntax OK'
    exit 0
fi

echo 'DEVICE TEST FAILED: the historical HLOS reconstruction is retired; use a full build with the updated DTS.' >&2
exit 1
