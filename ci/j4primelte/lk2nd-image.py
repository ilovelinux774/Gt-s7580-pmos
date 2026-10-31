#!/usr/bin/env python3
"""Build and independently inspect a J415F BOOT-only lk2nd + M1 debug image.

Geckyn's pinned msm8917-secondary project splits BOOT in RAM at **1 MiB**;
this is not the newer upstream 512-KiB layout. The first Android v0 image
contains a plain-gzip ARM32 lk.bin followed immediately by the J4+ lk2nd DTB,
with an empty outer ramdisk. At 1 MiB comes the complete Android v0 M1 debug
image, including the unmodified #37 initramfs. No rootfs image is generated.

The primary Samsung loader, secondary lk2nd, secure firmware and hardware are
not validated by this file. A successful check is not a device boot.
"""
from pathlib import Path
import argparse
import gzip
import hashlib
import importlib.util
import struct
import subprocess
import sys
import zlib

HERE = Path(__file__).resolve().parent
OFFSET = 1024 * 1024
PARTITION_SIZE = 32 * 1024 * 1024  # measured mmcblk0p23 (32768 KiB)
DONOR_SHA256 = '3ba9fa06e0d410dbd2e1f8e700090ab1eebc68f491a40dcfde53d805373ae80e'
DONOR_ROOT_UUID = '86e7262b-b628-4b89-8a6e-e96ef9b1aab6'
MAGIC = b'ANDROID!'
FDT_MAGIC = bytes.fromhex('d00dfeed')


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, str(HERE / filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


repack = load('j4_lk2nd_repack', 'mainline-repack-boot.py')


def require(ok, message):
    if not ok:
        raise ValueError(message)


def align(n, page):
    return (n + page - 1) // page * page


def parse_boot(data, *, require_ramdisk):
    """Independent Android-v0 header parser; accepts an empty *outer* ramdisk."""
    require(len(data) >= 2048 and data[:8] == MAGIC, 'missing Android v0 header')
    ks, ka, rs, ra, ss, sa, tags, page, version, osver = struct.unpack_from('<10I', data, 8)
    require(page == 2048 and version == 0, 'expected J4+ 2048-byte-page Android v0 image')
    require(ks > 0 and (not require_ramdisk or rs > 0), 'empty kernel or required ramdisk')
    end = page + align(ks, page) + align(rs, page) + align(ss, page)
    require(len(data) >= end, 'truncated boot image or padding')
    kernel = data[page:page + ks]
    rpos = page + align(ks, page)
    ramdisk = data[rpos:rpos + rs]
    spos = rpos + align(rs, page)
    second = data[spos:spos + ss]
    digest = hashlib.sha1()
    for blob in (kernel, ramdisk, second):
        digest.update(blob)
        digest.update(struct.pack('<I', len(blob)))
    require(data[576:608] == digest.digest().ljust(32, b'\0'), 'Android v0 image ID mismatch')
    cmdline = (data[64:576].split(b'\0', 1)[0] + data[608:1632].split(b'\0', 1)[0]).decode('ascii', 'strict')
    return dict(kernel=kernel, ramdisk=ramdisk, second=second, end=end,
                cmdline=cmdline, page=page, version=version,
                addrs=(ka, ra, sa, tags), osver=osver)


def check_fdt_blob(tree):
    require(len(tree) >= 40 and tree[:4] == FDT_MAGIC, 'not a flattened device tree')
    require(struct.unpack_from('>I', tree, 4)[0] == len(tree), 'DTB totalsize is incorrect')


def unpack_gzip_dtb(payload):
    require(payload[:3] == b'\x1f\x8b\x08' and payload[3] == 0,
            'kernel must be plain gzip without optional fields')
    decomp = zlib.decompressobj(31)
    try:
        binary = decomp.decompress(payload) + decomp.flush()
    except zlib.error as exc:
        raise ValueError('invalid gzip kernel') from exc
    require(decomp.eof, 'truncated gzip kernel')
    tree = decomp.unused_data
    check_fdt_blob(tree)
    return binary, tree


def root_uuid(cmdline):
    values = [v.split('=', 1)[1] for v in cmdline.split() if v.startswith('pmos_root_uuid=')]
    require(len(values) == 1, 'expected exactly one root UUID')
    return values[0]


def check_inner(inner, donor):
    """Reject rootfs-mode images and anything that changes the donor initramfs."""
    parsed, base = parse_boot(inner, require_ramdisk=True), parse_boot(donor, require_ramdisk=True)
    require(len(inner) == parsed['end'], 'second Android image has extra or missing bytes')
    require(parsed['ramdisk'] == base['ramdisk'], 'second image changed donor initramfs')
    require(not parsed['second'] and not base['second'], 'unexpected Android second payload')
    require(parsed['addrs'][1:] == base['addrs'][1:] and parsed['osver'] == base['osver'],
            'second image changed donor header fields')
    require(root_uuid(parsed['cmdline']) == root_uuid(base['cmdline']), 'root UUID changed')
    flags = parsed['cmdline'].split()
    require(flags.count('pmos.debug-shell') == 1, 'second image is not debug-shell mode')
    require(flags.count('pstore.compress=none') == 1, 'missing pstore diagnostic argument')
    # This old lk2nd passes hdr->cmdline (first 512 B) rather than appending
    # extra_cmdline, so all essential flags must fit entirely in that field.
    require(not inner[608:1632].split(b'\0', 1)[0], 'second cmdline exceeds lk2nd first field')
    first = inner[64:576].split(b'\0', 1)[0].decode('ascii').split()
    require('pmos.debug-shell' in first and f'pmos_root_uuid={root_uuid(base["cmdline"])}' in first,
            'debug/root arguments are not visible to lk2nd')
    binary, tree = unpack_gzip_dtb(parsed['kernel'])
    require(len(binary) >= 64 and binary[56:60] == b'ARM\x64', 'second kernel is not ARM64')
    text_offset, image_size = struct.unpack_from('<QQ', binary, 8)
    require(text_offset == 0x80000 and image_size >= len(binary), 'unexpected ARM64 shim header')
    expected_addr = (base['addrs'][0] & ~0x1fffff) + text_offset
    require(parsed['addrs'][0] == expected_addr, 'second image kernel header address is unexpected')
    # Model the fixed destinations used by this lk2nd; physical RAM and firmware
    # behavior must still be validated on the phone.
    require(0x80080000 + image_size <= 0x83400000,
            'effective ARM64 Image overlaps lk2nd tags address')
    return parsed, binary, tree


def build_combined(donor, lk_binary, lk_dtb, inner):
    check_inner(inner, donor)
    check_fdt_blob(lk_dtb)
    require(len(lk_binary) >= 64 and len(lk_binary) <= 3 * 1024 * 1024,
            'invalid lk2nd binary size')
    require(lk_binary[56:60] != b'ARM\x64', 'outer lk2nd must load as ARM32, not ARM64')
    require(struct.unpack_from('<I', lk_binary)[0] & 0xff000000 == 0xea000000,
            'lk2nd entry is not the expected ARM branch instruction')
    # Python 3.11/3.12 may delegate mtime=0 gzip to zlib and emit OS=Unix;
    # Python 3.13 emits OS=unknown. Normalize that metadata byte for the same
    # input, compiler and zlib version; gzip CRC covers the body, not this byte.
    gz = bytearray(gzip.compress(lk_binary, compresslevel=9, mtime=0))
    gz[9] = 255
    payload = bytes(gz) + lk_dtb
    base = repack.BootImage(donor)
    outer = base.repack(payload, ramdisk=b'', second=b'', cmdline='lk2nd',
                        kernel_addr=base.kernel_addr)
    outer_bytes = bytes(outer.data)
    require(len(outer_bytes) < OFFSET, 'outer lk2nd image exceeds the 1-MiB split')
    image = outer_bytes + bytes(OFFSET - len(outer_bytes)) + inner
    require(len(image) <= PARTITION_SIZE, 'combined image exceeds measured 32-MiB BOOT')
    return image


def verify_combined(image, donor, lk_binary, lk_dtb, inner):
    """Independently parse both finished headers and compare every input byte."""
    require(len(image) <= PARTITION_SIZE and len(image) > OFFSET,
            'combined BOOT length invalid')
    outer = parse_boot(image, require_ramdisk=False)
    require(outer['end'] < OFFSET, 'first Android image crosses split')
    require(outer['cmdline'] == 'lk2nd', 'outer bootargs must be exactly lk2nd')
    require(not outer['ramdisk'] and not outer['second'], 'outer image must not carry an initramfs')
    base = parse_boot(donor, require_ramdisk=True)
    require(outer['addrs'] == base['addrs'] and outer['osver'] == base['osver'],
            'outer image changed donor header fields')
    require(image[outer['end']:OFFSET] == bytes(OFFSET - outer['end']), 'nonzero split padding')
    binary, tree = unpack_gzip_dtb(outer['kernel'])
    require(binary == lk_binary and tree == lk_dtb, 'outer kernel/DTB changed during packing')
    require(image[OFFSET:] == inner, 'embedded second image is not byte-identical')
    parsed, kernel, dtb = check_inner(image[OFFSET:], donor)
    return dict(total=len(image), outer_end=outer['end'], inner_start=OFFSET,
                inner_length=len(inner), lk_binary=len(binary), inner_kernel=len(kernel),
                outer_dtb_sha256=hashlib.sha256(tree).hexdigest(),
                inner_dtb_sha256=hashlib.sha256(dtb).hexdigest(),
                ramdisk_sha256=hashlib.sha256(parsed['ramdisk']).hexdigest(),
                combined_sha256=hashlib.sha256(image).hexdigest())


def check_outer_dtb(path):
    """Check the compiled J4+ lk2nd DTB, not the separate Linux device tree."""
    def get(node, prop, kind='x'):
        try:
            return subprocess.check_output(['fdtget', '-t', kind, str(path), node, prop],
                                           text=True, stderr=subprocess.PIPE).strip()
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            raise ValueError(f'cannot read DTB {node} {prop}: {exc}') from exc
    def nums(node, prop):
        return [int(x, 16) for x in get(node, prop).split()]
    require(nums('/', 'qcom,msm-id') == [303, 0, 308, 0, 309, 0], 'wrong J4 SoC IDs')
    require(nums('/', 'qcom,board-id') == [8, 6], 'wrong J4 board revision')
    require(nums('/', 'qcom,pmic-id') == [0x10019, 0, 0, 0], 'wrong J4 PMIC ID')
    require(get('/j4primelte-generic', 'lk2nd,match-bootloader', 's') == 'J415*',
            'wrong lk2nd bootloader match')
    require(nums('/j4primelte-generic', 'lk2nd,pstore') == [0x86100000, 0x100000],
            'wrong lk2nd pstore area')
    require(nums('/reserved-memory/lk_region@0', 'reg') == [0, 0x8f600000, 0, 0x300000],
            'wrong outer LK reservation')
    require(nums('/reserved-memory/sec_debug_region@0', 'reg') == [0, 0x85000000, 0, 0x800000],
            'wrong outer secure-debug reservation')
    require(nums('/reserved-memory/klog_region@0', 'reg') == [0, 0x851ff000, 0, 0x201000],
            'wrong outer Samsung klog reservation')
    require(nums('/reserved-memory/modem_shared_mem_region@0x93000000', 'reg') == [0, 0x93000000, 0, 0x6000000],
            'wrong outer modem-shared reservation')
    require(nums('/soc/qcom,memshare/qcom,client_4', 'qcom,peripheral-size') == [0x6000000],
            'wrong outer memshare client')
    nodes = ('/reserved-memory/lk_region@0', '/reserved-memory/sec_debug_region@0',
             '/reserved-memory/klog_region@0', '/reserved-memory/modem_shared_mem_region@0x93000000',
             '/soc/qcom,memshare/qcom,client_4', '/memory', '/chosen')
    for node in nodes:
        try:
            props = subprocess.check_output(['fdtget', '-p', str(path), node],
                                            text=True, stderr=subprocess.PIPE).splitlines()
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            raise ValueError(f'missing Samsung-loader outer node {node}') from exc
        if node in nodes[:3]:
            require('no-map' in props, f'missing no-map in outer {node}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--donor', type=Path, required=True)
    parser.add_argument('--lk-binary', type=Path, required=True)
    parser.add_argument('--lk-dtb', type=Path, required=True)
    parser.add_argument('--inner', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    try:
        donor = args.donor.read_bytes()
        require(hashlib.sha256(donor).hexdigest() == DONOR_SHA256, 'unexpected #37 donor SHA-256')
        require(root_uuid(parse_boot(donor, require_ramdisk=True)['cmdline']) == DONOR_ROOT_UUID,
                'unexpected #37 donor root UUID')
        check_outer_dtb(args.lk_dtb)
        lk_binary, lk_dtb, inner = args.lk_binary.read_bytes(), args.lk_dtb.read_bytes(), args.inner.read_bytes()
        image = build_combined(donor, lk_binary, lk_dtb, inner)
        report = verify_combined(image, donor, lk_binary, lk_dtb, inner)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_bytes(image)
        require(args.out.read_bytes() == image, 'written image does not round-trip')
        lines = ['J415F lk2nd + mainline M1 debug, experimental and not device-tested',
                 'Layout: geckyn msm8917-secondary 1-MiB split, not upstream 512 KiB',
                 'The outer image carries no initramfs; the inner debug image carries #37 initramfs.',
                 'No rootfs image is included. Debug mode is expected to stop before normal rootfs mount.',
                 'A successful static verification does not establish a phone boot.']
        lines += [f'{k}={v}' for k, v in report.items()]
        text = '\n'.join(lines) + '\n'
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(text)
        print(text, end='')
    except (OSError, ValueError, UnicodeError, struct.error) as exc:
        print(f'lk2nd image refused: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
