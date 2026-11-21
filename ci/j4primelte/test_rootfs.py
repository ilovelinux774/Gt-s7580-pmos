#!/usr/bin/env python3
"""Tests for the root filesystem tooling.

    python3 ci/j4primelte/test_rootfs.py            # audit tests only
    sudo python3 ci/j4primelte/test_rootfs.py       # + a real ext4 build

The ext4 test writes a 48 MiB loop-mounted image and checks the two properties
that make the image usable on this phone: the label the initramfs hook searches
for, and the absence of ext4 features a 3.18 kernel cannot mount.
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
AUDIT = HERE / "audit-rootfs.py"
BUILD = HERE / "build-rootfs.sh"


def elf_with_interp(interp: str) -> bytes:
    payload = interp.encode() + b"\x00"
    ehdr, phdr = 64, 56
    offset = ehdr + phdr
    data = bytearray(offset + len(payload))
    data[0:4] = b"\x7fELF"
    data[4] = 2  # ELF64
    data[5] = 1  # little endian
    data[6] = 1
    struct.pack_into("<Q", data, 32, ehdr)
    struct.pack_into("<H", data, 54, phdr)
    struct.pack_into("<H", data, 56, 1)
    struct.pack_into("<I", data, ehdr, 3)  # PT_INTERP
    struct.pack_into("<Q", data, ehdr + 8, offset)
    struct.pack_into("<Q", data, ehdr + 32, len(payload))
    data[offset : offset + len(payload)] = payload
    return bytes(data)


def write_tree(root: Path, layout: dict[str, bytes | None]) -> None:
    for rel, content in layout.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if content is None:
            continue
        path.write_bytes(content)
        path.chmod(0o755)


class AuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "root"
        self.root.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_audit(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(AUDIT), str(self.root), *extra],
            capture_output=True, text=True,
        )

    def test_glibc_boot_path_is_refused(self) -> None:
        write_tree(self.root, {"usr/lib/systemd/systemd": elf_with_interp("/usr/lib/ld-linux-aarch64.so.1")})
        result = self.run_audit()
        self.assertEqual(result.returncode, 2)
        self.assertIn("Fatal: kernel too old", result.stderr)
        self.assertIn("glibc", result.stdout)

    def test_glibc_boot_path_can_be_downgraded_to_a_warning(self) -> None:
        write_tree(self.root, {"usr/lib/systemd/systemd": elf_with_interp("/usr/lib/ld-linux-aarch64.so.1")})
        result = self.run_audit("--allow-glibc-boot")
        self.assertEqual(result.returncode, 0)
        self.assertIn("WARNING", result.stdout)

    def test_musl_boot_path_is_accepted(self) -> None:
        write_tree(self.root, {
            "alpine/usr/bin/openrc-init": elf_with_interp("/lib/ld-musl-aarch64.so.1"),
        })
        (self.root / "usr/bin").mkdir(parents=True, exist_ok=True)
        os.symlink("/alpine/usr/bin/openrc-init", self.root / "usr/bin/openrc-init")
        result = self.run_audit()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("boot path: /usr/bin/openrc-init (musl)", result.stdout)

    def test_static_boot_path_is_accepted(self) -> None:
        write_tree(self.root, {"sbin/init": b"#!/bin/sh\nexec /bin/sh\n"})
        result = self.run_audit()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_dangling_symlink_is_not_a_boot_path(self) -> None:
        (self.root / "sbin").mkdir(parents=True)
        os.symlink("/alpine/sbin/init", self.root / "sbin/init")
        result = self.run_audit()
        self.assertEqual(result.returncode, 1)
        self.assertIn("no init", result.stderr)

    def test_glibc_systemd_beats_a_musl_init_unless_it_is_moved(self) -> None:
        """This is the trap build-rootfs.sh has to undo.

        Arch ships systemd, the hook tries systemd before OpenRC, and systemd
        is glibc. A rootfs with a perfectly good musl /sbin/init next to an
        untouched Arch systemd still cannot boot, and the audit has to say so
        rather than accept the musl init it finds second.
        """
        write_tree(self.root, {
            "usr/lib/systemd/systemd": elf_with_interp("/usr/lib/ld-linux-aarch64.so.1"),
            "alpine/sbin/init": elf_with_interp("/lib/ld-musl-aarch64.so.1"),
        })
        (self.root / "sbin").mkdir(parents=True, exist_ok=True)
        os.symlink("/alpine/sbin/init", self.root / "sbin/init")
        result = self.run_audit()
        self.assertEqual(result.returncode, 2)
        self.assertIn("boot path: /usr/lib/systemd/systemd (glibc)", result.stdout)

    def test_moving_systemd_aside_makes_the_musl_init_the_boot_path(self) -> None:
        write_tree(self.root, {
            "usr/lib/systemd/systemd": elf_with_interp("/usr/lib/ld-linux-aarch64.so.1"),
            "alpine/sbin/init": elf_with_interp("/lib/ld-musl-aarch64.so.1"),
        })
        (self.root / "sbin").mkdir(parents=True, exist_ok=True)
        os.symlink("/alpine/sbin/init", self.root / "sbin/init")
        (self.root / "usr/lib/systemd/systemd").rename(self.root / "alpine/systemd.arch")
        result = self.run_audit()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("boot path: /sbin/init (musl)", result.stdout)


@unittest.skipUnless(os.geteuid() == 0, "needs root for a loop-mounted image")
class Ext4ImageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "tree"
        write_tree(self.root, {
            "usr/bin/bash": elf_with_interp("/usr/lib/ld-linux-aarch64.so.1"),
            "usr/lib/libc.so.6": b"",
            "usr/lib/systemd/systemd": elf_with_interp("/usr/lib/ld-linux-aarch64.so.1"),
        })
        self.arch = self.tmp / "arch.tar.gz"
        subprocess.run(["tar", "-C", str(self.root), "-czf", str(self.arch), "."], check=True)
        self.alpine_tree = self.tmp / "alpinetree"
        write_tree(self.alpine_tree, {
            "sbin/apk": elf_with_interp("/lib/ld-musl-aarch64.so.1"),
            "sbin/init": elf_with_interp("/lib/ld-musl-aarch64.so.1"),
            "usr/bin/openrc-init": elf_with_interp("/lib/ld-musl-aarch64.so.1"),
        })
        self.alpine = self.tmp / "alpine.tar.gz"
        subprocess.run(["tar", "-C", str(self.alpine_tree), "-czf", str(self.alpine), "."], check=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PATH"] = "/sbin:/usr/sbin:" + env.get("PATH", "")
        env["ALPINE_BOOTSTRAP"] = str(self.alpine)
        return subprocess.run(
            ["bash", str(BUILD), str(self.arch), str(self.tmp / "rootfs.img"), "48"],
            capture_output=True, text=True, env=env,
        )

    def test_image_is_labelled_and_mountable_by_3_18(self) -> None:
        result = self.build()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        image = self.tmp / "rootfs.img"
        self.assertTrue(image.exists())

        label = subprocess.run(
            ["dumpe2fs", "-h", str(image)], capture_output=True, text=True
        ).stdout
        self.assertIn("Filesystem volume name:   ARCHROOT", label)
        features = [line for line in label.splitlines() if line.startswith("Filesystem features:")][0]
        for bad in ("metadata_csum", "meta_bg", "orphan_file", "fast_commit"):
            self.assertNotIn(bad, features)

    def test_arch_systemd_is_moved_out_of_the_boot_path(self) -> None:
        result = self.build()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("moved Arch systemd aside", result.stdout)
        mnt = self.tmp / "mnt"
        mnt.mkdir()
        subprocess.run(["mount", "-o", "loop,ro", str(self.tmp / "rootfs.img"), str(mnt)], check=True)
        try:
            self.assertFalse((mnt / "usr/lib/systemd/systemd").exists())
            self.assertTrue((mnt / "alpine/systemd.arch").exists())
            self.assertEqual(os.readlink(mnt / "sbin/init"), "/alpine/sbin/init")
            self.assertTrue((mnt / "alpine/etc/init.d/j4-rndis").exists())
        finally:
            subprocess.run(["umount", str(mnt)], check=True)

    def test_a_glibc_only_boot_path_fails_the_build(self) -> None:
        env = dict(os.environ)
        env["PATH"] = "/sbin:/usr/sbin:" + env.get("PATH", "")
        env.pop("ALPINE_BOOTSTRAP", None)
        result = subprocess.run(
            ["bash", str(BUILD), str(self.arch), str(self.tmp / "nogood.img"), "48"],
            capture_output=True, text=True, env=env,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot start on this kernel", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
