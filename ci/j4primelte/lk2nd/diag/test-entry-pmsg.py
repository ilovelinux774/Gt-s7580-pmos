#!/usr/bin/env python3
"""Exercise actual experimental ARM64 entry bytes in synthetic memory only."""
from pathlib import Path
import argparse
import gzip
import random
import struct
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_MEM_WRITE
from unicorn import arm64_const
from unicorn.arm64_const import UC_ARM64_REG_NZCV, UC_ARM64_REG_PC, UC_ARM64_REG_SP

BASE = 0x80080000
INNER = 0x180000
ZONE = 0x860c0000
MAGIC = 0x43474244
MSG = b'J4DIAG-HLOS-1 B: AArch64 wrapper reached; Linux unproven\n'


def decode(data):
    sig, start, n = struct.unpack_from('<III', data)
    if sig != MAGIC or n == 0 or n > 0x40000 - 12 or start > n:
        return None
    ring = data[12:12 + n]
    return ring[start:n] + ring[:start]


def run_case(image, inner, seed):
    u = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    # Wrapper through original entry; only the first few bytes of the original
    # Image are needed because execution stops at its entry.
    u.mem_map(0x80000000, 0x400000)
    u.mem_write(BASE, image[:INNER + 4096])
    u.mem_map(ZONE - 0x1000, 0x42000)
    rng = random.Random(seed)
    before = bytearray(rng.randbytes(0x42000))
    struct.pack_into('<III', before, 0x1000, MAGIC, 4, 4)
    before[0x100c:0x1010] = b'OLD\n'
    u.mem_write(ZONE - 0x1000, bytes(before))
    regs = {getattr(arm64_const, f'UC_ARM64_REG_X{i}'): rng.getrandbits(64) for i in range(31)}
    regs[arm64_const.UC_ARM64_REG_X0] = 0x83400000
    for i in (1, 2, 3):
        regs[getattr(arm64_const, f'UC_ARM64_REG_X{i}')] = 0
    regs[UC_ARM64_REG_SP] = 0x87f00000
    regs[UC_ARM64_REG_NZCV] = (seed & 0xf) << 28
    for register, value in regs.items():
        u.reg_write(register, value)
    writes = []
    def written(_u, _access, address, size, value, _data):
        writes.append((address, size, value))
    u.hook_add(UC_HOOK_MEM_WRITE, written)
    target = BASE + INNER
    u.emu_start(BASE, target, count=10000)
    assert u.reg_read(UC_ARM64_REG_PC) == target
    for i in list(range(0, 9)) + list(range(15, 31)):
        reg = getattr(arm64_const, f'UC_ARM64_REG_X{i}')
        assert u.reg_read(reg) == regs[reg], f'x{i} changed'
    for reg in (UC_ARM64_REG_SP, UC_ARM64_REG_NZCV):
        assert u.reg_read(reg) == regs[reg]
    assert bytes(u.mem_read(target, 4096)) == inner[:4096]
    after = bytearray(u.mem_read(ZONE - 0x1000, len(before)))
    assert before[:0x1000] == after[:0x1000]
    assert before[0x1000 + 12 + len(MSG):] == after[0x1000 + 12 + len(MSG):]
    assert decode(after[0x1000:0x41000]) == MSG
    assert writes[0] == (ZONE, 4, 0) and writes[-1] == (ZONE, 4, MAGIC)
    assert all(ZONE <= a and a + n <= ZONE + 12 + len(MSG) for a, n, _ in writes)
    shadow = before[0x1000:0x41000]
    for i, (addr, n, value) in enumerate(writes):
        off = addr - ZONE
        shadow[off:off + n] = value.to_bytes(n, 'little')
        assert decode(shadow) == (MSG if i == len(writes) - 1 else None)
    return len(writes)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--wrapped', type=Path, required=True)
    p.add_argument('--original', type=Path, required=True)
    a = p.parse_args()
    image, inner = gzip.decompress(a.wrapped.read_bytes()), gzip.decompress(a.original.read_bytes())
    assert image[56:60] == b'ARM\x64' and image[INNER:] == inner
    assert image.count(MSG) == 1
    assert struct.unpack_from('<QQ', image, 8)[0] == 0x80000
    for seed in range(16):
        n = run_case(image, inner, seed)
    print(f'PASS: 16 randomized ARM64 emulations, {n} bounded pmsg writes')
    print('PASS: stage B decoded, original Image unchanged, x0-x8/x15-x30/SP/NZCV preserved')
    print('LIMIT: monitor handoff, real cache, retention, Linux and hardware were not tested')


if __name__ == '__main__':
    main()
