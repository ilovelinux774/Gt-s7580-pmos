#!/usr/bin/env python3
"""Swap the kernel of an Android boot image for the mainline one.

Why only the kernel: the J4+ runs postmarketOS with an LK boot loader that hands
a ramdisk to the kernel, and that initramfs is what mounts /boot, finds the rootfs
by UUID and starts init in pmOS's partition layout. The downstream armv7 ramdisk
knows how to do that; a mainline defconfig ramdisk would not. So this tool takes
a boot image from a finished postmarketOS build and replaces exactly one payload
field, leaving the ramdisk byte for byte the same.

Layout of the legacy header (version 0) this phone uses, all little endian, all
u32 unless noted:

    0   magic "ANDROID!"
    8   kernel_size
   12   kernel_addr        0x80008000 on the J4+
   16   ramdisk_size
   20   ramdisk_addr       0x82000000
   24   second_size        (the vendor "second" area; kept as is)
   28   second_addr
   32   tags_addr          0x1e000000, where LK puts the appended tree
   36   page_size          2048 here
   40   header_version
   44   os_version
   48   name[16]           "j4primelte"
   64   cmdline[512]
  576   id[32]             SHA-1(payload || le32(size), for each payload), padded
  608   extra_cmdline[1024]

A GKI 16-word struct does not describe this image, and reading it with those
offsets is what once produced "unsupported boot header version 3" from a header
that was perfectly fine.
"""
from pathlib import Path
import argparse
import hashlib
import re
import struct
import sys
import zlib

MAGIC = b'ANDROID!'
NAME_OFFSET = 48
CMDLINE_OFFSET = 64
CMDLINE_FIRST = 512                         # the cmdline[] field
ID_OFFSET = CMDLINE_OFFSET + CMDLINE_FIRST  # id[8], 32 bytes
EXTRA_CMDLINE_OFFSET = ID_OFFSET + 32       # extra_cmdline[1024]
CMDLINE_REST = 1024
HEADER_BYTES = EXTRA_CMDLINE_OFFSET + CMDLINE_REST
MAX_IMAGE = 32 * 1024 * 1024                # the BOOT partition is 32 MiB
IMAGE_NAME = b'j4primelte'

# What the kernel needs on the command line to survive on a phone whose display
# and modems were started by the bootloader and are not claimed by any mainline
# driver yet. These are appended to whatever the donor image carried, which
# includes Samsung's own androidboot.*, lcd_id and vmalloc= arguments.
MAINLINE_BOOTARGS = ('clk_ignore_unused pd_ignore_unused regulator_ignore_unused '
                     'rootwait earlycon')
# postmarketOS initramfs flag: drop to the telnet shell instead of mounting the
# rootfs. The only way to look at a phone whose panel stays black.
DEBUG_FLAG = 'pmos.debug-shell'

DTB_MAGIC = bytes.fromhex('d00dfeed')
# Largest device tree we expect to see appended (msm8917 boards are ~45 KiB, and
# an arm64 defconfig tree with every qcom board in it is under 200 KiB).
DTB_WINDOW = 1 << 20


def find_appended_dtb(payload: bytes) -> int:
    """Offset of the DTB that trails an Image.gz-dtb payload.

    Looking for the magic near the end is not enough: the tree is tens of
    kilobytes, so its header sits far from the last byte. The totalsize in the DTB
    header has to account for exactly the bytes that follow the kernel, which is
    what makes a truncated or duplicated append detectable at all.
    """
    low = max(0, len(payload) - DTB_WINDOW)
    offset = payload.rfind(DTB_MAGIC)
    while offset >= low:
        if offset % 8 == 0 and offset + 8 <= len(payload):
            totalsize = struct.unpack_from('>I', payload, offset + 4)[0]
            if totalsize == len(payload) - offset and totalsize > 0x10:
                return offset
        offset = payload.rfind(DTB_MAGIC, low, offset)
    raise ValueError(
        f'no appended DTB found in the last {DTB_WINDOW // 1024} KiB of the '
        'payload: an arm64 kernel that boots with no device tree hangs before the '
        'console, so this is refused rather than packed')


def check_gzip_boundary(payload: bytes, dtb_at: int):
    """Reject an external gap: Qualcomm LK may expect the DTB at gzip EOF."""
    image = payload[:dtb_at]
    if len(image) < 18 or image[:3] != b'\x1f\x8b\x08':
        raise ValueError('kernel is not gzip')
    if image[3] not in (0, 8):
        raise ValueError('gzip has optional fields unsupported by reference LK')
    if image[3] == 8 and image.find(b'\0', 10, min(len(image), 266)) < 0:
        raise ValueError('gzip filename is missing or exceeds reference LK limits')
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        decoder.decompress(image)
        decoder.flush()
    except zlib.error as exc:
        raise ValueError('invalid gzip kernel') from exc
    if not decoder.eof or decoder.unused_data:
        raise ValueError('DTB must begin immediately at the end of gzip, with no gap')


def split_cmdline(encoded: bytes) -> tuple:
    """Split at Android v0's exact field boundary, without altering a byte.

    Android mkbootimg writes the first 512 bytes into cmdline[] and the
    remaining bytes into extra_cmdline[].  A token may cross the boundary;
    adding or removing a space on either side silently changes the bootargs.
    """
    if len(encoded) > CMDLINE_FIRST + CMDLINE_REST:
        raise ValueError('cmdline exceeds Android v0 header capacity')
    return encoded[:CMDLINE_FIRST], encoded[CMDLINE_FIRST:]


def align(value: int, page: int) -> int:
    return (value + page - 1) // page * page


def split_args(text: str) -> list:
    return [token for token in re.split(r'\s+', text.strip()) if token]


def with_cmdline(base_args, extra=(), drop_duplicates=True) -> str:
    """Merge bootargs without duplicating anything, keeping the donor's order."""
    tokens = split_args(base_args)
    for token in extra:
        key = token.split('=', 1)[0]
        if drop_duplicates and any(t.split('=', 1)[0] == key for t in tokens):
            continue
        tokens.append(token)
    return ' '.join(tokens)


class BootImage:
    """A header-v0 Android boot image, parsed and re-packable."""

    def __init__(self, data: bytes):
        if data[:8] != MAGIC:
            raise ValueError('not an Android boot image (missing ANDROID! magic)')
        if len(data) < HEADER_BYTES:
            raise ValueError('truncated boot image header')
        self.data = bytearray(data)
        (self.kernel_size, self.kernel_addr, self.ramdisk_size,
         self.ramdisk_addr, self.second_size, self.second_addr, self.tags_addr,
         self.page_size, self.header_version, self.os_version) = \
            struct.unpack_from('<10I', self.data, 8)
        if self.header_version != 0:
            raise ValueError(f'unsupported boot header version {self.header_version}; '
                             'this phone ships a v0 header and only v0 is written '
                             'back by this tool')
        if self.page_size not in (2048, 4096):
            raise ValueError(f'unsupported page size {self.page_size}')
        # Only v1/v2 headers define an area after extra_cmdline; in a v0 header
        # those bytes are vendor padding (or cmdline tail), so never validate them.
        self.recovery_dtbo_size = self.bootconfig_size = 0
        if self.header_version >= 1 and len(self.data) >= 1652:
            self.recovery_dtbo_size = struct.unpack_from('<I', self.data, 1632)[0]
            self.bootconfig_size = struct.unpack_from('<I', self.data, 1640)[0]
        self.name = bytes(self.data[NAME_OFFSET:NAME_OFFSET + 16])
        self.cmdline = self._read_cmdline()
        self.kernel = self._payload(0, self.kernel_size)
        self.ramdisk = self._payload(1, self.ramdisk_size)
        self.second = self._payload(2, self.second_size)
        required = self.page_size + sum(align(n, self.page_size) for n in
                                        (self.kernel_size, self.ramdisk_size,
                                         self.second_size))
        if len(self.data) < required:
            raise ValueError('truncated boot image including payload padding')

    def _region_start(self, index: int) -> int:
        offset = self.page_size                     # the header is page 0
        for sizes in (self.kernel_size, self.ramdisk_size, self.second_size)[:index]:
            offset += align(sizes, self.page_size)
        return offset

    def _payload(self, index: int, size: int) -> bytes:
        if not size:
            return b''
        start = self._region_start(index)
        end = start + size
        if end > len(self.data):
            raise ValueError('payload runs past the end of the image')
        return bytes(self.data[start:end])

    def _read_cmdline(self) -> str:
        first = bytes(self.data[CMDLINE_OFFSET:CMDLINE_OFFSET + CMDLINE_FIRST])
        rest = bytes(self.data[EXTRA_CMDLINE_OFFSET:
                               EXTRA_CMDLINE_OFFSET + CMDLINE_REST])
        first = first.split(b'\0', 1)[0].decode('ascii', 'replace')
        rest = rest.split(b'\0', 1)[0].decode('ascii', 'replace')
        return first + rest

    def set_cmdline(self, cmdline: str):
        encoded = cmdline.encode('ascii')
        if len(encoded) > CMDLINE_FIRST + CMDLINE_REST:
            raise ValueError(f'cmdline is {len(encoded)} B, over the '
                             f'{CMDLINE_FIRST + CMDLINE_REST} B the header can hold')
        first, tail = split_cmdline(encoded)
        # Fixed-size slice assignment: writing 32 bytes into a 12-byte slice of a
        # bytearray grows the array and shifts every payload after it. That is how
        # the id field once turned into a UnicodeDecodeError at offset 64.
        self.data[CMDLINE_OFFSET:CMDLINE_OFFSET + CMDLINE_FIRST] = \
            first.ljust(CMDLINE_FIRST, b'\0')
        self.data[EXTRA_CMDLINE_OFFSET:EXTRA_CMDLINE_OFFSET + CMDLINE_REST] = \
            tail.ljust(CMDLINE_REST, b'\0')
        self.cmdline = cmdline

    def with_cmdline(self, cmdline: str) -> 'BootImage':
        image = BootImage(bytes(self.data))
        image.set_cmdline(cmdline)
        return image

    def set_id(self, kernel: bytes, ramdisk: bytes, second: bytes):
        # Android mkbootimg v0 includes the little-endian size after EACH
        # payload (including a zero size for an empty second stage). This is
        # validated independently against the phone's known-good #37 image.
        hasher = hashlib.sha1()
        for blob in (kernel, ramdisk, second):
            hasher.update(blob)
            hasher.update(struct.pack('<I', len(blob)))
        digest = hasher.digest()
        # Exactly 32 bytes: the legacy id field is id[8] u32s, and a 20-byte
        # digest has to be zero padded rather than written short.
        self.data[ID_OFFSET:ID_OFFSET + 32] = digest.ljust(32, b'\0')

    def repack(self, kernel: bytes, ramdisk=None, second=None,
               cmdline: str = None) -> 'BootImage':
        out = BootImage(bytes(self.data))
        ramdisk = self.ramdisk if ramdisk is None else ramdisk
        second = self.second if second is None else second
        out._build(kernel, ramdisk, second)
        # _build starts from a fresh header page, so the cmdline has to be written
        # after it; the donor's arguments are carried over by the caller.
        if cmdline is not None:
            out.set_cmdline(cmdline)
        out.set_id(kernel, ramdisk, second)
        return out

    def _build(self, kernel: bytes, ramdisk: bytes, second: bytes):
        """Re-serialize the image: header page, then the three payloads.

        Every field that is not derived from the payloads is carried over from the
        donor, because LK reads the load addresses and os_version and has no way
        to forgive a change there.
        """
        page = self.page_size
        # Preserve the donor's entire header page, including vendor padding.
        out = bytearray(self.data[:page])
        out[0:8] = MAGIC
        struct.pack_into('<10I', out, 8, len(kernel), self.kernel_addr,
                         len(ramdisk), self.ramdisk_addr,
                         len(second), self.second_addr,
                         self.tags_addr, page, 0, self.os_version)
        out[NAME_OFFSET:NAME_OFFSET + 16] = self.name[:16].ljust(16, b'\0')
        for blob in (kernel, ramdisk, second):
            out += blob
            pad = align(len(blob), page) - len(blob)
            if pad:
                out += b'\0' * pad
        if len(out) > MAX_IMAGE:
            raise ValueError(f'{len(out)} B does not fit the {MAX_IMAGE} B BOOT '
                             'partition; strip the kernel or drop drivers')
        self.data = out
        # Sizes *and* payloads changed, so the object has to describe the image it
        # now holds. Reading .kernel off a packed image used to hand back the
        # donor's kernel, which is the kind of stale-object bug a verifier then
        # cheerfully confirms.
        self.kernel_size, self.ramdisk_size, self.second_size = \
            len(kernel), len(ramdisk), len(second)
        self.kernel, self.ramdisk, self.second = kernel, ramdisk, second
        self.cmdline = self._read_cmdline()


def build(base: BootImage, kernel: bytes, debug_shell: bool = False,
          cmdline_extra: str = '') -> BootImage:
    """Return a copy of `base` whose kernel is `kernel`, ramdisk untouched."""
    roots = [arg for arg in split_args(base.cmdline)
             if arg.startswith('pmos_root_uuid=')]
    root_pattern = (r'pmos_root_uuid=[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-'
                    r'[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
    if len(roots) != 1 or not re.fullmatch(root_pattern, roots[0]):
        raise ValueError('donor must have exactly one valid pmos_root_uuid')
    if not base.kernel or not base.ramdisk:
        raise ValueError('donor must include a nonempty kernel and initramfs')
    if not debug_shell and DEBUG_FLAG in split_args(base.cmdline):
        raise ValueError('rootfs image cannot be built from a debug-shell donor')
    extra = split_args(MAINLINE_BOOTARGS)
    if debug_shell:
        extra.append(DEBUG_FLAG)
    extra += split_args(cmdline_extra)
    image = base.repack(kernel, cmdline=with_cmdline(base.cmdline, extra))
    # Re-read through the same parser: if the header we just wrote does not
    # describe the payloads we wrote, the packing is wrong and it must not ship.
    check = BootImage(bytes(image.data))
    assert check.kernel == kernel, 'kernel payload did not round-trip'
    assert check.ramdisk == base.ramdisk, 'ramdisk changed: the donor initramfs ' \
                                          'must be copied byte for byte'
    assert check.second == base.second, 'second area changed without being asked to'
    assert check.name == base.name, 'boot partition name changed'
    assert (check.kernel_addr, check.ramdisk_addr, check.tags_addr,
            check.page_size) == (base.kernel_addr, base.ramdisk_addr,
                                 base.tags_addr, base.page_size)
    return image


def report(image: BootImage, base: BootImage, base_path: Path, kernel_path: Path,
           out_path: Path) -> str:
    name = base.name.split(b'\0', 1)[0].decode('ascii', 'replace')
    same = image.ramdisk == base.ramdisk
    lines = [
        f'base      : {base_path} (kernel {base.kernel_size} B, '
        f'ramdisk {base.ramdisk_size} B, name {name})',
        f'kernel    : {kernel_path} ({image.kernel_size} bytes)',
        f'ramdisk   : {image.ramdisk_size} bytes, '
        + ('copied byte for byte' if same else 'CHANGED (bug)'),
        f'page size : {image.page_size}, header v{image.header_version}',
        f'load addrs: kernel 0x{image.kernel_addr:08x} '
        f'ramdisk 0x{image.ramdisk_addr:08x} tags 0x{image.tags_addr:08x}',
        f'cmdline   : {image.cmdline}',
        f'image     : {out_path} ({len(image.data)} bytes)',
    ]
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--base', required=True, type=Path,
                        help='donor boot image (a pmOS boot.img for this phone)')
    parser.add_argument('--kernel', required=True, type=Path,
                        help='Image.gz with the board DTB appended')
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--debug-shell', action='store_true',
                        help=f'add {DEBUG_FLAG} so the initramfs drops to a shell')
    parser.add_argument('--cmdline-extra', default='', help='extra bootargs')
    parser.add_argument('--report', type=Path, help='write a summary here')
    args = parser.parse_args(argv)

    base = BootImage(args.base.read_bytes())
    kernel = args.kernel.read_bytes()
    if not kernel:
        raise SystemExit('kernel payload is empty')
    if kernel[:3] != b'\x1f\x8b\x08':
        raise SystemExit('kernel payload is not a gzip stream: expected the arm64 '
                         'Image.gz with the DTB appended (LK only decompresses gzip '
                         'and only reads an appended tree when dt_size == 0)')
    try:
        dtb_at = find_appended_dtb(kernel)
        check_gzip_boundary(kernel, dtb_at)
    except ValueError as exc:
        raise SystemExit(str(exc))
    print(f'kernel payload: {dtb_at} B Image.gz + '
          f'{len(kernel) - dtb_at} B appended DTB')
    image = build(base, kernel, debug_shell=args.debug_shell,
                  cmdline_extra=args.cmdline_extra)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(bytes(image.data))
    text = report(image, base, args.base, args.kernel, args.out)
    print(text, end='')
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
