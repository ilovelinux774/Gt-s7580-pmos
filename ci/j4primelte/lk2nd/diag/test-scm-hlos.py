#!/usr/bin/env python3
"""Run actual J415F experimental lk2nd handoff code in synthetic ARM memory.

The secure monitor, Samsung loader, cache, DDR, persistence and phone are NOT
emulated. An SMC hook simulates an interrupted call followed by a failure to
check register ABI and marker sequencing. Never infer a physical boot from this.
"""
from pathlib import Path
import argparse
import random
import struct
import subprocess

from unicorn import Uc, UC_ARCH_ARM, UC_MODE_ARM, UC_HOOK_CODE, UC_HOOK_MEM_WRITE
from unicorn.arm_const import *

LOW_PARAM = 0x80055000
LOW_STUB = 0x80056000
ZONE = 0x860c0000
MAGIC = 0x43474244
RET = 0x90000000
STACK = 0x900ff000
PREFIX = b'J4DIAG-HLOS-1 '


def symbols(elf):
    out = {}
    for line in subprocess.check_output(['arm-none-eabi-nm', '-n', str(elf)], text=True).splitlines():
        f = line.split()
        if len(f) == 3:
            try:
                out[f[2]] = int(f[0], 16)
            except ValueError:
                pass
    return out


def decode(u):
    sig, start, size = struct.unpack('<III', bytes(u.mem_read(ZONE, 12)))
    if sig != MAGIC or size > 0x40000 - 12 or start > size:
        return None
    data = bytes(u.mem_read(ZONE + 12, size))
    return data[start:size] + data[:start]


def new(raw, sym, seed):
    u = Uc(UC_ARCH_ARM, UC_MODE_ARM)
    u.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_A15)
    u.reg_write(UC_ARM_REG_C1_C0_2, 0xf00000)
    u.reg_write(UC_ARM_REG_FPEXC, 0x40000000)
    for addr, size in ((0x80000000, 0x800000), (ZONE - 0x1000, 0x42000),
                       (0x8f600000, 0x400000), (0x90000000, 0x100000)):
        u.mem_map(addr, size)
    u.mem_write(sym['_start'], raw)
    rng = random.Random(seed)
    u.mem_write(ZONE - 0x1000, rng.randbytes(0x42000))
    u.reg_write(UC_ARM_REG_SP, STACK)
    u.reg_write(UC_ARM_REG_LR, RET)
    return u


def direct_stub(raw, sym, seed):
    u = new(raw, sym, seed)
    rng = random.Random(seed)
    saved = {r: rng.getrandbits(32) for r in (UC_ARM_REG_R4, UC_ARM_REG_R5,
             UC_ARM_REG_R6, UC_ARM_REG_R7, UC_ARM_REG_R8, UC_ARM_REG_R9,
             UC_ARM_REG_R10, UC_ARM_REG_R11)}
    for r, v in saved.items():
        u.reg_write(r, v)
    args = (0x200010f, 0x12, LOW_PARAM, 80, 0x99887766, 0x01234567)
    for r, v in zip((UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3), args):
        u.reg_write(r, v)
    u.mem_write(STACK, struct.pack('<2I', *args[4:]))
    seen = []
    def smc(uc, pc, size, data):
        regs = tuple(uc.reg_read(r) for r in (UC_ARM_REG_R0, UC_ARM_REG_R1,
                     UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6))
        seen.append(regs)
        uc.reg_write(UC_ARM_REG_R0, 1 if len(seen) == 1 else 0xfffffffd)
        uc.reg_write(UC_ARM_REG_PC, pc + 4)
    u.hook_add(UC_HOOK_CODE, smc, begin=LOW_STUB + 32, end=LOW_STUB + 32)
    u.emu_start(sym['j4_hlos_smc'], RET, count=100000)
    assert u.reg_read(UC_ARM_REG_PC) == RET
    assert seen[0] == args + (0,), seen
    assert seen[1] == (1,) + args[1:] + (0,), seen
    assert len(seen) == 2 and u.reg_read(UC_ARM_REG_R0) == 0xfffffffd
    assert u.reg_read(UC_ARM_REG_SP) == STACK
    assert all(u.reg_read(r) == v for r, v in saved.items()), 'callee-saved registers changed'


def handoff(raw, sym, seed, modern, lazy):
    u = new(raw, sym, seed)
    before = bytes(u.mem_read(ZONE - 0x1000, 0x42000))
    # Execute the actual is_scm_armv8_support() and scm_init(). Either provide
    # a previously cached result, or stub just the secure availability query.
    # This tests control flow, not the true device's monitor capabilities.
    u.mem_write(sym['scm_initialized'], struct.pack('<I', 0 if lazy else 1))
    u.mem_write(sym['scm_arm_support'], struct.pack('<I', 0 if lazy else modern))
    queries = []
    u.reg_write(UC_ARM_REG_R0, 0x80080000)
    u.reg_write(UC_ARM_REG_R1, 0x83400000)
    commits = []
    writes = []
    def write(uc, access, address, size, value, data):
        if ZONE - 0x1000 <= address < ZONE + 0x41000:
            writes.append((address, size, value))
            assert ZONE <= address and address + size <= ZONE + 12 + 192
            if address == ZONE and size == 4 and value == MAGIC:
                start, n = struct.unpack('<II', bytes(uc.mem_read(ZONE + 4, 8)))
                assert start == n and 0 < n <= 192
                commits.append(bytes(uc.mem_read(ZONE + 12, n)))
    u.hook_add(UC_HOOK_MEM_WRITE, write)
    def ret(v=0):
        u.reg_write(UC_ARM_REG_R0, v & 0xffffffff)
        u.reg_write(UC_ARM_REG_PC, u.reg_read(UC_ARM_REG_LR))
    def hook_printf(uc, pc, size, data):
        ret()
    def availability(uc, pc, size, data):
        assert (uc.reg_read(UC_ARM_REG_R0), uc.reg_read(UC_ARM_REG_R1)) == (6, 1)
        queries.append(modern)
        ret(modern)
    def panic(uc, pc, size, data):
        uc.emu_stop()
    for name, fun in (('_dprintf', hook_printf), ('is_scm_call_available', availability),
                      ('_panic', panic), ('__stack_chk_fail', panic)):
        u.hook_add(UC_HOOK_CODE, fun, begin=sym[name], end=sym[name])
    seen = []
    if modern:
        def smc(uc, pc, size, data):
            regs = tuple(uc.reg_read(r) for r in (UC_ARM_REG_R0, UC_ARM_REG_R1,
                         UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6))
            seen.append(regs)
            assert decode(uc) == PREFIX + b'A1: ARMv8 HLOS SMC path\n'
            words = struct.unpack('<10Q', bytes(uc.mem_read(LOW_PARAM, 80)))
            assert words == (0x83400000,) + (0,) * 8 + (0x80080000,), words
            uc.reg_write(UC_ARM_REG_R0, 1 if len(seen) == 1 else 0xfffffffe)
            uc.reg_write(UC_ARM_REG_PC, pc + 4)
        u.hook_add(UC_HOOK_CODE, smc, begin=LOW_STUB + 32, end=LOW_STUB + 32)
    else:
        def legacy(uc, pc, size, data):
            regs = tuple(uc.reg_read(r) for r in (UC_ARM_REG_R0, UC_ARM_REG_R1,
                         UC_ARM_REG_R2, UC_ARM_REG_R3))
            seen.append(regs)
            assert regs[:2] == (1, 15) and regs[3] == 80, regs
            assert decode(uc) == PREFIX + b'A1: legacy SCM path\n'
            words = struct.unpack('<10Q', bytes(uc.mem_read(regs[2], 80)))
            assert words == (0x83400000,) + (0,) * 8 + (0x80080000,), words
            ret(0xfffffffe)
        u.hook_add(UC_HOOK_CODE, legacy, begin=sym['scm_call'], end=sym['scm_call'])
    u.emu_start(sym['scm_elexec_call'], RET, count=200000)
    assert u.reg_read(UC_ARM_REG_PC) == sym['_panic'], hex(u.reg_read(UC_ARM_REG_PC))
    assert struct.unpack('<I', bytes(u.mem_read(sym['scm_initialized'], 4)))[0] == 1
    assert struct.unpack('<I', bytes(u.mem_read(sym['scm_arm_support'], 4)))[0] == modern
    assert queries == ([modern] if lazy else []), queries
    expected_middle = (b'A1: ARMv8 HLOS SMC path\n' if modern else b'A1: legacy SCM path\n')
    assert commits == [PREFIX + b'A0: lk2nd entered SCM handoff\n', PREFIX + expected_middle,
                       PREFIX + b'R: SCM returned, no handoff\n'], commits
    assert decode(u) == commits[-1]
    if modern:
        assert len(seen) == 2 and seen[0] == (0x200010f, 0x12, LOW_PARAM, 80, 0, 0, 0), seen
        assert seen[1] == (1, 0x12, LOW_PARAM, 80, 0, 0, 0), seen
    else:
        assert len(seen) == 1
    after = bytes(u.mem_read(ZONE - 0x1000, 0x42000))
    assert before[:0x1000] == after[:0x1000]
    assert before[0x1000 + 12 + 192:] == after[0x1000 + 12 + 192:]
    assert writes
    return len(writes)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('elf', type=Path)
    p.add_argument('bin', type=Path)
    a = p.parse_args()
    sym = symbols(a.elf)
    raw = a.bin.read_bytes()
    assert sym['_start'] == 0x8f600000 and sym['__data_end'] - sym['_start'] == len(raw)
    for seed in range(16):
        direct_stub(raw, sym, seed)
        for lazy in (0, 1):
            modern_writes = handoff(raw, sym, seed, 1, lazy)
            legacy_writes = handoff(raw, sym, seed, 0, lazy)
    print('PASS 16 randomized cases per SCM branch and cached/lazy detection mode')
    print('PASS actual is_scm_armv8_support/scm_init, with simulated availability response')
    print('PASS HLOS SCM inputs x0=0x0200010f x1=0x12 x2=0x80055000 x3=80; DTB/ELR in 80-byte block')
    print('PASS simulated interruption retry and error return; AAPCS r4-r11/SP preserved')
    print(f'PASS A0, A1, R vendor-ring messages and bounded writes ({modern_writes} modern / {legacy_writes} legacy)')
    print('LIMIT: no real SMC, Samsung loader, caches, physical RAM, reset retention or hardware boot')


if __name__ == '__main__':
    main()
