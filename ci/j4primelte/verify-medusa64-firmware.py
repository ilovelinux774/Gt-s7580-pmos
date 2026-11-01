#!/usr/bin/env python3
"""Verify A300 blobs are actually registered as kernel built-in firmware.

This checks the linked ARM64 vmlinux ELF's .builtin_fw table and pointer targets,
not just strings in the kernel. The workflow separately objcopies this vmlinux
and compares it byte-for-byte with the gzip's raw Image. This is an offline
check, not evidence that the hardware loads the microcode or renders.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import zlib

PM4_SHA = 'f78c57e879e96a2c0ceeea31dbd30f7f1d4526f9c876e80481c654f08daaecd3'
PFP_SHA = 'b6e66400d46d52dc343d370b94362d564a765994bda47dc026961dd47a9ca7a9'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Elf64:
    """Minimal bounds-checked parser for a non-relocatable ARM64 vmlinux."""
    def __init__(self, data):
        self.data = data
        require(len(data) >= 64 and data[:7] == b'\x7fELF\x02\x01\x01',
                'not a little-endian ELF64 file')
        (typ, machine, version, _, _, shoff, _, ehsize, _, _, shentsize,
         shnum, shstrndx) = struct.unpack_from('<HHIQQQIHHHHHH', data, 16)
        require(typ == 2 and machine == 183 and version == 1 and ehsize == 64,
                'vmlinux must be a non-relocatable ARM64 executable')
        require(shentsize == 64 and 0 < shnum < 65535 and shstrndx < shnum and
                shoff <= len(data) and shnum * shentsize <= len(data) - shoff,
                'invalid ELF section header table')
        self.sections = []
        for i in range(shnum):
            name, kind, flags, addr, off, size, _, _, _, _ = struct.unpack_from(
                '<IIQQQQIIQQ', data, shoff + i * shentsize)
            if kind != 8:  # SHT_NOBITS consumes memory but no bytes in the file
                require(off <= len(data) and size <= len(data) - off,
                        'ELF section outside file')
            self.sections.append({'nameidx': name, 'kind': kind, 'flags': flags,
                                  'addr': addr, 'off': off, 'size': size})
        names_sec = self.sections[shstrndx]
        require(names_sec['kind'] == 3, 'invalid ELF section name string table')
        names = data[names_sec['off']:names_sec['off'] + names_sec['size']]
        for sec in self.sections:
            sec['name'] = self._cstring(names, sec['nameidx'], 256)

    @staticmethod
    def _cstring(data, off, maximum):
        require(off < len(data), 'invalid string offset')
        end = data.find(b'\0', off, min(len(data), off + maximum))
        require(end != -1, 'unterminated string')
        try:
            return data[off:end].decode('ascii')
        except UnicodeDecodeError as exc:
            raise ValueError('non-ASCII ELF string') from exc

    def read_address(self, addr, length):
        require(0 <= length <= 16 * 1024 * 1024, 'implausible firmware length')
        hits = [s for s in self.sections
                if s['kind'] == 1 and s['flags'] & 2 and s['size'] and
                s['addr'] <= addr and addr - s['addr'] <= s['size'] and
                length <= s['size'] - (addr - s['addr'])]
        require(len(hits) == 1, 'firmware pointer is not in one allocated section')
        sec = hits[0]
        off = sec['off'] + (addr - sec['addr'])
        return self.data[off:off + length]

    def read_name(self, addr):
        # Bound the name within its mapped ELF section, not across sections.
        hits = [s for s in self.sections if s['kind'] == 1 and s['flags'] & 2 and
                s['size'] and s['addr'] <= addr < s['addr'] + s['size']]
        require(len(hits) == 1, 'firmware name pointer is not in one allocated section')
        sec = hits[0]
        off = sec['off'] + (addr - sec['addr'])
        limit = min(sec['off'] + sec['size'], off + 256)
        return self._cstring(self.data[off:limit], 0, 256)

    def builtin_firmware(self):
        hits = [s for s in self.sections if s['name'] == '.builtin_fw']
        require(len(hits) == 1 and hits[0]['kind'] == 1 and hits[0]['flags'] & 2,
                'missing or invalid .builtin_fw section')
        sec = hits[0]
        require(0 < sec['size'] <= 256 * 1024 and sec['size'] % 24 == 0,
                'invalid 64-bit builtin firmware table size')
        entries = []
        for pos in range(sec['off'], sec['off'] + sec['size'], 24):
            name_addr, data_addr, size = struct.unpack_from('<QQQ', self.data, pos)
            name = self.read_name(name_addr)
            entries.append((name, self.read_address(data_addr, size)))
        return entries


def verify(vmlinux, config, image_gz, pm4, pfp):
    cfg = set(config.splitlines())
    for line in ('CONFIG_FW_LOADER=y', 'CONFIG_FIRMWARE_IN_KERNEL=y',
                 'CONFIG_EXTRA_FIRMWARE="a300_pm4.fw a300_pfp.fw"',
                 'CONFIG_EXTRA_FIRMWARE_DIR="firmware"'):
        require(line in cfg, f'required config missing: {line}')
    require('CONFIG_RELOCATABLE_KERNEL=y' not in cfg, 'unexpected relocatable kernel')
    expected = {'a300_pm4.fw': (pm4, PM4_SHA, 9220),
                'a300_pfp.fw': (pfp, PFP_SHA, 1156)}
    for name, (blob, digest, length) in expected.items():
        require(len(blob) == length and sha(blob) == digest,
                f'wrong pinned input firmware: {name}')
    dec = zlib.decompressobj(31)
    try:
        raw = dec.decompress(image_gz) + dec.flush()
    except zlib.error as exc:
        raise ValueError('invalid Image.gz') from exc
    require(dec.eof and not dec.unused_data and len(raw) >= 64 and raw[56:60] == b'ARM\x64',
            'invalid or concatenated ARM64 Image.gz')
    elf = Elf64(vmlinux)
    entries = elf.builtin_firmware()
    result = {}
    for name, (blob, digest, length) in expected.items():
        matches = [data for entry, data in entries if entry == name]
        require(len(matches) == 1 and matches[0] == blob,
                f'{name} missing, duplicated or incorrect in builtin firmware table')
        require(raw.count(blob) == 1, f'{name} not present once in the raw kernel Image')
        result[name] = {'bytes': length, 'sha256': digest, 'registered_once': True}
    return {'result': 'PASS: linked builtin firmware table and raw Image; hardware untested',
            'vmlinux_sha256': sha(vmlinux), 'raw_image_sha256': sha(raw),
            'builtin_table_entries': len(entries), 'firmware': result}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    for name in ('vmlinux', 'config', 'image-gz', 'pm4', 'pfp', 'report'):
        ap.add_argument('--' + name, type=Path, required=True)
    a = ap.parse_args()
    result = verify(a.vmlinux.read_bytes(), a.config.read_text(), a.image_gz.read_bytes(),
                    a.pm4.read_bytes(), a.pfp.read_bytes())
    a.report.parent.mkdir(parents=True, exist_ok=True)
    a.report.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
