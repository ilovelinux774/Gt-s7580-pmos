#!/usr/bin/env python3
"""Install j4-rootfs-move on a running J4+.

SYSTEM (mmcblk0p47) is small and userdata (mmcblk0p53) is the biggest partition
on the phone, so the running rootfs can be moved there. The helper never touches
the running root (it stays as a fallback) and never writes the boot partition:
pointing the boot image at the new UUID is a separate, reversible step.

Use on a phone that is already installed:

    python3 rootfs-move.py --install
    j4-rootfs-move --check
"""
import argparse
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAYLOAD = HERE / 'rootfs' / 'usr' / 'local' / 'sbin' / 'j4-rootfs-move'
DEST = Path('/usr/local/sbin/j4-rootfs-move')


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--install', action='store_true',
                        help='write the helper to %s' % DEST)
    args = parser.parse_args()
    if not args.install:
        parser.print_help()
        return 0
    if os.geteuid() != 0:
        raise SystemExit('run as root')
    text = PAYLOAD.read_text()
    DEST.write_text(text)
    DEST.chmod(0o755)
    print('INSTALL_OK: wrote %s' % DEST)
    print('Next: j4-rootfs-move --check')


if __name__ == '__main__':
    main()
