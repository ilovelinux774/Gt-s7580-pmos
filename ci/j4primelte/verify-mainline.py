#!/usr/bin/env python3
"""Independent verification of the mainline build artifacts.

This deliberately does not read the build log or trust the packing script's own
assertions: it re-parses the packed boot images, decompiles the built device
trees and re-checks the finished .config, so a bug in one half of the pipeline
cannot hide in the other. Exit code is 0 only when every check passed.

Usage:
    verify-mainline.py [MAINLINE_DIR] [DONOR_BOOT_IMG] [panel]

MAINLINE_DIR holds <variant>-Image.gz-dtb, <variant>.dtb, kernel-config-<variant>
and boot/boot-<variant>-<mode>.img; "panel" says the M2 variant was requested.
"""
from pathlib import Path
import importlib.util
import re
import shutil
import subprocess
import sys
import zlib

HERE = Path(__file__).resolve().parent


def load(name: str, path: str):
    """Import one of the dashed sibling modules, the only way that works here."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


repack = load('j4_repack', str(HERE / 'mainline-repack-boot.py'))
config = load('j4_config', str(HERE / 'mainline-config.py'))


def gzip_is_truncated(blob: bytes) -> bool:
    """True if the gzip stream ends before its own checksum.

    A CI runner that runs out of disk mid-copy leaves exactly this: a file of the
    right size whose tail is missing. The phone would hang inside the loader with
    nothing on the console, so it is worth an explicit check.
    """
    try:
        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
        decompressor.decompress(blob)
        # A complete stream leaves the trailer consumed; if a footer is still
        # expected, decompress() returns without error but eof is False.
        return not decompressor.eof
    except zlib.error:
        return True


def dtc_text(dtb: Path):
    """The device tree as source, or None when no dtc is installed."""
    dtc = shutil.which('dtc')
    if not dtc:
        return None
    proc = subprocess.run([dtc, '-I', 'dtb', '-O', 'dts', str(dtb)],
                          capture_output=True, text=True)
    if proc.returncode:
        raise SystemExit(f'dtc failed on {dtb}: {proc.stderr.strip()[:400]}')
    return proc.stdout


def property_ints(text: str, name: str):
    match = re.search(rf'{name}\s*=\s*<([^>]*)>', text)
    if not match:
        return None
    return [int(token, 0) for token in match.group(1).replace('\n', ' ').split()]


def node_body(text: str, opener: str):
    """The inside of the first node whose name matches `opener`, braces counted.

    A regex on indentation would tie this checker to how dtc happens to format its
    output; counting braces keeps it working on a decompiled tree, a hand-written
    one and a test fixture alike.
    """
    match = re.search(opener + r'[^{]*\{', text)
    if not match:
        return None
    depth, index = 1, match.end()
    while index < len(text) and depth:
        if text[index] == '{':
            depth += 1
        elif text[index] == '}':
            depth -= 1
        index += 1
    if depth:
        return None
    return text[match.end():index - 1]


def node_status(text: str, node_pattern: str):
    """status of the first node whose name matches, defaulting to "okay"."""
    body = node_body(text, node_pattern)
    if body is None:
        return None
    status = re.search(r'status\s*=\s*"([a-z]+)"', body)
    return status.group(1) if status else 'okay'


def status_of_compatible(text: str, compatible: str):
    """status of the node that carries this compatible, found from the inside out.

    Matching the node by name is how this check failed to check anything for a
    while: upstream calls the display controller `display-subsystem@1a00000` and
    only keeps `mdss:` as a label, which dtc throws away. A regex on `mdss@` then
    returned None, and None satisfied "must not be okay" for M1 while the panel
    build was accused of not being okay. The compatible is what a driver binds
    to, so it is what identifies the node.
    """
    at = text.find(compatible)
    if at < 0:
        return None
    depth, index = 0, at
    while index:
        if text[index] == '}':
            depth += 1
        elif text[index] == '{':
            depth -= 1
            if depth < 0:
                break
        index -= 1
    start = text.rfind('\n', 0, index) + 1
    header = text[start:index + 1]
    body = node_body(text[start:], r'[^{]*')
    if body is None:
        return None
    status = re.search(r'status\s*=\s*"([a-z]+)"', body)
    return status.group(1) if status else 'okay'


def check_console(text: str):
    """Return a problem string unless /chosen points at an enabled uart.

    This is the check that turned a "looks fine in source" device tree into a
    phone that prints nothing: msm8917.dtsi labels its BLSP1 uarts and leaves them
    status = "disabled", and an unpatched tree happily keeps
    stdout-path = "framebuffer0" from the template. Either mistake yields a boot
    with no console at all.
    """
    body = node_body(text, r'/?chosen')
    if body is None:
        return 'no /chosen node at all'
    path = re.search(r'stdout-path\s*=\s*"([^"]*)"', body)
    if not path:
        return '/chosen has no stdout-path, so the console lands wherever klog_ctl ' \
               'decides and nothing is readable over serial'
    target = path.group(1)
    alias = target.split(':')[0]
    if not alias.startswith('serial'):
        return f'stdout-path is "{target}", not a serial port: the boot log would ' \
               'only exist on a display driver that M1 does not run'
    body = node_body(text, r'/?aliases')
    if body is None:
        return f'/chosen says "{target}" but the tree has no /aliases node to ' \
               'resolve it'
    link = re.search(rf'^\s*{re.escape(alias)}\s*=\s*([^;]+);', body, re.M)
    if not link:
        return f'stdout-path refers to the alias "{alias}" which /aliases does not ' \
               'define'
    # A source tree writes `serial0 = &blsp1_uart1;` and the flattened one that
    # dtc prints writes `serial0 = "/soc@0/serial@78af000";`. Both have to be
    # followed, otherwise the check passes on the file a human reads and fails on
    # the artifact that is actually flashed - or the other way round.
    value = link.group(1).strip()
    ref = re.match(r'&(\w+)', value)
    if ref:
        opener, what = rf'{ref.group(1)}:', f'node labelled {ref.group(1)}'
    else:
        quoted = re.match(r'"([^"]+)"', value)
        if not quoted:
            return f'/aliases {alias} = {value}, which is neither a label nor a path'
        name = quoted.group(1).rstrip('/').split('/')[-1]
        opener, what = rf'(?:\w+:\s*)?{re.escape(name)}', f'node {name}'
    node = node_body(text, opener)
    if node is None:
        return f'no {what} for the console alias {alias}'
    status = re.search(r'status\s*=\s*"([a-z]+)"', node)
    if status and status.group(1) != 'okay':
        return f'console {what} is status = "{status.group(1)}"'
    return None


def check_dtb(mainline: Path, name: str, panel: bool, failures: list):
    dtb = mainline / f'{name}.dtb'
    if not dtb.is_file():
        failures.append(f'{name}: no built device tree ({dtb.name})')
        return
    text = dtc_text(dtb)
    if text is None:
        print(f'note: dtc is not installed, {dtb.name} only checked for size '
              f'({dtb.stat().st_size} bytes)')
        if dtb.stat().st_size < 4096:
            failures.append(f'{name}: {dtb.name} is too small to be a board tree')
        return
    for expected in ('Samsung Galaxy J4+ (SM-J415F)', 'samsung,j4primelte'):
        if expected not in text:
            failures.append(f'{name}: device tree does not say "{expected}"')
    board = property_ints(text, 'qcom,board-id')
    if board != [8, 6]:
        failures.append(f'{name}: qcom,board-id is {board}, LK picks the tree with '
                        '<8 6> for this board')
    if property_ints(text, 'qcom,msm-id') is None:
        failures.append(f'{name}: no qcom,msm-id; the loader matches on it')
    if '"ramoops"' not in text:
        failures.append(f'{name}: no ramoops reserved region, a panic before the '
                        'shell would leave nothing behind')
    problem = check_console(text)
    if problem:
        failures.append(f'{name}: {problem}')

    # The milestone gates. An M1 tree that already lights up MDSS or claims the
    # panel's power pins is a bug even when it boots: it fights the bootloader for
    # the display controller, and the phone hangs with a black screen and no log.
    present = {
        'panel driver node': 'samsung,s6d7at0b01-pm6003xb1' in text,
        'lm3632 bias': 'ti,lm3632' in text,
        'panel extra power rail': 'lcd_extra_power' in text,
        'touchscreen': 'samsung,s6sy761' in text,
    }
    mdss = status_of_compatible(text, '"qcom,mdss"')
    if mdss is None:
        failures.append(f'{name}: no qcom,mdss node in the built tree, so the '
                        'display gate cannot be checked at all')
    for what, got in present.items():
        if got != panel:
            failures.append(f'{name}: {what} in the device tree (panel build: '
                            f'{panel}) - the milestone gate is not holding')
    if panel and mdss != 'okay':
        failures.append(f'{name}: mdss is "{mdss}" in a panel build, so DRM can '
                        'never take the display over')
    if not panel and mdss == 'okay':
        failures.append(f'{name}: mdss is okay in an M1 build; simple-framebuffer '
                        'and DRM would fight over the same hardware')
    framebuffer = 'simple-framebuffer' in text
    if framebuffer == panel:
        failures.append(f'{name}: simple-framebuffer present={framebuffer} but this '
                        f'is the {"panel" if panel else "M1"} build, where it must '
                        f'be {"absent" if panel else "the console"}')


def check_appended(mainline: Path, name: str, failures: list):
    combined = mainline / f'{name}-Image.gz-dtb'
    dtb = mainline / f'{name}.dtb'
    if not combined.is_file() or not dtb.is_file():
        failures.append(f'{name}: missing {combined.name} or {dtb.name}')
        return
    blob = combined.read_bytes()
    tree = dtb.read_bytes()
    if len(blob) <= len(tree) or blob[-len(tree):] != tree:
        failures.append(f'{name}: trailing bytes are not the built DTB')
        return
    kernel = blob[:len(blob) - len(tree)]
    if not kernel.startswith(b'\x1f\x8b\x08'):
        failures.append(f'{name}: the kernel part of the payload is not a gzip '
                        'Image.gz stream; LK would hand an uncompressed Image to a '
                        'decompressing bootloader and die in the first instruction')
    elif gzip_is_truncated(kernel):
        # A truncated gzip stream is the signature of an out-of-disk CI run: the
        # phone resets during decompression and leaves nothing on the console.
        failures.append(f'{name}: the gzip kernel stream is truncated')


def check_boot(boot_dir: Path, base: Path, failures: list):
    if not base or not base.is_file():
        failures.append('no donor boot.img given to compare payloads against')
        return
    donor = repack.BootImage(base.read_bytes())
    images = sorted(boot_dir.glob('boot-*.img'))
    if not images:
        failures.append('no boot images were produced at all')
        return
    for image in images:
        data = image.read_bytes()
        try:
            img = repack.BootImage(data)
        except ValueError as exc:
            failures.append(f'{image.name}: {exc}')
            continue
        if img.ramdisk != donor.ramdisk:
            failures.append(f'{image.name}: ramdisk is not the donor ramdisk byte '
                            'for byte')
        if img.kernel_addr != donor.kernel_addr or img.tags_addr != donor.tags_addr:
            failures.append(f'{image.name}: load addresses changed, LK may not like '
                            'that')
        if len(data) > repack.MAX_IMAGE:
            failures.append(f'{image.name}: {len(data)} B does not fit a 32 MiB BOOT '
                            'partition')
        if img.kernel_size <= 0 or img.kernel_size > len(data):
            failures.append(f'{image.name}: kernel payload size {img.kernel_size} '
                            'is bogus')
        if b'pmos_root_uuid=' not in img.cmdline.encode():
            failures.append(f'{image.name}: cmdline lost pmos_root_uuid, '
                            'the phone would sit in the initramfs forever')
        debug_expected = 'debug' in image.name
        debug_actual = 'pmos.debug-shell' in img.cmdline
        if debug_expected != debug_actual:
            failures.append(f'{image.name}: pmos.debug-shell={debug_actual}, '
                            f'expected {debug_expected}')
        for flag in ('clk_ignore_unused', 'pd_ignore_unused'):
            if flag not in img.cmdline:
                failures.append(f'{image.name}: {flag} missing, the splash dies')


def check_configs(mainline: Path, panel_expected: bool, failures: list):
    for name, panel in (('m1', False), ('panel', True)):
        cfg = mainline / f'kernel-config-{name}'
        if not cfg.is_file():
            if name == 'm1':
                failures.append('no kernel-config-m1 to audit')
            continue
        if panel and not panel_expected:
            continue
        try:
            report = config.check_config(cfg, panel=panel)
        except SystemExit:
            failures.append(f'{cfg.name}: boot-critical options not built in '
                            '(see the config check output above)')
            continue
        if report.get('enabled', 0) < 1000:
            failures.append(f'{cfg.name}: only {report.get("enabled")} built-in '
                            'options, which is not a defconfig-based kernel')
    if panel_expected and not (mainline / 'kernel-config-panel').is_file():
        failures.append('panel variant was requested but never produced a config')


def main(argv):
    mainline = Path(argv[1]) if len(argv) > 1 else Path('artifacts/mainline')
    boot_dir = mainline / 'boot'
    donor = Path(argv[2]) if len(argv) > 2 and argv[2] else None
    panel_expected = len(argv) > 3 and argv[3] == 'panel'
    failures: list = []
    check_appended(mainline, 'm1', failures)
    check_dtb(mainline, 'm1', panel=False, failures=failures)
    if panel_expected:
        check_appended(mainline, 'panel', failures)
        check_dtb(mainline, 'panel', panel=True, failures=failures)
    check_boot(boot_dir, donor, failures)
    check_configs(mainline, panel_expected, failures)
    for line in failures:
        print('FAIL:', line)
    if failures:
        return 1
    images = sorted(p.name for p in boot_dir.glob('boot-*.img'))
    print('mainline artifacts verified: '
          + ', '.join(images if images else ['kernel payloads only (no donor boot.img)']))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
