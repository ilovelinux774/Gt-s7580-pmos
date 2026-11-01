#!/usr/bin/env python3
"""Stage narrowly scoped pmOS/J415F Rev06 changes in a pinned Medusa 64 tree.

This edits only the throwaway kernel source checkout, never a phone or /vendor.
The touchscreen driver's sec,bringup=1 path returns before its automatic probe
firmware update. It does not guarantee that every other driver has no persistent
hardware effects, nor that touch will work on an untested kernel.
"""
from pathlib import Path
import argparse
import hashlib
import importlib.util

HERE = Path(__file__).resolve().parent
DTS_REL = 'arch/arm/boot/dts/samsung/msm8917/msm8917-sec-j4primelte-sea-open-r06.dts'
TOUCH_REL = 'firmware/tsp_sec/s6d7at01a01_j4prime.fw.ihex'
TOUCH_DRIVER_REL = 'drivers/input/touchscreen/sec_incell_ts/sec_ts_fw.c'
DTS_ORIGINAL_SHA = '96a637509d9b29b730052d645cf205d133bd1c38bcf1dd14f90c66243afe5ce0'
DRIVER_SHA = '40a0eac585ad214d82ea3022f4b26722743ce7dffbb5a207fefcac4667778f1b'
TOUCH_64_SHA = '447237c44a591e57d85c19bfb1c6229d500439ef2198f65a93ae7316a9a32315'
TOUCH_Q_SHA = '749efaba664d760b2eb1eb127146534cd8575af22d52f513b202829cc9686dda'
OLD_BRINGUP = 'sec,bringup = <0x0>;'
NEW_BRINGUP = 'sec,bringup = <0x1>;'
FW_NAME = 'sec,firmware_name = "tsp_sec/s6d7at01a01_j4prime.fw";'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def prepare(tree: Path, donor_firmware: Path):
    dts = tree / DTS_REL
    driver = tree / TOUCH_DRIVER_REL
    firmware = tree / TOUCH_REL
    source = dts.read_bytes()
    driver_data = driver.read_bytes()
    q_firmware = donor_firmware.read_bytes()
    if sha(driver_data) != DRIVER_SHA:
        raise ValueError('touchscreen update driver is not the audited Medusa 64 version')
    if (b'int sec_ts_firmware_update_on_probe(' not in driver_data or
            b'if (ts->plat_data->bringup == 1)' not in driver_data):
        raise ValueError('touchscreen update-disable path not found')
    if sha(q_firmware) != TOUCH_Q_SHA:
        raise ValueError('Q touchscreen firmware is not the pinned known-working-source file')
    current_fw_hash = sha(firmware.read_bytes())
    if current_fw_hash not in (TOUCH_64_SHA, TOUCH_Q_SHA):
        raise ValueError('unexpected touchscreen firmware in Medusa 64 tree')
    text = source.decode('utf-8')
    if text.count(FW_NAME) != 1:
        raise ValueError('expected exactly one J4+ touchscreen firmware property')
    if sha(source) == DTS_ORIGINAL_SHA:
        if text.count(OLD_BRINGUP) != 1 or NEW_BRINGUP in text:
            raise ValueError('unexpected original touchscreen bringup setting')
        new_text = text.replace(OLD_BRINGUP, NEW_BRINGUP)
    else:
        if text.count(NEW_BRINGUP) != 1 or OLD_BRINGUP in text:
            raise ValueError('DTS is neither the original nor the exact staged version')
        original_text = text.replace(NEW_BRINGUP, OLD_BRINGUP)
        if sha(original_text.encode('utf-8')) != DTS_ORIGINAL_SHA:
            raise ValueError('DTS has unexpected additional modifications')
        new_text = text
    # Preflight the existing WCNSS SMD adaptation before changing any files.
    # It is compiled into the new kernel, not loaded from the ARM32 rootfs.
    spec = importlib.util.spec_from_file_location('j4_bt_staging', HERE / 'kernel-bt.py')
    bt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bt)
    bt_dir = tree / 'drivers/bluetooth'
    bt_kconfig = (bt_dir / 'Kconfig').read_text()
    bt_makefile = (bt_dir / 'Makefile').read_text()
    if not bt.DRIVER.is_file() or (f'config {bt.SYMBOL}' not in bt_kconfig and
                                    bt.KCONFIG_ANCHOR not in bt_kconfig):
        raise ValueError('Bluetooth staging prerequisites are missing')
    bt_dest = bt_dir / 'hci_smd.c'
    bt_ready = (bt_dest.is_file() and bt_dest.read_bytes() == bt.DRIVER.read_bytes() and
                f'config {bt.SYMBOL}' in bt_kconfig and 'hci_smd.o' in bt_makefile)
    # No writes occur until these input checks have passed.
    if firmware.read_bytes() != q_firmware:
        firmware.write_bytes(q_firmware)
    if text != new_text:
        dts.write_text(new_text, encoding='utf-8')
    if bt_ready:
        print('WCNSS Bluetooth driver already staged')
    else:
        bt.apply(tree)
    return {
        'rev06_dts_sha256': sha(new_text.encode('utf-8')),
        'touch_q_ihex_sha256': sha(q_firmware),
        'touch_driver_sha256': sha(driver_data),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--kernel-tree', type=Path, required=True)
    ap.add_argument('--touch-q-ihex', type=Path, required=True)
    args = ap.parse_args()
    result = prepare(args.kernel_tree, args.touch_q_ihex)
    for key, value in result.items():
        print(f'{key}={value}')
    print('Rev06 sec,bringup=1 prevents the audited automatic touchscreen probe update.')
    print('Staged built-in WCNSS Bluetooth driver; behavior on the phone is unverified.')


if __name__ == '__main__':
    main()
