#!/usr/bin/env python3
"""Synthetic offline checks for the Medusa built-in A300 firmware verifier."""
from pathlib import Path
import gzip
import importlib.util
import struct
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('firmware_check', HERE / 'verify-medusa64-firmware.py')
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)

CFG = '\n'.join(('CONFIG_FW_LOADER=y', 'CONFIG_FIRMWARE_IN_KERNEL=y',
                 'CONFIG_EXTRA_FIRMWARE="a300_pm4.fw a300_pfp.fw"',
                 'CONFIG_EXTRA_FIRMWARE_DIR="firmware"')) + '\n'


def fixture_elf(pm4, pfp, *, duplicate=False, missing=False, invalid_pointer=False,
                bad_size=False, typ=2, machine=183):
    names = b'\0.shstrtab\0.rodata\0.builtin_fw\0'
    ro_addr, table_addr = 0xffff000000400000, 0xffff000000500000
    n1, n2 = b'a300_pm4.fw\0', b'a300_pfp.fw\0'
    ro = n1 + n2 + pm4 + pfp
    p1 = ro_addr if not invalid_pointer else 0x12345678
    first = (p1, ro_addr + len(n1) + len(n2), len(pm4) + (1 if bad_size else 0))
    second = (ro_addr + len(n1), ro_addr + len(n1) + len(n2) + len(pm4), len(pfp))
    entries = [first] if missing else [first, second]
    if duplicate:
        entries.append(first)
    table = b''.join(struct.pack('<QQQ', *x) for x in entries)
    payload = bytearray(b'\0' * 64)
    name_off = len(payload); payload += names
    while len(payload) % 8: payload += b'\0'
    ro_off = len(payload); payload += ro
    while len(payload) % 8: payload += b'\0'
    table_off = len(payload); payload += table
    while len(payload) % 8: payload += b'\0'
    shoff = len(payload)
    sections = [
        (0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
        (names.index(b'.shstrtab'), 3, 0, 0, name_off, len(names), 0, 0, 1, 0),
        (names.index(b'.rodata'), 1, 2, ro_addr, ro_off, len(ro), 0, 0, 8, 0),
        (names.index(b'.builtin_fw'), 1, 2, table_addr, table_off, len(table), 0, 0, 8, 0),
    ]
    for section in sections:
        payload += struct.pack('<IIQQQQIIQQ', *section)
    ident = b'\x7fELF\x02\x01\x01' + b'\0' * 9
    header = struct.pack('<16sHHIQQQIHHHHHH', ident, typ, machine, 1, 0, 0,
                         shoff, 0, 64, 0, 0, 64, len(sections), 1)
    payload[:64] = header
    return bytes(payload)


def image_gz(pm4, pfp, duplicate=False):
    raw = bytearray(64)
    raw[56:60] = b'ARM\x64'
    raw += pm4 + pfp
    if duplicate:
        raw += pm4
    return gzip.compress(bytes(raw), mtime=0)


class BuiltinFirmware(unittest.TestCase):
    def setUp(self):
        self.pm4 = b'\x93' * 9220
        self.pfp = b'\xe4' * 1156
        self.old_pm4, self.old_pfp = f.PM4_SHA, f.PFP_SHA
        f.PM4_SHA, f.PFP_SHA = f.sha(self.pm4), f.sha(self.pfp)
        self.elf = fixture_elf(self.pm4, self.pfp)
        self.gz = image_gz(self.pm4, self.pfp)

    def tearDown(self):
        f.PM4_SHA, f.PFP_SHA = self.old_pm4, self.old_pfp

    def verify(self, elf=None, config=CFG, gz=None, pm4=None, pfp=None):
        return f.verify(self.elf if elf is None else elf, config,
                        self.gz if gz is None else gz,
                        self.pm4 if pm4 is None else pm4,
                        self.pfp if pfp is None else pfp)

    def test_valid_builtins(self):
        result = self.verify()
        self.assertEqual(result['builtin_table_entries'], 2)
        self.assertTrue(result['firmware']['a300_pm4.fw']['registered_once'])

    def test_missing_and_duplicate_table_entries_rejected(self):
        for kwargs in ({'missing': True}, {'duplicate': True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.verify(elf=fixture_elf(self.pm4, self.pfp, **kwargs))

    def test_bad_pointer_and_length_rejected(self):
        for kwargs in ({'invalid_pointer': True}, {'bad_size': True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.verify(elf=fixture_elf(self.pm4, self.pfp, **kwargs))

    def test_wrong_or_relocatable_elf_rejected(self):
        for kwargs in ({'typ': 3}, {'machine': 40}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.verify(elf=fixture_elf(self.pm4, self.pfp, **kwargs))
        with self.assertRaises(ValueError): self.verify(elf=b'bad ELF')

    def test_missing_or_duplicate_firmware_in_image_rejected(self):
        with self.assertRaises(ValueError): self.verify(gz=image_gz(self.pm4, b''))
        with self.assertRaises(ValueError): self.verify(gz=image_gz(self.pm4, self.pfp, duplicate=True))
        with self.assertRaises(ValueError): self.verify(gz=self.gz + b'extra')

    def test_input_hash_and_config_checked(self):
        with self.assertRaises(ValueError): self.verify(pm4=b'bad')
        with self.assertRaises(ValueError): self.verify(pfp=b'bad')
        with self.assertRaises(ValueError): self.verify(config=CFG.replace('a300_pfp.fw', 'wrong.fw'))
        with self.assertRaises(ValueError): self.verify(config=CFG + 'CONFIG_RELOCATABLE_KERNEL=y\n')


if __name__ == '__main__':
    unittest.main(verbosity=2)
