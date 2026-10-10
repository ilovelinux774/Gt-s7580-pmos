#!/usr/bin/env python3
"""Small, fail-closed adaptations for this pinned pmbootstrap revision."""
from pathlib import Path
import sys

root = Path(sys.argv[1])
p = root / 'pmb/chroot/run.py'
text = p.read_text()
old = '        "CHARSET": "UTF-8",'
assert text.count(old) == 1, 'pmbootstrap chroot environment changed'
p.write_text(text.replace(old, '        "GOGC": "off",\n' + old))

p = root / 'pmb/install/format.py'
text = p.read_text()
old = 'category_opts = ["-O", "^metadata_csum"]'
assert text.count(old) == 1, 'pmbootstrap ext4 formatter changed'
new = 'category_opts = ["-O", "^metadata_csum,^metadata_csum_seed,^orphan_file,^64bit,^meta_bg"]'
p.write_text(text.replace(old, new))
print('Applied QEMU/Go GC workaround and conservative downstream ext4 features')
