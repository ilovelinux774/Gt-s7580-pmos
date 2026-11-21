#!/usr/bin/env python3
"""Tests for make_rootswitch_initramfs.py and the hook it installs.

    python3 ci/j4primelte/test_rootswitch.py

The hook is executed for real, under /bin/sh, in a throwaway directory tree with
``blkid``/``mount``/``umount`` stubs, so the safety behaviour that matters --
never touching a postmarketOS install, never switching to a root without an
init, never resizing anything -- is asserted rather than described.
"""

from __future__ import annotations

import gzip
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import make_rootswitch_initramfs as mk  # noqa: E402
from uncpio import read_newc  # noqa: E402

FAKE_PMOS_FILES = {
    "init": "#!/bin/sh\nexec /init_2nd.sh\n",
    "init_2nd.sh": textwrap.dedent(
        """\
        #!/bin/sh
        . /init_functions.sh
        . /init_functions_2nd.sh
        setup_udev
        run_hooks /hooks
        check_keys
        mount_subpartitions
        run_hooks /hooks-extra
        wait_root_partition
        delete_old_install_partition
        resize_root_partition
        unlock_root_partition
        resize_root_filesystem
        mount_root_partition
        resize_filesystem_after_mount /sysroot
        init="/sbin/init"
        setup_bootchart2
        run_hooks /hooks-cleanup
        exec switch_root /sysroot "$init"
        """
    ),
    "init_functions.sh": "#!/bin/sh\nrun_hooks() { for h in \"$1\"/*.sh; do sh \"$h\"; done; }\n",
    "usr/bin/busybox": "not really busybox\n",
}


def make_fake_pmos(dest: Path) -> Path:
    for name, content in FAKE_PMOS_FILES.items():
        path = dest / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(0o755)
    (dest / "hooks").mkdir(exist_ok=True)
    (dest / "hooks-cleanup").mkdir(exist_ok=True)
    # a symlinked top-level directory, as the real initramfs has
    os.symlink("usr/bin", dest / "bin")
    (dest / "etc").mkdir(exist_ok=True)
    return dest


class CpioTests(unittest.TestCase):
    def test_newc_entry_layout(self) -> None:
        entry = mk.newc_entry("./foo", 0o100644, b"hello", 1, 0)
        self.assertEqual(entry[:6], b"070701")
        fields = [entry[6 + i * 8 : 6 + (i + 1) * 8] for i in range(13)]
        self.assertEqual(int(fields[6], 16), 5)  # filesize
        self.assertEqual(int(fields[11], 16), 6)  # namesize includes the NUL
        self.assertEqual(entry[110:116], b"./foo\x00")

    def test_trailer_has_a_namesize(self) -> None:
        trailer = mk.newc_entry("TRAILER!!!", 0, b"", 0, 0)
        fields = [trailer[6 + i * 8 : 6 + (i + 1) * 8] for i in range(13)]
        self.assertEqual(int(fields[11], 16), len("TRAILER!!!") + 1)
        # the archive must end on a NUL that is part of the name field
        self.assertEqual(trailer[110 + len("TRAILER!!!"):110 + len("TRAILER!!!") + 1], b"\x00")

    def test_archive_roundtrips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            (root / "a").mkdir(parents=True)
            (root / "a/b").write_text("data")
            os.symlink("a/b", root / "link")
            blob = mk.build_cpio(root)
            entries = {name: (mode, body) for name, mode, _u, _g, body, _m in read_newc(blob)}
            self.assertIn("./a/b", entries)
            self.assertEqual(entries["./a/b"][1], b"data")
            self.assertEqual(entries["./link"][0] & 0o170000, 0o120000)
            self.assertEqual(entries["./link"][1], b"a/b")


class BuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.src = make_fake_pmos(self.tmp / "pmos")
        self.out = self.tmp / "out.gz"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, label: str = "ARTIXROOT") -> Path:
        argv = [
            "make_rootswitch_initramfs.py",
            str(self.src),
            str(self.out),
            "--root-label", label,
        ]
        old = sys.argv
        sys.argv = argv
        try:
            self.assertEqual(mk.main(), 0)
        finally:
            sys.argv = old
        return self.out

    def entries(self) -> dict[str, bytes]:
        blob = gzip.decompress(self.out.read_bytes())
        return {
            name: body
            for name, _mode, _u, _g, body, _m in read_newc(blob)
        }

    def test_output_is_plain_gzip_of_a_cpio(self) -> None:
        self.build()
        self.assertEqual(self.out.read_bytes()[:2], b"\x1f\x8b")
        self.assertTrue(self.entries())

    def test_hook_is_installed_with_the_label(self) -> None:
        self.build(label="MYROOT")
        entries = self.entries()
        hook = entries[f"./hooks-extra/{mk.HOOK_NAME}"].decode()
        self.assertIn('LABEL="MYROOT"', hook)
        self.assertNotIn("@ROOT_LABEL@", hook)

    def test_rejects_labels_that_cannot_go_on_ext4(self) -> None:
        with self.assertRaises(SystemExit):
            for label in ("", "has space", "semi;colon"):
                argv = ["x", str(self.src), str(self.out), "--root-label", label]
                old = sys.argv
                sys.argv = argv
                try:
                    mk.main()
                finally:
                    sys.argv = old

    def test_init_2nd_skips_the_pmos_root_chain_when_the_hook_ran(self) -> None:
        self.build()
        init_2nd = self.entries()["./init_2nd.sh"].decode()
        self.assertIn("if [ -z \"$j4_init\" ]; then", init_2nd)
        # every pmOS root operation must be inside that guard
        guard_start = init_2nd.index('if [ -z "$j4_init" ]; then')
        guard_end = init_2nd.index("\nfi\n", guard_start)
        for call in ("wait_root_partition", "mount_root_partition", "resize_root_partition"):
            self.assertIn(call, init_2nd[guard_start:guard_end])
        self.assertIn('if [ -n "$j4_init" ]; then', init_2nd)
        self.assertIn('init="$j4_init"', init_2nd)

    def test_init_2nd_still_works_without_the_hook(self) -> None:
        self.build()
        init_2nd = self.entries()["./init_2nd.sh"].decode()
        self.assertIn('init="/sbin/init"', init_2nd)

    def test_generated_scripts_are_valid_sh(self) -> None:
        self.build()
        for name in (f"./hooks-extra/{mk.HOOK_NAME}", f"./hooks-cleanup/{mk.CLEANUP_NAME}", "./init_2nd.sh"):
            with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as handle:
                handle.write(self.entries()[name].decode())
                path = handle.name
            try:
                result = subprocess.run(["sh", "-n", path], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, f"{name}: {result.stderr}")
            finally:
                os.unlink(path)

    def test_pmos_symlinked_directories_survive(self) -> None:
        self.build()
        entries = self.entries()
        self.assertEqual(entries["./bin"], b"usr/bin")


class HookBehaviourTests(unittest.TestCase):
    """Run the real hook with stubbed blkid/mount/umount in a temp tree."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.j4root = self.tmp / "j4root"
        self.sysroot = self.tmp / "sysroot"
        self.run_state = self.tmp / "run" / "j4-rootswitch"
        self.dev = self.tmp / "dev"
        for path in (self.j4root, self.sysroot, self.dev):
            path.mkdir(parents=True)
        self.log = self.tmp / "mounts.log"
        (self.bin / "blkid").write_text("#!/bin/sh\nprintf 'ARTIXROOT\\n'\n")
        (self.bin / "mount").write_text(f"#!/bin/sh\necho \"mount $*\" >> {self.log}\nexit 0\n")
        (self.bin / "umount").write_text(f"#!/bin/sh\necho \"umount $*\" >> {self.log}\n")
        for tool in ("blkid", "mount", "umount"):
            (self.bin / tool).chmod(0o755)
        (self.dev / "mmcblk0p47").write_text("")
        self.hook = self.render_hook()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def render_hook(self) -> Path:
        text = mk.HOOK.replace("@ROOT_LABEL@", "ARTIXROOT")
        text = (
            text.replace("/dev/kmsg", str(self.tmp / "kmsg"))
            .replace("/dev/console", str(self.tmp / "console"))
            .replace("/run/j4-rootswitch", str(self.run_state))
            .replace("/j4root", str(self.j4root))
            .replace("/sysroot", str(self.sysroot))
            .replace("/dev/mmcblk*p* /dev/sd*", str(self.dev / "mmcblk*p*"))
            .replace('[ -b "$candidate" ]', '[ -e "$candidate" ]')

        )
        # init candidates are absolute paths inside the target root
        for candidate in ("/usr/lib/systemd/systemd", "/usr/bin/openrc-init",
                          "/sbin/init", "/usr/bin/init"):
            text = text.replace(f'"{candidate}"', f'"{self.j4root}{candidate}"')
        path = self.tmp / "hook.sh"
        path.write_text(text)
        return path

    def run_hook(self) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}:{env.get('PATH', '/usr/bin:/bin')}"
        return subprocess.run(["sh", str(self.hook)], capture_output=True, text=True, env=env)

    def log_lines(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_missing_label_does_nothing(self) -> None:
        (self.bin / "blkid").write_text("#!/bin/sh\nexit 0\n")
        result = self.run_hook()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.log_lines(), [])
        self.assertFalse((self.run_state / "device").exists())

    def test_root_without_an_init_is_refused(self) -> None:
        result = self.run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"mount -t ext4 -o ro {self.dev}/mmcblk0p47 {self.j4root}", self.log_lines())
        self.assertIn(f"umount {self.j4root}", self.log_lines())
        self.assertFalse((self.run_state / "init").exists())
        self.assertFalse(any("move" in line for line in self.log_lines()))

    def test_postmarketos_target_is_refused(self) -> None:
        (self.j4root / "etc").mkdir()
        (self.j4root / "etc/os-release").write_text('ID=postmarketos\nNAME="postmarketOS"\n')
        (self.j4root / "sbin").mkdir()
        init = self.j4root / "sbin/init"
        init.write_text("#!/bin/sh\n")
        init.chmod(0o755)
        self.run_hook()
        self.assertIn(f"umount {self.j4root}", self.log_lines())
        self.assertFalse(any("move" in line for line in self.log_lines()))
        self.assertFalse((self.run_state / "init").exists())

    def test_openrc_target_switches_and_records_the_init(self) -> None:
        (self.j4root / "usr/bin").mkdir(parents=True)
        init = self.j4root / "usr/bin/openrc-init"
        init.write_text("#!/bin/sh\n")
        init.chmod(0o755)
        result = self.run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self.log_lines()
        self.assertIn(f"mount -o move {self.j4root} {self.sysroot}", lines)
        self.assertEqual((self.run_state / "init").read_text().strip(), "/usr/bin/openrc-init")
        self.assertIn("mmcblk0p47", (self.run_state / "device").read_text())

    def test_openrc_init_is_found_before_the_generic_init(self) -> None:
        (self.j4root / "usr/bin").mkdir(parents=True)
        (self.j4root / "sbin").mkdir()
        for name in ("usr/bin/openrc-init", "sbin/init"):
            path = self.j4root / name
            path.write_text("#!/bin/sh\n")
            path.chmod(0o755)
        self.run_hook()
        self.assertEqual((self.run_state / "init").read_text().strip(), "/usr/bin/openrc-init")

    def test_sbin_init_is_still_honoured_when_there_is_no_openrc_init(self) -> None:
        (self.j4root / "sbin").mkdir()
        init = self.j4root / "sbin/init"
        init.write_text("#!/bin/sh\n")
        init.chmod(0o755)
        self.run_hook()
        self.assertEqual((self.run_state / "init").read_text().strip(), "/sbin/init")

    def test_nothing_is_ever_resized_or_written_to_the_target(self) -> None:
        (self.j4root / "usr/bin").mkdir(parents=True)
        init = self.j4root / "usr/bin/openrc-init"
        init.write_text("#!/bin/sh\n")
        init.chmod(0o755)
        self.run_hook()
        for line in self.log_lines():
            self.assertNotIn("mkfs", line)
            self.assertNotIn("resize", line)
            self.assertNotIn("parted", line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
