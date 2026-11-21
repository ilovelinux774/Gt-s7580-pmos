#!/usr/bin/env python3
"""Tests for the root filesystem tooling.

    python3 ci/j4primelte/test_rootfs.py            # audit tests only
    sudo python3 ci/j4primelte/test_rootfs.py       # + a real ext4 build

The audit tests are the important ones. They assert the rule this project got
wrong once: a glibc tree is fine when its ABI floor is at or below the phone's
kernel, and only a floor *above* the kernel makes an image unbootable. Nothing
here keys off "musl" or "glibc" as a label.

The ext4 test builds a small image from a fake tree and checks the three
properties that make it usable on 3.18: the label the initramfs looks for, the
absence of ext4 features 3.18 cannot mount, and a populated tree (owner, modes,
symlinks, device nodes) that came out of ``mke2fs -d`` without a loop mount.
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
sys.path.insert(0, str(HERE))
import elfnotes  # noqa: E402

ELF_MACHINE = {"aarch64": 183, "arm32": 40}


def elf_with_floor(interp: str | None, floor: tuple[int, int, int] | None,
                   machine: str = "aarch64") -> bytes:
    """A minimal ELF64-LE with an optional PT_INTERP and .note.ABI-tag."""
    payload = b""
    phdrs: list[bytes] = []
    ehdr = 64
    phdr = 56
    count = 1 if interp else 0
    count += 1 if floor else 0
    offset = ehdr + phdr * count
    body = bytearray()

    def add_segment(p_type: int, data: bytes) -> None:
        nonlocal offset
        phdrs.append(struct.pack("<IIQQQQQQ", p_type, 4, offset, 0, 0, len(data), len(data), 4))
        body.extend(data)
        pad = (-len(data)) % 8
        body.extend(b"\x00" * pad)
        offset += len(data) + pad

    if interp:
        add_segment(3, interp.encode() + b"\x00")
    if floor:
        name = b"GNU\x00"
        desc = struct.pack("<IIII", 0, *floor)
        note = struct.pack("<III", len(name), len(desc), 1) + name + desc
        add_segment(4, note)

    data = bytearray(ehdr + phdr * len(phdrs) + len(body))
    data[0:4] = b"\x7fELF"
    data[4] = 2  # ELF64
    data[5] = 1  # little endian
    data[6] = 1
    struct.pack_into("<H", data, 18, ELF_MACHINE[machine])
    struct.pack_into("<Q", data, 32, ehdr)          # e_phoff
    struct.pack_into("<H", data, 54, phdr)          # e_phentsize
    struct.pack_into("<H", data, 56, len(phdrs))    # e_phnum
    for index, entry in enumerate(phdrs):
        data[ehdr + index * phdr : ehdr + (index + 1) * phdr] = entry
    data[ehdr + phdr * len(phdrs):] = body
    return bytes(data)


def write_tree(root: Path, layout: dict[str, bytes | None]) -> None:
    for rel, content in layout.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if content is None:
            continue
        path.write_bytes(content)
        path.chmod(0o755)


class ElfNoteTests(unittest.TestCase):
    """The reader itself, on real files from this machine plus synthetic ones."""

    def test_reads_the_abi_tag_of_a_real_binary(self) -> None:
        facts = elfnotes.read_elf("/bin/sh")
        self.assertTrue(facts.readable, facts.reason)
        self.assertEqual(facts.kind, "glibc")
        self.assertIsNotNone(facts.abi_floor)
        self.assertLessEqual(facts.abi_floor, (6, 1, 0))

    def test_a_static_binary_has_no_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "static"
            path.write_bytes(elf_with_floor(None, None))
            facts = elfnotes.read_elf(path)
            self.assertEqual(facts.kind, "static")
            self.assertIsNone(facts.abi_floor)

    def test_a_floor_is_read_back_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lib"
            path.write_bytes(elf_with_floor("/lib/ld-linux-aarch64.so.1", (4, 4, 0)))
            facts = elfnotes.read_elf(path)
            self.assertEqual(facts.kind, "glibc")
            self.assertEqual(facts.abi_floor, (4, 4, 0))
            self.assertEqual(facts.floor_text, "4.4.0")

    def test_musl_interpreter_is_classified_from_the_string(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lib"
            path.write_bytes(elf_with_floor("/lib/ld-musl-aarch64.so.1", None))
            self.assertEqual(elfnotes.read_elf(path).kind, "musl")

    def test_kernel_version_parsing(self) -> None:
        self.assertEqual(elfnotes.parse_version("3.18.120-j4-medusa64-debug+"), (3, 18, 120))
        self.assertEqual(elfnotes.parse_version("4.4"), (4, 4, 0))
        self.assertEqual(elfnotes.parse_version("3.7.0"), (3, 7, 0))

    def test_floor_above_the_kernel_is_detected(self) -> None:
        kernel = elfnotes.parse_version("3.18.120")
        self.assertTrue(elfnotes.floor_exceeds((4, 4, 0), kernel))
        self.assertFalse(elfnotes.floor_exceeds((3, 7, 0), kernel))
        self.assertFalse(elfnotes.floor_exceeds((3, 18, 120), kernel))
        self.assertFalse(elfnotes.floor_exceeds(None, kernel))


class AuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "root"
        self.root.mkdir()
        (self.root / "etc").mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_audit(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(AUDIT), str(self.root), *extra],
            capture_output=True, text=True,
        )

    def test_artix_glibc_floor_3_7_boots_on_3_18(self) -> None:
        """The whole point: ALARM/ARMtix glibc carries 3.7.0, so it is fine."""
        write_tree(self.root, {
            "usr/bin/openrc-init": elf_with_floor("/lib/ld-linux-aarch64.so.1", (3, 7, 0)),
            "usr/lib/ld-linux-aarch64.so.1": b"",  # present, not ELF
            "bin/busybox": elf_with_floor("/lib/ld-musl-aarch64.so.1", None),
        })
        os.symlink("usr/bin", self.root / "sbin")
        result = self.run_audit()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("boot path: /usr/bin/openrc-init", result.stdout)
        self.assertIn("glibc", result.stdout)

    def test_a_glibc_floor_above_the_kernel_fails(self) -> None:
        """Upstream Arch Linux's glibc is --enable-kernel=4.4. That tree dies."""
        write_tree(self.root, {
            "usr/bin/openrc-init": elf_with_floor("/lib/ld-linux-aarch64.so.1", (4, 4, 0)),
        })
        result = self.run_audit()
        self.assertEqual(result.returncode, 2)
        self.assertIn("refuse to start", result.stdout)
        self.assertIn("Fatal: kernel too old", result.stderr)

    def test_one_bad_helper_anywhere_fails_the_audit(self) -> None:
        """The init is fine; something it execs is not. That is still a dead boot."""
        write_tree(self.root, {
            "usr/bin/openrc-init": elf_with_floor("/lib/ld-linux-aarch64.so.1", (3, 7, 0)),
            "usr/lib/systemd/systemd": elf_with_floor("/usr/lib/ld-linux-aarch64.so.1", (4, 19, 0)),
        })
        result = self.run_audit()
        self.assertEqual(result.returncode, 2)
        self.assertIn("/usr/lib/systemd/systemd", result.stdout)

    def test_kernel_argument_changes_the_verdict(self) -> None:
        write_tree(self.root, {
            "usr/bin/openrc-init": elf_with_floor("/lib/ld-linux-aarch64.so.1", (4, 4, 0)),
        })
        self.assertEqual(self.run_audit().returncode, 2)
        self.assertEqual(self.run_audit("--kernel", "6.1.0").returncode, 0)

    def test_openrc_init_is_preferred_over_systemd(self) -> None:
        write_tree(self.root, {
            "usr/bin/openrc-init": elf_with_floor("/lib/ld-linux-aarch64.so.1", (3, 7, 0)),
            "usr/lib/systemd/systemd": elf_with_floor("/lib/ld-linux-aarch64.so.1", (3, 7, 0)),
        })
        result = self.run_audit()
        self.assertIn("boot path: /usr/bin/openrc-init", result.stdout)

    def test_static_boot_path_is_accepted(self) -> None:
        write_tree(self.root, {"sbin/init": b"#!/bin/sh\nexec /bin/sh\n"})
        result = self.run_audit()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_dangling_symlink_is_not_a_boot_path(self) -> None:
        (self.root / "sbin").mkdir(parents=True)
        os.symlink("/usr/bin/openrc-init", self.root / "sbin/init")
        result = self.run_audit()
        self.assertEqual(result.returncode, 1)
        self.assertIn("no candidate for PID 1", result.stderr)

    def test_unknown_floor_is_a_warning_not_a_failure(self) -> None:
        write_tree(self.root, {
            "usr/bin/openrc-init": elf_with_floor("/lib/ld-linux-aarch64.so.1", None),
        })
        result = self.run_audit()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no ABI note", result.stdout)

    def test_wrong_architecture_is_a_warning(self) -> None:
        write_tree(self.root, {
            "usr/bin/openrc-init": elf_with_floor("/lib/ld-linux-aarch64.so.1", (3, 7, 0),
                                                  machine="arm32"),
        })
        result = self.run_audit()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("are not aarch64", result.stdout)

    def test_json_report_has_the_decision_in_it(self) -> None:
        write_tree(self.root, {
            "usr/bin/openrc-init": elf_with_floor("/lib/ld-linux-aarch64.so.1", (3, 7, 0)),
        })
        out = self.tmp / "audit.json"
        self.run_audit("--json-out", str(out))
        import json

        report = json.loads(out.read_text())
        self.assertEqual(report["kernel"], "3.18.120")
        self.assertEqual(report["chosen_by_hook"]["hook_path"], "/usr/bin/openrc-init")
        self.assertEqual(report["chosen_by_hook"]["abi_floor"], "3.7.0")
        self.assertEqual(report["refuses_this_kernel"], [])


@unittest.skipUnless(os.geteuid() == 0, "needs root to create device nodes")
class Ext4ImageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.tree = self.tmp / "tree"
        write_tree(self.tree, {
            "etc/os-release": b'ID=artix\nNAME="Artix Linux"\n',
            "usr/bin/openrc-init": elf_with_floor("/lib/ld-linux-aarch64.so.1", (3, 7, 0)),
            "usr/lib/ld-linux-aarch64.so.1": b"",
            "etc/init.d/j4-rndis": b"#!/sbin/openrc-run\n",
        })
        (self.tree / "etc/init.d").mkdir(parents=True, exist_ok=True)
        os.chmod(self.tree / "etc/init.d/j4-rndis", 0o755)
        (self.tree / "usr/bin/bash").write_bytes(
            elf_with_floor("/usr/lib/ld-linux-aarch64.so.1", (3, 7, 0))
        )
        os.chmod(self.tree / "usr/bin/bash", 0o755)
        os.symlink("/usr/bin/openrc-init", self.tree / "etc/init.d/../sbin-init-link") \
            if (self.tree / "sbin").exists() else None
        subprocess.run(["tar", "-C", str(self.tree), "-cJf", str(self.tmp / "artix.tar.xz"), "."],
                       check=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, *extra: str, size: str = "48") -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PATH"] = "/sbin:/usr/sbin:" + env.get("PATH", "")
        return subprocess.run(
            ["bash", str(BUILD), str(self.tmp / "artix.tar.xz"),
             str(self.tmp / "rootfs.img"), size, *extra],
            capture_output=True, text=True, env=env,
        )

    def test_image_is_labelled_and_mountable_by_3_18(self) -> None:
        result = self.build()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        image = self.tmp / "rootfs.img"
        self.assertTrue(image.exists())
        label = subprocess.run(["dumpe2fs", "-h", str(image)],
                               capture_output=True, text=True).stdout
        self.assertIn("Filesystem volume name:   ARTIXROOT", label)
        features = [line for line in label.splitlines()
                    if line.startswith("Filesystem features:")][0]
        for bad in ("metadata_csum", "meta_bg", "orphan_file", "fast_commit", "quota"):
            self.assertNotIn(bad, features, features)

    def test_the_tree_is_populated_without_a_loop_mount(self) -> None:
        result = self.build()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        listing = subprocess.run(["debugfs", "-R", "ls -l /usr/bin", str(self.tmp / "rootfs.img")],
                                 capture_output=True, text=True).stdout
        self.assertIn("openrc-init", listing)
        self.assertIn("bash", listing)

    def test_an_unbootable_tree_fails_the_build(self) -> None:
        tree = self.tmp / "upstream"
        write_tree(tree, {
            "etc/os-release": b"ID=arch\n",
            "usr/bin/openrc-init": elf_with_floor("/lib/ld-linux-aarch64.so.1", (4, 4, 0)),
        })
        archive = self.tmp / "upstream.tar.xz"
        subprocess.run(["tar", "-C", str(tree), "-cJf", str(archive), "."], check=True)
        env = dict(os.environ)
        env["PATH"] = "/sbin:/usr/sbin:" + env.get("PATH", "")
        result = subprocess.run(
            ["bash", str(BUILD), str(archive), str(self.tmp / "bad.img"), "48"],
            capture_output=True, text=True, env=env,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot start on this kernel", result.stderr)

    def test_a_tree_without_openrc_init_is_refused_early(self) -> None:
        tree = self.tmp / "wronginit"
        write_tree(tree, {"etc/os-release": b"ID=artix\n"})
        archive = self.tmp / "wronginit.tar.xz"
        subprocess.run(["tar", "-C", str(tree), "-cJf", str(archive), "."], check=True)
        env = dict(os.environ)
        env["PATH"] = "/sbin:/usr/sbin:" + env.get("PATH", "")
        result = subprocess.run(
            ["bash", str(BUILD), str(archive), str(self.tmp / "x.img"), "48"],
            capture_output=True, text=True, env=env,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no openrc-init", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
