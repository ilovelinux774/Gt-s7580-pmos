#!/usr/bin/env python3
"""Static checks for J415FXXU6BVJ1 ARM64 loader compatibility.

The address model is based on this exact bootloader's disassembly and a recorded
2-GiB SM-J415F memory map, not a guarantee for every phone or firmware. Secure
monitor behavior, actual SMEM table ordering, hardware and kernel boot are not
emulated here. Requires the distro's fdtget (device-tree-compiler) tool.
"""
import hashlib
import shutil
import struct
import subprocess
import zlib
from pathlib import Path

LOADER_RAM_BASE = 0x80000000
LOADER_KERNEL = LOADER_RAM_BASE + 0x80000
LOADER_TAGS = LOADER_RAM_BASE + 0x3400000
LOADER_INITRD = LOADER_RAM_BASE + 0x3600000
SCRATCH = 0xa0100000  # the inspected loader's >512-MiB RAM case
SCRATCH_END = 0xc0000000
INNER_OFFSET = 0x180000
EXPECTED_MARKER = b'J4A64-SHIM-v1\0'


def complete_gzip(blob):
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    result = d.decompress(blob) + d.flush()
    if not d.eof or d.unused_data:
        raise ValueError('expected exactly one complete gzip stream')
    return result


def validate_wrapper(image: bytes, original: bytes):
    """Reconstruct the only permitted shim, independently of the writer."""
    if len(image) < INNER_OFFSET + 64:
        raise ValueError('missing J4 ARM64 wrapper or inner Image')
    inner = image[INNER_OFFSET:]
    if inner[56:60] != b'ARM\x64':
        raise ValueError('inner Image has no ARM64 magic at aligned entry')
    inner_offset, inner_size, flags = struct.unpack_from('<QQQ', inner, 8)
    if inner_offset != 0 or not inner_size or inner_size < len(inner):
        raise ValueError('inner ARM64 text_offset or effective size is invalid')
    if inner != original:
        raise ValueError('inner Image differs from the built original')
    # AArch64: nop; b +60 to the stub; at 0x40, b +0x17ffc0 to
    # the original Image. These instructions do not alter x0-x3 or SP.
    expected = bytearray(INNER_OFFSET)
    struct.pack_into('<IIQQQ', expected, 0, 0xd503201f, 0x1400000f,
                     0x80000, INNER_OFFSET + inner_size, flags)
    expected[56:60] = b'ARM\x64'
    struct.pack_into('<I', expected, 0x40, 0x1405fff0)
    expected[0x44:0x44 + len(EXPECTED_MARKER)] = EXPECTED_MARKER
    if image[:INNER_OFFSET] != expected:
        raise ValueError('wrapper header, branch encoding or padding was changed')
    if (LOADER_KERNEL + INNER_OFFSET) % 0x200000:
        raise ValueError('inner Image is not 2 MiB aligned in the loader model')
    return {'inner_sha256': hashlib.sha256(inner).hexdigest(),
            'outer_size': INNER_OFFSET + inner_size,
            'inner_entry': LOADER_KERNEL + INNER_OFFSET}


def fdtget(dtb: Path, node: str, prop=None, fmt=None, listing=False):
    tool = shutil.which('fdtget')
    if not tool:
        raise ValueError('fdtget is required to audit reserved memory')
    args = [tool]
    if listing:
        args.append('-l')
    elif prop is None:
        args.append('-p')
    elif fmt:
        args += ['-t', fmt]
    args += [str(dtb), node]
    if prop is not None:
        args.append(prop)
    result = subprocess.run(args, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def properties(dtb, node):
    value = fdtget(dtb, node)
    return set(value.splitlines()) if value is not None else None


def words(dtb, node, prop):
    value = fdtget(dtb, node, prop, 'x')
    if value is None:
        return None
    try:
        return [int(x, 16) for x in value.split()]
    except ValueError as exc:
        raise ValueError(f'{node}: invalid {prop} cells') from exc


def tuple_ranges(values, what):
    if values is None or not values or len(values) % 4:
        raise ValueError(f'{what}: expected 2-address-cell/2-size-cell reg tuples')
    result = []
    for i in range(0, len(values), 4):
        address = values[i] << 32 | values[i + 1]
        size = values[i + 2] << 32 | values[i + 3]
        if not size or address + size > 1 << 64:
            raise ValueError(f'{what}: zero-sized or overflowing active reservation')
        result.append((address, address + size))
    return result


def overlaps(a, b):
    return a[0] < b[1] and b[0] < a[1]


def reservations(dtb: Path):
    """Collect each active reserved-memory reg tuple plus FDT reserve-map entries."""
    if words(dtb, '/reserved-memory', '#address-cells') != [2] or \
       words(dtb, '/reserved-memory', '#size-cells') != [2]:
        raise ValueError('/reserved-memory must use two address and two size cells')
    children = fdtget(dtb, '/reserved-memory', listing=True)
    if children is None:
        raise ValueError('missing /reserved-memory')
    result = []
    for child in children.splitlines():
        node = '/reserved-memory/' + child
        props = properties(dtb, node)
        if props is None:
            raise ValueError(f'cannot read {node}')
        status = fdtget(dtb, node, 'status', 's')
        if status is not None and status not in ('ok', 'okay'):
            continue
        if 'reg' not in props:
            # Dynamic enabled allocations cannot be bounded by this static audit.
            raise ValueError(f'{node}: active reservation has no static reg')
        for low, high in tuple_ranges(words(dtb, node, 'reg'), node):
            result.append((node, low, high))
    data = dtb.read_bytes()
    if len(data) < 40 or struct.unpack_from('>I', data, 0)[0] != 0xd00dfeed:
        raise ValueError('invalid FDT header')
    position = struct.unpack_from('>I', data, 16)[0]
    while True:
        if position + 16 > len(data):
            raise ValueError('unterminated FDT memory reserve map')
        start, size = struct.unpack_from('>QQ', data, position)
        position += 16
        if not start and not size:
            break
        if not size or start + size > 1 << 64:
            raise ValueError('invalid FDT memory reserve-map entry')
        result.append(('FDT reserve-map', start, start + size))
    for i, a in enumerate(result):
        for b in result[i + 1:]:
            if overlaps(a[1:], b[1:]):
                raise ValueError(f'reservations overlap: {a[0]} and {b[0]}')
    return result


def audit_reservations(dtb: Path, occupied=()):
    regions = reservations(dtb)
    for name, low, high in occupied:
        for node, rlow, rhigh in regions:
            if overlaps((low, high), (rlow, rhigh)):
                raise ValueError(f'{name} overlaps {node}: '
                                 f'{low:#x}-{high:#x} vs {rlow:#x}-{rhigh:#x}')
    return regions


def require_loader_nodes(dtb: Path):
    """Check the pre-fixup compatibility nodes this Samsung loader requires."""
    expected = {
        '/reserved-memory/lk_region@0': [(0x8f600000, 0x8f900000)],
        '/reserved-memory/sec-debug@85000000': [(0x85000000, 0x85800000)],
        '/reserved-memory/modem_shared_mem_region@0x93000000':
            [(0x93000000, 0x95000000), (0x97800000, 0x99000000)],
        '/reserved-memory/qseecom@95000000': [(0x95000000, 0x97800000)],
    }
    for node, ranges in expected.items():
        props = properties(dtb, node)
        if props is None or 'no-map' not in props:
            raise ValueError(f'{node}: missing required no-map node')
        if tuple_ranges(words(dtb, node, 'reg'), node) != ranges:
            raise ValueError(f'{node}: loader-compatible ranges changed')
        status = fdtget(dtb, node, 'status', 's')
        if status is not None and status not in ('okay', 'ok'):
            raise ValueError(f'{node}: required reservation disabled')
    for node in ('/reserved-memory/sec_debug_region@0',
                 '/reserved-memory/klog_region@0'):
        if properties(dtb, node) is None or \
           fdtget(dtb, node, 'status', 's') != 'disabled' or \
           words(dtb, node, 'reg') != [0, 0, 0, 0]:
            raise ValueError(f'{node}: missing disabled zero-sized loader placeholder')
    parent = '/soc/qcom,memshare'
    if fdtget(dtb, parent, 'status', 's') != 'disabled':
        raise ValueError('missing or enabled loader-only memshare parent')
    if words(dtb, parent + '/qcom,client_4', 'qcom,peripheral-size') != [0x6000000]:
        raise ValueError('missing Samsung memshare client_4 / peripheral-size')


def runtime_layout(image: bytes, tree_size: int, boot_size: int, ramdisk_size: int,
                   page_size: int):
    if len(image) < 64 or image[56:60] != b'ARM\x64':
        raise ValueError('not an ARM64 Image')
    size = struct.unpack_from('<Q', image, 16)[0]
    if not size or size < len(image):
        raise ValueError('invalid effective ARM64 Image size')
    if tree_size <= 0 or tree_size + 0x10000 > 0x200000:
        raise ValueError('DTB plus fixup allowance is too large')
    if page_size != 2048:
        raise ValueError('inspected J4+ bootloader expects 2048-byte pages')
    output = SCRATCH + boot_size + page_size
    occupied = [
        ('kernel including effective BSS', LOADER_KERNEL, LOADER_KERNEL + size),
        ('tags with fixup allowance', LOADER_TAGS, LOADER_TAGS + tree_size + 0x10000),
        ('initramfs', LOADER_INITRD, LOADER_INITRD + ramdisk_size),
        ('loader scratch input', SCRATCH, SCRATCH + boot_size),
        ('loader scratch decompressed', output, output + len(image)),
    ]
    for name, low, high in occupied:
        if not 0x80000000 <= low < high <= SCRATCH_END:
            raise ValueError(f'{name} lies outside the recorded first 1-GiB RAM bank')
    if occupied[-1][2] > SCRATCH_END:
        raise ValueError('decompressed image exceeds the inspected scratch buffer')
    for i, a in enumerate(occupied):
        for b in occupied[i + 1:]:
            if overlaps(a[1:], b[1:]):
                raise ValueError(f'runtime regions overlap: {a[0]} and {b[0]}')
    return occupied
