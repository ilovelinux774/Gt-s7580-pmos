#!/usr/bin/env python3
"""Stage the J4+ mainline files into a kernel source tree.

The phone's mainline support lives entirely in this repository: a device tree,
a kconfig fragment and (from M2 on) a generated panel driver. A kernel tree is
patched by copying those files in and adding the lines that make kbuild see
them. Nothing is committed upstream, so the pin in mainline-sources.env plus
these files fully describe a build.

Idempotent by design: the CI job may run this twice (a retry, or a cached tree),
and re-running must not duplicate a Makefile line - but a stanza or a line whose
*content* changed here has to be refreshed, or a tree staged by an older version
of this file keeps building the older rules forever.
"""
from pathlib import Path
import argparse
import re
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
SRC = HERE / 'mainline'
DTS = 'msm8917-samsung-j4primelte.dts'
DTB = DTS.replace('.dts', '.dtb')
# The M2 tree is the same source with the milestone symbol defined, in a wrapper
# file: dtc has no -D and the kernel preprocesses dts with cpp, so a build-time
# define cannot reach the #ifdef blocks. See the wrapper for the long version.
PANEL_DTS = 'msm8917-samsung-j4primelte-panel.dts'
PANEL_DTB = PANEL_DTS.replace('.dts', '.dtb')
PANEL_C = 'panel-samsung-s6d7at0b01.c'
FRAGMENT = 'j4mainline.fragment'
PANEL_SYMBOL = 'DRM_PANEL_SAMSUNG_S6D7AT0B01'

# The Kconfig entry for the generated panel driver. Two traps, both learned the
# hard way:
#   * "select X" together with "depends on X" for the same X is a kconfig
#     *recursive dependency*, and kconfig then refuses to read the whole tree -
#     which breaks every build, not just the panel one.
#   * a dependency on a symbol this tree does not define is not an error either:
#     kconfig invents a promptless always-n symbol with that name and the driver
#     silently can never be enabled. (MIPI_DSI was renamed DRM_MIPI_DSI upstream;
#     depending on the old name cost a full build cycle to notice.) The unit
#     test checks every name in here against the pinned tree.
PANEL_KCONFIG = f"""config {PANEL_SYMBOL}
\ttristate "Samsung S6D7AT0B01 video-mode DSI panel (Galaxy J4+)"
\tdepends on OF && DRM_MIPI_DSI
\tselect DRM_PANEL_SIMPLE
\thelp
\t  Say Y here to enable support for the 720x1480 video-mode panel used
\t  by the Samsung Galaxy J4+ / J6+ (S6D7AT0B01 with a PM6003XB1 bias).
\t  Init sequences are generated from the vendor device tree.
"""

# A Kconfig stanza: the `config` line plus every indented line under it.
PANEL_STANZA = re.compile(rf'config {PANEL_SYMBOL}\n(?:[ \t].*\n)*', re.M)


def insert_after_last(path: Path, anchor: str, line: str) -> bool:
    """Add `line` after the last line matching `anchor`, unless it is there."""
    text = path.read_text()
    if line.strip() in text:
        return False
    # The anchors are line anchors (^/mds), so MULTILINE is not optional: without
    # it ^ only matches the start of the whole file and the anchor is "not found".
    matches = list(re.finditer(anchor, text, re.M))
    if not matches:
        raise SystemExit(f'{path}: anchor {anchor!r} not found; refusing to guess '
                         'where the file should have been edited')
    end = matches[-1].end()
    newline = text.find('\n', end)
    newline = len(text) if newline < 0 else newline + 1
    path.write_text(text[:newline] + line + '\n' + text[newline:])
    return True


def stage_dts(root: Path):
    """Copy the device trees in and register both with kbuild."""
    dts_dir = root / 'arch' / 'arm64' / 'boot' / 'dts' / 'qcom'
    if not dts_dir.is_dir():
        raise SystemExit(f'{dts_dir}: not a Qualcomm arm64 kernel tree')
    for source in (DTS, PANEL_DTS):
        if not (SRC / source).is_file():
            raise SystemExit(f'{SRC / source}: staged device tree source is missing')
        shutil.copyfile(SRC / source, dts_dir / source)
    for name, dtb in ((DTS, DTB), (PANEL_DTS, PANEL_DTB)):
        insert_after_last(
            dts_dir / 'Makefile',
            r'^dtb-\$\(CONFIG_ARCH_QCOM\).*$',
            # A real tab, not a literal backslash-t: make reads the variable name
            # up to the first whitespace, so a wrong escape silently defines a
            # different variable and the dtb is never built.
            'dtb-$(CONFIG_ARCH_QCOM)\t+= ' + dtb,
        )
    return dts_dir / DTS


def stage_panel(root: Path):
    """Register the generated panel driver (M2)."""
    panel_dir = root / 'drivers' / 'gpu' / 'drm' / 'panel'
    if not panel_dir.is_dir():
        raise SystemExit(f'{panel_dir}: no DRM panel directory in this tree')
    if not (SRC / PANEL_C).is_file():
        raise SystemExit(f'{SRC / PANEL_C}: panel driver is missing')
    shutil.copyfile(SRC / PANEL_C, panel_dir / PANEL_C)
    insert_after_last(
        panel_dir / 'Makefile',
        r'^obj-\$\(CONFIG_DRM_PANEL_[A-Z0-9_]+\).*$',
        f'obj-$(CONFIG_{PANEL_SYMBOL}) += {PANEL_C.replace(".c", ".o")}',
    )
    kconfig = panel_dir / 'Kconfig'
    text = kconfig.read_text()
    wanted = PANEL_KCONFIG.rstrip('\n') + '\n'
    existing = PANEL_STANZA.search(text)
    if existing:
        if existing.group(0) != wanted:
            kconfig.write_text(text[:existing.start()] + wanted + text[existing.end():])
        return
    marker = '\nendmenu\n'
    if marker not in text:
        raise SystemExit(f'{kconfig}: no endmenu to insert the panel entry before')
    index = text.rindex(marker)
    kconfig.write_text(text[:index] + '\n' + wanted + text[index + 1:])


def stage_fragment(root: Path):
    """Keep the fragment inside the tree too, so `make xconfig` shows it."""
    dest = root / 'arch' / 'arm64' / 'configs' / FRAGMENT
    shutil.copyfile(SRC / FRAGMENT, dest)
    return dest


def staged_paths() -> list:
    """Every path the stager touches, for the build script's change guard."""
    return [
        'arch/arm64/boot/dts/qcom/Makefile',
        'arch/arm64/boot/dts/qcom/' + DTS,
        'arch/arm64/boot/dts/qcom/' + PANEL_DTS,
        'arch/arm64/configs/' + FRAGMENT,
        'drivers/gpu/drm/panel/Kconfig',
        'drivers/gpu/drm/panel/Makefile',
        'drivers/gpu/drm/panel/' + PANEL_C,
    ]


def restore(root: Path):
    """Put the tree back to exactly the pinned commit.

    The three tracked files are checked out; the four files the stager adds are
    untracked, so they have to be removed by hand. A tree left half-staged would
    make the next run's "unexpected diff" guard complain about our own files.
    """
    tracked = ['arch/arm64/boot/dts/qcom/Makefile',
               'drivers/gpu/drm/panel/Kconfig',
               'drivers/gpu/drm/panel/Makefile']
    subprocess.run(['git', '-C', str(root), 'checkout', '--', *tracked], check=False)
    for path in ('arch/arm64/boot/dts/qcom/' + DTS,
                 'arch/arm64/boot/dts/qcom/' + PANEL_DTS,
                 'arch/arm64/configs/' + FRAGMENT,
                 'drivers/gpu/drm/panel/' + PANEL_C):
        (root / path).unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('kernel', help='kernel source tree to patch')
    parser.add_argument('--panel', action='store_true',
                        help='also stage the M2 panel driver and its kconfig entry')
    parser.add_argument('--restore', action='store_true',
                        help='undo the staging instead of applying it')
    parser.add_argument('--list-staged', action='store_true',
                        help='print the paths this stager touches')
    args = parser.parse_args(argv)
    root = Path(args.kernel).resolve()
    if not (root / 'Kconfig').is_file():
        raise SystemExit(f'{root}: does not look like a kernel tree')
    if args.list_staged:
        for path in staged_paths():
            print(path)
        return 0
    if args.restore:
        restore(root)
        print(f'Restored {root} to the pinned tree')
        return 0
    stage_dts(root)
    stage_fragment(root)
    if args.panel:
        stage_panel(root)
        print(f'Staged J4+ mainline device tree and the panel driver into {root}')
    else:
        print(f'Staged J4+ mainline device tree into {root}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
