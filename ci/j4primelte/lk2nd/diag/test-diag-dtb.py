#!/usr/bin/env python3
"""Mutation tests for the experimental pmsg reservation FDT checker."""
import argparse
import importlib.util
from pathlib import Path
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('dtb', HERE / 'diag-dtb.py')
dtb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dtb)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base', type=Path, required=True)
    args = p.parse_args()
    base = args.base.read_bytes()
    with tempfile.TemporaryDirectory(prefix='j4-fdt-test-') as d:
        tree = Path(d) / 'test.dtb'
        good = dtb.create(base, tree)
        # Each independent corruption must be rejected, including loss of the
        # marker's no-map and unrelated changes to loader-critical properties.
        mutations = [
            ['-d', str(tree), dtb.MARKER, 'no-map'],
            ['-t', 'x', str(tree), dtb.MARKER, 'reg', '0', '860c1000', '0', '40000'],
            ['-t', 'x', str(tree), dtb.RAMOOPS, 'pmsg-size', '40000'],
            ['-t', 'x', str(tree), dtb.RAMOOPS, 'reg', '0', '86000000', '0', '100000'],
            ['-t', 'x', str(tree), '/', 'qcom,board-id', '8', '7'],
            ['-r', str(tree), dtb.MARKER],
            ['-d', str(tree), dtb.RAMOOPS, 'no-map'],
        ]
        for command in mutations:
            tree.write_bytes(good)
            subprocess.run(['fdtput'] + command, check=True)
            try:
                dtb.validate(base, tree.read_bytes())
            except ValueError:
                pass
            else:
                raise AssertionError(f'accepted corrupted tree: {command}')
    print(f'PASS: good diagnostic DTB accepted; {len(mutations)} independent corruptions rejected')


if __name__ == '__main__':
    main()
