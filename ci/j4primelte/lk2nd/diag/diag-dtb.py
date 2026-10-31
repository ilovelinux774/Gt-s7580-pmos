#!/usr/bin/env python3
"""Reserve the experimental J415F pmsg stage marker separately from ramoops.

Input is the previously validated M1 tree. The semantic FDT diff is checked
independently: only ramoops size/pmsg/no-map and a no-map marker node may
change. No claim about reset retention, recovery DTB or device boot is made.
"""
import argparse
from pathlib import Path
import struct
import subprocess
import tempfile

RAMOOPS = '/reserved-memory/ramoops@86000000'
MARKER = '/reserved-memory/j4-diag@860c0000'
CELL = lambda *n: struct.pack('>' + 'I' * len(n), *n)


def parse(blob):
    if len(blob) < 40:
        raise ValueError('short FDT header')
    magic, total, st, strings, reserve, version, last, boot, nstrings, nstruct = struct.unpack_from('>10I', blob)
    if magic != 0xd00dfeed or total != len(blob) or version < 17:
        raise ValueError('invalid FDT header')
    if st + nstruct > total or strings + nstrings > total:
        raise ValueError('invalid FDT section bounds')
    reservations = []
    pos = reserve
    while True:
        if pos + 16 > total:
            raise ValueError('unterminated memreserve')
        address, size = struct.unpack_from('>QQ', blob, pos)
        pos += 16
        if address == size == 0:
            break
        reservations.append((address, size))
    stack, nodes = [], {}
    pos, end = st, st + nstruct
    def cstring(offset, limit):
        if not 0 <= offset < limit <= len(blob):
            raise ValueError('invalid FDT string offset')
        stop = blob.find(b'\0', offset, limit)
        if stop < 0:
            raise ValueError('unterminated FDT string')
        return blob[offset:stop].decode('ascii'), stop + 1
    while pos < end:
        token = struct.unpack_from('>I', blob, pos)[0]
        pos += 4
        if token == 1:
            name, pos = cstring(pos, end)
            pos = (pos + 3) & ~3
            stack.append(name)
            path = '/' + '/'.join(stack[1:])
            if path in nodes:
                raise ValueError('duplicate node ' + path)
            nodes[path] = {}
        elif token == 2:
            if not stack:
                raise ValueError('unbalanced END_NODE')
            stack.pop()
        elif token == 3:
            if not stack or pos + 8 > end:
                raise ValueError('property outside node')
            length, nameoff = struct.unpack_from('>II', blob, pos)
            pos += 8
            if pos + length > end or nameoff >= nstrings:
                raise ValueError('property outside FDT section')
            name, _ = cstring(strings + nameoff, strings + nstrings)
            path = '/' + '/'.join(stack[1:])
            if name in nodes[path]:
                raise ValueError('duplicate property')
            nodes[path][name] = blob[pos:pos + length]
            pos = (pos + length + 3) & ~3
        elif token == 4:
            continue
        elif token == 9:
            if stack:
                raise ValueError('unfinished node')
            return nodes, reservations, boot
        else:
            raise ValueError(f'unknown FDT token {token}')
    raise ValueError('missing FDT_END')


def validate(base, result):
    old, oldreserve, oldboot = parse(base)
    got, newreserve, newboot = parse(result)
    if oldreserve != newreserve or oldboot != newboot:
        raise ValueError('FDT memreserve or boot CPU changed')
    if MARKER in old or RAMOOPS not in old:
        raise ValueError('unexpected base device tree')
    p = old[RAMOOPS]
    expected_base = {'compatible': b'ramoops\0', 'reg': CELL(0, 0x86000000, 0, 0x100000),
                     'record-size': CELL(0x40000), 'console-size': CELL(0x40000),
                     'ftrace-size': CELL(0x40000), 'pmsg-size': CELL(0x40000)}
    if any(p.get(k) != v for k, v in expected_base.items()):
        raise ValueError('base ramoops does not match validated M1')
    expected = {path: props.copy() for path, props in old.items()}
    expected[RAMOOPS]['reg'] = CELL(0, 0x86000000, 0, 0xc0000)
    expected[RAMOOPS]['pmsg-size'] = CELL(0)
    expected[RAMOOPS]['no-map'] = b''
    expected[MARKER] = {'reg': CELL(0, 0x860c0000, 0, 0x40000), 'no-map': b''}
    if got != expected:
        extra = {k for k in got.keys() ^ expected.keys()}
        changed = {k for k in got.keys() & expected.keys() if got[k] != expected[k]}
        raise ValueError(f'unexpected DTB changes: nodes={extra}, properties={changed}')
    # Check the stage slot against every other enabled reserved-memory range.
    for path, props in got.items():
        if not path.startswith('/reserved-memory/') or '/' in path[len('/reserved-memory/'):]:
            continue
        if path in (RAMOOPS, MARKER) or props.get('status') in (b'disabled\0', b'fail\0'):
            continue
        reg = props.get('reg')
        if reg is None:
            continue
        if len(reg) % 16:
            raise ValueError(f'unexpected reserved-memory reg shape in {path}')
        for at in range(0, len(reg), 16):
            address, size = struct.unpack_from('>QQ', reg, at)
            if size and address < 0x86100000 and 0x860c0000 < address + size:
                raise ValueError(f'marker overlaps {path}')
    # For mainline order dump -> console -> pmsg -> ftrace: pmsg=0, so the
    # dump, console, ftrace and reserved marker occupy successive 0x40000 slots.
    assert 0x86000000 + (0xc0000 - 0x40000 - 0x40000) == 0x86040000
    assert 0x86040000 + 0x40000 == 0x86080000
    assert 0x86080000 + 0x40000 == 0x860c0000
    assert 0x860c0000 + 0x40000 == 0x86100000


def create(base, out):
    with tempfile.TemporaryDirectory(prefix='j4-dtb-') as tmp:
        work = Path(tmp) / 'tree.dtb'
        work.write_bytes(base)
        commands = [
            ['-t', 'x', str(work), RAMOOPS, 'reg', '0', '86000000', '0', 'c0000'],
            ['-t', 'x', str(work), RAMOOPS, 'pmsg-size', '0'],
            [str(work), RAMOOPS, 'no-map'],
            ['-c', str(work), MARKER],
            ['-t', 'x', str(work), MARKER, 'reg', '0', '860c0000', '0', '40000'],
            [str(work), MARKER, 'no-map'],
        ]
        for command in commands:
            subprocess.run(['fdtput'] + command, check=True)
        result = work.read_bytes()
    validate(base, result)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(result)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    a = p.parse_args()
    create(a.base.read_bytes(), a.out)
    print('PASS semantic FDT diff: only diagnostic ramoops/no-map reservation changed')
    print('PASS modeled mainline slots: dump 86000000, console 86040000, ftrace 86080000, marker 860c0000')
    print('LIMIT: real recovery pmsg address and warm-reset retention not established')


if __name__ == '__main__':
    main()
