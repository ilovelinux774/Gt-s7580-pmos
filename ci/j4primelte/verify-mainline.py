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
import hashlib
import re
import shutil
import subprocess
import struct
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
loader_checks = load('j4_loader_checks', str(HERE / 'mainline-loader-checks.py'))


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


def body_of_compatible(text: str, compatible: str):
    """Find the flattened node carrying a compatible string; labels are gone."""
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
    if index == 0:
        return None
    start = text.rfind('\n', 0, index) + 1
    return node_body(text[start:], r'[^{]*')


def status_of_compatible(text: str, compatible: str):
    body = body_of_compatible(text, compatible)
    if body is None:
        return None
    status = re.search(r'status\s*=\s*"([a-z]+)"', body)
    return status.group(1) if status else 'okay'


def check_usb(text: str):
    """Check the actual controller, its 28nm PHY, and dummy VBUS phandles."""
    problems = []
    compat = {'controller': '"qcom,ci-hdrc"',
              'phy': '"qcom,usb-hs-28nm-femtophy"',
              'extcon': '"linux,extcon-usb-dummy"'}
    nodes = {name: body_of_compatible(text, val) for name, val in compat.items()}
    for name, body in nodes.items():
        if body is None:
            problems.append(f'missing {name} device-tree node')
        elif status_of_compatible(text, compat[name]) != 'okay':
            problems.append(f'{name} device-tree node is disabled')
    if problems:
        return problems
    controller = nodes['controller']
    if not re.search(r'dr_mode\s*=\s*"peripheral"', controller):
        problems.append('controller is not in peripheral mode')
    for prop, name in (('phys', 'phy'), ('extcon', 'extcon')):
        link = property_ints(controller, prop)
        target = property_ints(nodes[name], 'phandle')
        if not target or link != target:
            problems.append(f'controller {prop} does not point to the {name} node')
    return problems


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


def check_loader_ids(dtb: Path):
    """Check exact root-node IDs against the device-tested Rev06 donor.

    Use fdtget rather than searching decompiled text, so a same-named property
    in an unrelated child node cannot satisfy this critical loader guard.
    """
    fdtget = shutil.which('fdtget')
    if not fdtget:
        return ['fdtget is required to check root DTB identification']
    expected = {
        'qcom,msm-id': [303, 0, 308, 0, 309, 0],
        'qcom,board-id': [8, 6],
        'qcom,pmic-id': [0x10019, 0, 0, 0],
    }
    failures = []
    for prop, want in expected.items():
        proc = subprocess.run([fdtget, '-t', 'x', str(dtb), '/', prop],
                              capture_output=True, text=True)
        try:
            got = [int(x, 16) for x in proc.stdout.split()] if proc.returncode == 0 else None
        except ValueError:
            got = None
        if got != want:
            failures.append(f'root {prop} is {got}; Rev06 donor uses {want}')
    return failures


def check_dtb(mainline: Path, name: str, panel: bool, failures: list):
    dtb = mainline / f'{name}.dtb'
    if not dtb.is_file():
        failures.append(f'{name}: no built device tree ({dtb.name})')
        return
    text = dtc_text(dtb)
    if text is None:
        failures.append(f'{name}: dtc is required to verify the compiled device tree')
        return
    for expected in ('Samsung Galaxy J4+ (SM-J415F)', 'samsung,j4primelte'):
        if expected not in text:
            failures.append(f'{name}: device tree does not say "{expected}"')
    for problem in check_loader_ids(dtb):
        failures.append(f'{name}: {problem}')
    if '"ramoops"' not in text:
        failures.append(f'{name}: no ramoops reserved region, a panic before the '
                        'shell would leave nothing behind')
    problem = check_console(text)
    if problem:
        failures.append(f'{name}: {problem}')
    for problem in check_usb(text):
        failures.append(f'{name}: USB: {problem}')

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


def reference_lk_gzip_end(blob: bytes):
    """Emulate Qualcomm LK's gzip-header skip and raw DEFLATE end calculation.

    Reference: msm8916-mainline/lk2nd lib/zlib_inflate/decompress.c. This is a
    compatibility check against that implementation, not proof of Samsung LK.
    """
    if len(blob) < 18 or blob[:3] != b'\x1f\x8b\x08' or blob[3] not in (0, 8):
        raise ValueError('unsupported reference LK gzip header')
    pos = 10
    if blob[3] == 8:
        end = blob.find(b'\0', pos, min(len(blob), pos + 256))
        if end < 0:
            raise ValueError('invalid or oversized reference LK gzip filename')
        pos = end + 1
    raw = zlib.decompressobj(-zlib.MAX_WBITS)
    raw.decompress(blob[pos:])
    if not raw.eof:
        raise ValueError('truncated raw DEFLATE stream')
    deflate_end = len(blob) - len(raw.unused_data)
    if deflate_end + 8 > len(blob):
        raise ValueError('missing gzip footer')
    return deflate_end + 8


def check_appended(mainline: Path, name: str, failures: list):
    """Independently check gzip, the ARM64 Image header and the trailing DTB."""
    combined = mainline / f'{name}-Image.gz-dtb'
    dtb = mainline / f'{name}.dtb'
    if not combined.is_file() or not dtb.is_file():
        failures.append(f'{name}: missing {combined.name} or {dtb.name}')
        return
    blob, tree = combined.read_bytes(), dtb.read_bytes()
    if len(tree) < 40 or tree[:4] != b'\xd0\x0d\xfe\xed':
        failures.append(f'{name}: built DTB has no valid FDT header')
        return
    if struct.unpack_from('>I', tree, 4)[0] != len(tree):
        failures.append(f'{name}: DTB totalsize does not match the file')
        return
    if len(blob) <= len(tree) or blob[-len(tree):] != tree:
        failures.append(f'{name}: trailing bytes are not the built DTB')
        return
    dtb_offset = len(blob) - len(tree)
    compressed = blob[:dtb_offset]
    if len(compressed) < 18 or not compressed.startswith(b'\x1f\x8b\x08'):
        failures.append(f'{name}: kernel is not a complete gzip stream')
        return
    if compressed[3] != 0:
        failures.append(f'{name}: gzip has optional header fields; the J4+ '
                        'ARM64 reference uses plain gzip')
    try:
        stream = zlib.decompressobj(16 + zlib.MAX_WBITS)
        image = stream.decompress(compressed) + stream.flush()
    except zlib.error:
        failures.append(f'{name}: gzip kernel is invalid or truncated')
        return
    if not stream.eof:
        failures.append(f'{name}: gzip kernel is truncated')
        return
    if stream.unused_data:
        failures.append(f'{name}: gap between gzip EOF and DTB; reference LK '
                        'would look for the DTB in the gap')
    try:
        lk_offset = reference_lk_gzip_end(blob)
        if lk_offset != dtb_offset:
            failures.append(f'{name}: reference LK gzip end {lk_offset} differs '
                            f'from DTB offset {dtb_offset}')
    except (ValueError, zlib.error) as exc:
        failures.append(f'{name}: reference LK cannot parse the gzip header: {exc}')
    if len(image) < 64 or image[56:60] != b'ARM\x64':
        failures.append(f'{name}: decompressed payload has no ARM64 Image magic')
        return
    text_offset, image_size, flags = struct.unpack_from('<QQQ', image, 8)
    if not image_size or image_size < len(image):
        failures.append(f'{name}: ARM64 effective image size {image_size} is invalid')
    print(f'{name}: ARM64 Image {len(image)} B, effective {image_size} B, '
          f'text offset 0x{text_offset:x}, flags 0x{flags:x}, '
          f'DTB offset {dtb_offset}, gzip trailer gap {len(stream.unused_data)} B')


def check_wrapper(mainline: Path, name: str, failures: list):
    payload = mainline / f'{name}-Image.gz-dtb'
    original = mainline / f'{name}-original-Image.gz'
    tree = mainline / f'{name}.dtb'
    if not payload.is_file() or not tree.is_file():
        return  # Other gates report these.
    if not original.is_file():
        failures.append(f'{name}: missing original Image.gz for shim comparison')
        return
    try:
        blob = payload.read_bytes()
        tree_size = tree.stat().st_size
        wrapped = loader_checks.complete_gzip(blob[:-tree_size])
        inner = loader_checks.complete_gzip(original.read_bytes())
        report = loader_checks.validate_wrapper(wrapped, inner)
        print(f'{name}: register-preserving shim -> 2 MiB aligned '
              f'0x{report["inner_entry"]:x}; original Image SHA256 '
              f'{report["inner_sha256"]}')
    except (OSError, ValueError, zlib.error, struct.error) as exc:
        failures.append(f'{name}: invalid ARM64 alignment shim: {exc}')


def read_boot_independently(data: bytes):
    """Read a legacy boot.img without using the packer's parser or ID code."""
    if len(data) < 1632 or data[:8] != b'ANDROID!':
        raise ValueError('missing Android v0 header')
    (ks, ka, rs, ra, ss, sa, tags, page, version, osver) = \
        struct.unpack_from('<10I', data, 8)
    if version != 0 or page not in (2048, 4096):
        raise ValueError('unsupported header version or page size')
    if not ks or not rs:
        raise ValueError('empty kernel or initramfs')
    align = lambda n: ((n + page - 1) // page) * page
    expected_len = page + align(ks) + align(rs) + align(ss)
    if len(data) < expected_len:
        raise ValueError('truncated payload or padding')
    kernel_at = page
    ramdisk_at = kernel_at + align(ks)
    second_at = ramdisk_at + align(rs)
    kernel = data[kernel_at:kernel_at + ks]
    ramdisk = data[ramdisk_at:ramdisk_at + rs]
    second = data[second_at:second_at + ss]
    sha = hashlib.sha1()
    for part, length in ((kernel, ks), (ramdisk, rs), (second, ss)):
        sha.update(part)
        sha.update(struct.pack('<I', length))
    correct_id = sha.digest() + bytes(12)
    command = (data[64:576].split(b'\0', 1)[0] +
               data[608:1632].split(b'\0', 1)[0]).decode('ascii', 'strict')
    return {'kernel': kernel, 'ramdisk': ramdisk, 'second': second,
            'addresses': (ka, ra, sa, tags, page, version, osver),
            'cmdline': command, 'valid_id': data[576:608] == correct_id}


def root_uuid(command: str):
    values = [arg for arg in command.split() if arg.startswith('pmos_root_uuid=')]
    pattern = r'pmos_root_uuid=[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-' \
              r'[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
    return values[0] if len(values) == 1 and re.fullmatch(pattern, values[0]) else None


def check_boot(boot_dir: Path, base: Path, failures: list, required_images=()):
    if not base or not base.is_file():
        failures.append('no donor boot.img given to compare payloads against')
        return
    try:
        donor = read_boot_independently(base.read_bytes())
    except (ValueError, UnicodeError, struct.error) as exc:
        failures.append(f'donor: invalid boot image: {exc}')
        return
    if not donor['valid_id']:
        failures.append('donor: Android v0 payload/size checksum does not match')
    donor_uuid = root_uuid(donor['cmdline'])
    if not donor_uuid:
        failures.append('donor: missing, invalid or ambiguous pmos_root_uuid')
    images = sorted(boot_dir.glob('boot-*.img'))
    for name in required_images:
        if not (boot_dir / name).is_file():
            failures.append(f'missing required image: {name}')
    if not images:
        failures.append('no boot images were produced at all')
        return
    for image_path in images:
        try:
            data = image_path.read_bytes()
            img = read_boot_independently(data)
        except (ValueError, UnicodeError, struct.error) as exc:
            failures.append(f'{image_path.name}: invalid boot image: {exc}')
            continue
        name = image_path.name
        if not img['valid_id']:
            failures.append(f'{name}: Android v0 payload/size checksum mismatch')
        if img['ramdisk'] != donor['ramdisk']:
            failures.append(f'{name}: ramdisk is not the donor ramdisk byte for byte')
        if img['second'] != donor['second']:
            failures.append(f'{name}: second payload changed')
        if img['addresses'][1:] != donor['addresses'][1:]:
            failures.append(f'{name}: donor ramdisk/tags or other header fields changed')
        try:
            header = zlib.decompress(img['kernel'], 16 + zlib.MAX_WBITS)[:64]
            if len(header) < 64 or header[56:60] != b'ARM\x64':
                raise ValueError('not an ARM64 Image')
            offset = struct.unpack_from('<Q', header, 8)[0]
            if offset >= 2 * 1024 * 1024 or offset % 4096:
                raise ValueError('unsupported ARM64 text_offset')
            expected_addr = (donor['addresses'][0] & ~((1 << 21) - 1)) + offset
            if img['addresses'][0] != expected_addr:
                failures.append(f'{name}: ARM64 kernel load address is not '
                                f'0x{expected_addr:x} for text_offset 0x{offset:x}')
        except (ValueError, zlib.error, struct.error) as exc:
            failures.append(f'{name}: cannot verify ARM64 load address: {exc}')
        if len(data) > 32 * 1024 * 1024:
            failures.append(f'{name}: image exceeds the measured 32 MiB BOOT partition')
        if root_uuid(img['cmdline']) != donor_uuid or not donor_uuid:
            failures.append(f'{name}: pmos_root_uuid differs from the donor')
        match = re.fullmatch(r'boot-(m1|panel)-(rootfs|debug)\.img', name)
        if not match:
            failures.append(f'{name}: unexpected image name')
            continue
        variant, mode = match.groups()
        expected_payload = boot_dir.parent / f'{variant}-Image.gz-dtb'
        if not expected_payload.is_file():
            failures.append(f'{name}: missing staged kernel payload')
        elif img['kernel'] != expected_payload.read_bytes():
            failures.append(f'{name}: kernel does not match staged {variant} payload')
        debug_actual = 'pmos.debug-shell' in img['cmdline'].split()
        if (mode == 'debug') != debug_actual:
            failures.append(f'{name}: debug-shell flag does not match image mode')
        for flag in ('clk_ignore_unused', 'pd_ignore_unused',
                     'regulator_ignore_unused', 'rootwait', 'earlycon'):
            if flag not in img['cmdline'].split():
                failures.append(f'{name}: required boot argument {flag} missing')


def check_memory_layout(mainline: Path, base: Path, name: str, failures: list):
    """Check the exact-build loader model, including all DTB reservation tuples.

    The base, total RAM and SMEM entry ordering are inferred from the recorded
    phone; this check and the private bootloader emulation are not a device boot.
    """
    payload = mainline / f'{name}-Image.gz-dtb'
    dtb = mainline / f'{name}.dtb'
    if not base or not base.is_file() or not payload.is_file() or not dtb.is_file():
        return  # Missing artifacts are reported by other gates.
    try:
        donor = read_boot_independently(base.read_bytes())
        raw = zlib.decompress(payload.read_bytes(), 16 + zlib.MAX_WBITS)
        page = donor['addresses'][4]
        align = lambda n: (n + page - 1) // page * page
        boot_size = page + align(payload.stat().st_size) + \
                    align(len(donor['ramdisk'])) + align(len(donor['second']))
        occupied = loader_checks.runtime_layout(raw, dtb.stat().st_size,
                                                boot_size, len(donor['ramdisk']), page)
        loader_checks.require_loader_nodes(dtb)
        regions = loader_checks.audit_reservations(dtb, occupied)
        print(f'{name}: audited {len(regions)} active reservation tuples and '
              f'{len(occupied)} loader/kernel regions (recorded 2-GiB RAM model)')
    except (OSError, ValueError, UnicodeError, struct.error, zlib.error) as exc:
        failures.append(f'{name}: could not audit loader memory layout: {exc}')


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
    check_wrapper(mainline, 'm1', failures)
    check_dtb(mainline, 'm1', panel=False, failures=failures)
    if panel_expected:
        check_appended(mainline, 'panel', failures)
        check_wrapper(mainline, 'panel', failures)
        check_dtb(mainline, 'panel', panel=True, failures=failures)
    required = ['boot-m1-rootfs.img', 'boot-m1-debug.img']
    if panel_expected:
        required += ['boot-panel-rootfs.img', 'boot-panel-debug.img']
    check_boot(boot_dir, donor, failures, required_images=required)
    check_memory_layout(mainline, donor, 'm1', failures)
    if panel_expected:
        check_memory_layout(mainline, donor, 'panel', failures)
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
