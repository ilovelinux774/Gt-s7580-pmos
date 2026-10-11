#!/usr/bin/env python3
"""One-line provenance note about the boot.img that donates the initramfs.

Kept in its own file so the workflow does not need an inline heredoc, which is
easy to get wrong and impossible to test.
"""
from pathlib import Path
import struct
import sys


def note(data: bytes) -> str:
    if data[:8] != b'ANDROID!':
        raise SystemExit('donor is not an Android boot image (no ANDROID! magic)')
    if len(data) < 72:
        raise SystemExit('donor boot image is truncated')
    ksize, kaddr, rsize, raddr, ssize, saddr, tags, page, version, osver = \
        struct.unpack_from('<10I', data, 8)
    if version != 0 or page not in (2048, 4096):
        raise SystemExit(f'donor uses boot header v{version} with page {page}; '
                         'this job only repacks header v0 images')
    if rsize == 0:
        raise SystemExit('donor has no ramdisk: there is nothing to inherit, and a '
                         'boot image with an empty ramdisk cannot start pmOS init')
    name = data[48:64].split(b'\0', 1)[0].decode('ascii', 'replace')
    return (f'donor boot.img: kernel {ksize} B @0x{kaddr:08x}, ramdisk {rsize} B '
            f'@0x{raddr:08x}, second {ssize} B, page {page}, tags 0x{tags:08x}, '
            f'header v0, name {name or "(unset)"}\n')


def main(argv):
    text = note(Path(argv[1]).read_bytes())
    print(text.rstrip())
    if len(argv) > 2:
        Path(argv[2]).write_text(text)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
