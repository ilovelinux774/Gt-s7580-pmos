#!/usr/bin/env python3
"""Stage the WCNSS (SMD) Bluetooth HCI driver into a pinned kernel tree.

The j4primelte kernel enables CONFIG_BT but no HCI transport at all, so the
Bluetooth core inside the WCNSS (WCN3620) is unreachable and no hci device is
ever created. This helper copies ci/j4primelte/kernel/hci_smd.c into a freshly
checked-out kernel and wires it up (Kconfig + Makefile) so that

    CONFIG_BT_HCI_SMD=y

can be enabled by build-kernel.sh. Every step is idempotent: re-running the
helper on an already patched tree changes nothing.
"""
import argparse
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
DRIVER = HERE / 'kernel' / 'hci_smd.c'
SYMBOL = 'BT_HCI_SMD'

KCONFIG_ENTRY = '''config BT_HCI_SMD
\tbool "Qualcomm WCNSS (SMD) HCI driver"
\tdepends on BT && MSM_SMD
\thelp
\t  Bluetooth HCI driver for the Bluetooth core inside the Qualcomm WCNSS
\t  (WCN3620, "pronto"). The controller is reached over the two SMD channels
\t  APPS_RIVA_BT_CMD and APPS_RIVA_BT_ACL on the APPS <-> WCNSS edge, which
\t  only exist once the WCNSS firmware has booted.

\t  Say Y here for the Samsung Galaxy J4+ (j4primelte): its kernel enables
\t  no other HCI transport at all, so without this the phone has no
\t  Bluetooth device.

'''

MAKEFILE_LINE = 'obj-$(CONFIG_%s)\t\t+= hci_smd.o\n' % SYMBOL

# Insert ahead of the QCOM-specific entries at the end of the menu.
KCONFIG_ANCHOR = 'config MSM_BT_POWER'


def candidate_files(root):
    """Files that must be part of the kernel build cache key."""
    return sorted((HERE / 'kernel').glob('*.c')) + sorted((HERE / 'kernel').glob('*.h'))


def stage_driver(tree):
    """Copy the driver into drivers/bluetooth/. Returns the destination path."""
    if not DRIVER.is_file():
        raise SystemExit('missing driver source: %s' % DRIVER)
    dest = Path(tree) / 'drivers' / 'bluetooth' / 'hci_smd.c'
    shutil.copyfile(DRIVER, dest)
    return dest


def wire_makefile(tree):
    """Append the build line to drivers/bluetooth/Makefile (idempotent)."""
    makefile = Path(tree) / 'drivers' / 'bluetooth' / 'Makefile'
    text = makefile.read_text()
    if 'hci_smd.o' in text:
        return makefile
    if not text.endswith('\n'):
        text += '\n'
    makefile.write_text(text + MAKEFILE_LINE)
    return makefile


def wire_kconfig(tree):
    """Insert the Kconfig entry ahead of the QCOM entries (idempotent)."""
    kconfig = Path(tree) / 'drivers' / 'bluetooth' / 'Kconfig'
    text = kconfig.read_text()
    if ('config %s' % SYMBOL) in text:
        return kconfig
    if KCONFIG_ANCHOR not in text:
        raise SystemExit('Kconfig anchor %r not found in %s' % (KCONFIG_ANCHOR, kconfig))
    kconfig.write_text(text.replace(KCONFIG_ANCHOR,
                                    KCONFIG_ENTRY + KCONFIG_ANCHOR, 1))
    return kconfig


def apply(tree):
    tree = Path(tree)
    if not (tree / 'drivers' / 'bluetooth').is_dir():
        raise SystemExit('not a kernel tree: %s' % tree)
    dest = stage_driver(tree)
    makefile = wire_makefile(tree)
    kconfig = wire_kconfig(tree)
    print('staged %s' % dest)
    print('wired %s and %s' % (makefile.name, kconfig.name))
    return dest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tree', help='path to the pinned kernel source tree')
    parser.add_argument('--check', action='store_true',
                        help='only report whether the tree is already wired')
    args = parser.parse_args()
    if args.check:
        kconfig = Path(args.tree) / 'drivers' / 'bluetooth' / 'Kconfig'
        makefile = Path(args.tree) / 'drivers' / 'bluetooth' / 'Makefile'
        ok = ('config %s' % SYMBOL) in kconfig.read_text() and \
             'hci_smd.o' in makefile.read_text()
        print('wired' if ok else 'not wired')
        return 0 if ok else 1
    apply(args.tree)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
