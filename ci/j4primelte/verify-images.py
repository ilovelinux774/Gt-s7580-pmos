#!/usr/bin/env python3
"""Validate the image formats and produce a separate initramfs-debug boot image.

For boot header v0, cmdline is outside the kernel/ramdisk SHA-1 ID calculation.
Only cmdline header bytes change; the compiled kernel and ramdisk stay identical.
"""
from pathlib import Path
import hashlib
import json
import struct
import sys

folder = Path(sys.argv[1])
boot_path = folder / 'boot.img'
boot = bytearray(boot_path.read_bytes())
assert boot[:8] == b'ANDROID!', 'Not an Android boot image'
ksize, kaddr, rsize, raddr, ssize, saddr, tags, page, version, osver = struct.unpack_from('<10I', boot, 8)
assert version == 0, f'Unsupported boot header version: {version}'
assert page == 2048, f'Unexpected boot page size: {page}'
assert kaddr == 0x80008000 and raddr == 0x82000000, 'Unexpected load addresses'
assert 0 < ksize < len(boot) and 0 < rsize < len(boot), 'Empty/invalid payload'
align = lambda size: (size + page - 1) // page * page
assert len(boot) >= page + align(ksize) + align(rsize) + align(ssize), 'Truncated boot image'
# A guardrail, NOT a claim that the user's physical BOOT partition is this size.
assert len(boot) <= 32 * 1024 * 1024, 'Boot image exceeds the 32 MiB build guardrail'
cmd = bytes(boot[64:576]).split(b'\0', 1)[0] + bytes(boot[608:1632]).split(b'\0', 1)[0]
assert b'pmos.debug-shell' not in cmd, 'Normal boot unexpectedly stops in initramfs'
debug_cmd = cmd + b' pmos.debug-shell'
assert len(debug_cmd) < 1536, 'Boot cmdline too long'
debug = bytearray(boot)
debug[64:576] = debug_cmd[:512].ljust(512, b'\0')
debug[608:1632] = debug_cmd[512:].ljust(1024, b'\0')
assert debug[page:] == boot[page:], 'Debug image payload changed'
(folder / 'boot-debug.img').write_bytes(debug)

rootfs = folder / 'rootfs.img'
with rootfs.open('rb') as f:
    f.seek(1080)
    assert f.read(2) == b'\x53\xef', 'Rootfs is not a raw ext4 filesystem'
    f.seek(512)
    assert f.read(8) != b'EFI PART', 'Rootfs unexpectedly contains GPT'
report = {
    'target': 'Samsung Galaxy J4+ SM-J415F / samsung-j4primelte',
    'hardware_tested': False,
    'boot_header_version': version,
    'page_size': page,
    'boot_image_bytes': len(boot),
    'kernel_bytes': ksize,
    'ramdisk_bytes': rsize,
    'rootfs_bytes': rootfs.stat().st_size,
    'cmdline': cmd.decode(),
    'debug_cmdline': debug_cmd.decode(),
    'payload_sha256': hashlib.sha256(boot[page:]).hexdigest(),
    'boot_partition_size_verified_on_phone': False,
}
(folder / 'image-info.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report, indent=2))
