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

# A third variant, for hunting the slab freelist corruption that panics this
# board after tens of minutes (victims fault in __kmalloc_track_caller with a
# poisoned freelist pointer, and one of them is always init). The parameter and
# /sys/kernel/slab/*/validate only exist with CONFIG_SLUB_DEBUG, which this
# vendor defconfig turns off ("# CONFIG_SLUB_DEBUG is not set"); build-kernel.sh
# re-enables it and asserts it, so an image that ignores this one is a build
# failure, not a quiet no-op. panic_on_oops=0 keeps
# the box alive after the first oops, so the report that names the corrupting
# path is not lost along with it.
slub_cmd = cmd + b' slub_debug=FZPU panic_on_oops=0'
assert len(slub_cmd) < 1536, 'Boot cmdline too long'
assert b'pmos.debug-shell' not in slub_cmd, 'Slab-debug image must still boot'
slub = bytearray(boot)
slub[64:576] = slub_cmd[:512].ljust(512, b'\0')
slub[608:1632] = slub_cmd[512:].ljust(1024, b'\0')
assert slub[page:] == boot[page:], 'Slab-debug image payload changed'
(folder / 'boot-slabdebug.img').write_bytes(slub)

# A fourth variant for the boot text. The kernel carries the fbcon patch in
# every image now (the veto is gone, so vtcon1 survives a refused bind), but
# binding *during* register_framebuffer() is what boot-looped run #35:
# mdss_fb_open() powers the panel up a second time while the boot loader still
# owns it. "fbcon=late" moves the takeover to late_initcall(), i.e. after the
# display driver is completely up -- the context the bind was proven in by hand
# on this phone. Appending a token to fbcon= cannot be done by tacking bytes
# onto the cmdline, because the whole string is one __setup() argument: it has
# to be substituted inside it, and the substitution must be asserted.
late_cmd = cmd.replace(b'fbcon=map:0,scrollback:256k',
                       b'fbcon=map:0,scrollback:256k,late')
assert late_cmd != cmd, ('the base cmdline no longer carries the exact fbcon= '
                         'argument this variant rewrites; refusing to build an '
                         'image that is just a copy of boot.img')
assert b'pmos.debug-shell' not in late_cmd, 'late-bind image must boot normally'
assert len(late_cmd) < 1536, 'Boot cmdline too long'
late = bytearray(boot)
late[64:576] = late_cmd[:512].ljust(512, b'\0')
late[608:1632] = late_cmd[512:].ljust(1024, b'\0')
assert late[page:] == boot[page:], 'Late-bind image payload changed'
(folder / 'boot-latebind.img').write_bytes(late)

# The one-image version of the two-step experiment: bind the console at
# late_initcall AND keep the allocator's own traps armed, so a corruption
# caused by the bind reports itself instead of killing init 25 minutes later.
# panic_on_oops=0 matters as much as FZPU here: without it the first victim
# (usually Xorg) takes the box down before the trap on the writing site is
# ever reached. U adds the user-space redzone check, which is what tells us
# whether anything lands in the tail past fbcon's softback_end.
combined_cmd = late_cmd + b' slub_debug=FZPU,U panic_on_oops=0'
assert len(combined_cmd) < 1536, 'Boot cmdline too long'
assert b'pmos.debug-shell' not in combined_cmd, 'combined image must boot normally'
combined = bytearray(boot)
combined[64:576] = combined_cmd[:512].ljust(512, b'\0')
combined[608:1632] = combined_cmd[512:].ljust(1024, b'\0')
assert combined[page:] == boot[page:], 'Combined image payload changed'
(folder / 'boot-latebind-slabdebug.img').write_bytes(combined)

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
    'slubdebug_cmdline': slub_cmd.decode(),
    'latebind_cmdline': late_cmd.decode(),
    'latebind_slubdebug_cmdline': combined_cmd.decode(),
    'payload_sha256': hashlib.sha256(boot[page:]).hexdigest(),
    'boot_partition_size_verified_on_phone': False,
}
(folder / 'image-info.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report, indent=2))
