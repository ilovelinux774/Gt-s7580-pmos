#!/usr/bin/env python3
"""Customize only the mounted CI image, never a host or phone partition."""
from pathlib import Path
import os
import shutil
import sys

root = Path(sys.argv[1]).resolve()
overlay = Path(sys.argv[2]).resolve()
if root == Path('/') or not os.path.ismount(root):
    raise SystemExit('Expected a mounted image directory, not the host root')
info = root / 'usr/share/deviceinfo/deviceinfo'
if not info.exists() or 'samsung-j4primelte' not in info.read_text():
    raise SystemExit('Not the expected j4primelte image')
for path in sorted(overlay.rglob('*')):
    dest = root / path.relative_to(overlay)
    if path.is_dir():
        dest.mkdir(parents=True, exist_ok=True)
        continue
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, dest)
    dest.chmod(0o755 if path.read_bytes().startswith(b'#!') else 0o644)

# Crucially, this is AFTER pmbootstrap install; it locks root on every install.
# An empty hash is distinct from the locked account markers '!' and '*'.
shadow = root / 'etc/shadow'
lines = shadow.read_text().splitlines()
found = False
for i, line in enumerate(lines):
    fields = line.split(':')
    if fields[0] == 'root':
        fields[1] = ''
        fields[7] = ''  # no account expiration
        lines[i] = ':'.join(fields)
        found = True
assert found, 'root account missing'
shadow.write_text('\n'.join(lines) + '\n')
shadow.chmod(0o640)

# Never run an additional SSH daemon with broader default listen addresses.
levels = root / 'etc/runlevels'
for level in levels.iterdir():
    if not level.is_dir():
        continue
    for daemon in ('sshd', 'dropbear', 'telnetd'):
        link = level / daemon
        if link.exists() or link.is_symlink():
            link.unlink()
level = levels / 'boot'
level.mkdir(parents=True, exist_ok=True)
link = level / 'j4-usb-debug'
if link.exists() or link.is_symlink():
    link.unlink()
link.symlink_to('../../init.d/j4-usb-debug')
with (root / 'etc/rc.conf').open('a') as f:
    f.write('\n# J4+ bring-up: persist OpenRC boot output.\nrc_logger="YES"\n')
(root / 'var/log/j4').mkdir(parents=True, exist_ok=True)
assert shadow.read_text().splitlines()[0].startswith('root::') or any(
    line.startswith('root::') for line in shadow.read_text().splitlines())
print('Final image: root password empty; custom USB-bound debug service enabled')
