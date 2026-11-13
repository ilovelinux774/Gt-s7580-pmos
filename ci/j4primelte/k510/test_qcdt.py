#!/usr/bin/env python3
"""Small dependency-free tests for qcdt.py and its DTB matching metadata."""
import argparse
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import qcdt


def fixture_dtb() -> bytes:
    props = {
        "qcom,msm-id": struct.pack(">6I", 303, 0, 308, 0, 309, 0),
        "qcom,board-id": struct.pack(">2I", 8, 6),
        "qcom,pmic-id": struct.pack(">4I", 0x10019, 0, 0, 0),
    }
    strings = bytearray()
    offsets = {}
    for name in props:
        offsets[name] = len(strings)
        strings += name.encode() + b"\0"
    structure = bytearray(struct.pack(">I", qcdt.FDT_BEGIN_NODE) + b"\0\0\0\0")
    for name, value in props.items():
        structure += struct.pack(">III", qcdt.FDT_PROP, len(value), offsets[name])
        structure += value
        structure += bytes((-len(value)) % 4)
    structure += struct.pack(">II", qcdt.FDT_END_NODE, qcdt.FDT_END)
    reserve_map = bytes(16)
    struct_off = qcdt.FDT_HEADER_SIZE + len(reserve_map)
    strings_off = struct_off + len(structure)
    total = strings_off + len(strings)
    header = struct.pack(">10I", qcdt.FDT_MAGIC, total, struct_off, strings_off,
                         qcdt.FDT_HEADER_SIZE, 17, 16, 0, len(strings), len(structure))
    return header + reserve_map + structure + strings


class QcdtTests(unittest.TestCase):
    def test_root_properties_and_qcdt_v3_aliases(self):
        dtb = fixture_dtb()
        props = qcdt.parse_root_cells(dtb)
        self.assertEqual(props["qcom,msm-id"], [303, 0, 308, 0, 309, 0])
        image = qcdt.make_qcdt(dtb, 4096)
        info = qcdt.validate_qcdt(image, 4096)
        self.assertEqual(info["version"], 3)
        self.assertEqual(info["entries"], 3)
        self.assertEqual(info["total_size"], len(image))
        for index, chipset in enumerate((303, 308, 309)):
            fields = struct.unpack_from("<10I", image, 12 + index * 40)
            self.assertEqual(fields[:4], (chipset, 8, 6, 0))
            self.assertEqual(fields[4:8], (0x10019, 0, 0, 0))
            self.assertEqual(fields[8], info["dtb_offset"])
            self.assertEqual(fields[9], info["dtb_size"])

    def test_rejects_missing_match_properties(self):
        with self.assertRaisesRegex(ValueError, "missing root matching properties"):
            qcdt.parse_root_cells(bad_root_dtb())

    def test_rejects_corrupt_qcdt_payload(self):
        image = bytearray(qcdt.make_qcdt(fixture_dtb()))
        image[0] = 0
        with self.assertRaisesRegex(ValueError, "QCDT magic"):
            qcdt.validate_qcdt(bytes(image))


def bad_root_dtb() -> bytes:
    # A valid, empty-root FDT; required Qualcomm matching metadata is absent.
    strings = b""
    structure = struct.pack(">III", qcdt.FDT_BEGIN_NODE, 0, qcdt.FDT_END_NODE) + struct.pack(">I", qcdt.FDT_END)
    reserve = bytes(16)
    struct_off = 56
    strings_off = struct_off + len(structure)
    total = strings_off
    return (struct.pack(">10I", qcdt.FDT_MAGIC, total, struct_off, strings_off,
                        40, 17, 16, 0, 0, len(structure)) + reserve + structure + strings)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-out", type=Path)
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(QcdtTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.wasSuccessful() and args.fixture_out:
        args.fixture_out.write_bytes(qcdt.make_qcdt(fixture_dtb()))
    raise SystemExit(not result.wasSuccessful())
