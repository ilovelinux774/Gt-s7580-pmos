#!/usr/bin/env python3
"""Import the pinned J4+ Rev06 board DTS into the Redmi 4.19 kernel tree."""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import urllib.request

SOURCE_REPO = os.environ.get("J4_DTS_SOURCE_REPO", "geckyn/kernel_msm8917-4.9")
SOURCE_COMMIT = os.environ.get(
    "J4_DTS_SOURCE_COMMIT", "5713b6504118bcee2ef2aa7823c3446142e2166b"
)
FILES = (
    "arch/arm64/boot/dts/samsung/msm8917-sec-j4primelte-sea-open-r06.dts",
    "arch/arm64/boot/dts/samsung/msm8917-sec-j4primelte-sea-open-r06.dtsi",
    "arch/arm64/boot/dts/samsung/msm8917-sec-lte-common.dtsi",
    "arch/arm64/boot/dts/samsung/msm8917-pinctrl-sec-j4primelte-sea-open-r06.dtsi",
    "arch/arm64/boot/dts/samsung/msm8917-sec-j4primelte-battery-common-r03.dtsi",
    "arch/arm64/boot/dts/samsung/msm8917-sec-j4primelte-battery-common.dtsi",
    "arch/arm64/boot/dts/samsung/msm8917-j4primelte-s2mu005.dtsi",
    "arch/arm64/boot/dts/samsung/msm8917-sec-j4primelte-display-r03.dtsi",
    "arch/arm64/boot/dts/samsung/msm8917-sec-j4primelte-sensor-r03.dtsi",
    "arch/arm64/boot/dts/samsung/msm8917-sec-j4primelte-nfc-r04.dtsi",
    "arch/arm64/boot/dts/samsung/msm8917-mdss-panels.dtsi",
    "drivers/video/fbdev/msm/samsung/S6D7AT0B01_PM6003XB1/dsi_panel_S6D7AT0B01_PM6003XB1_hdp_video.dtsi",
    "drivers/video/fbdev/msm/samsung/S6D7AT0B01_BS060WKM/dsi_panel_S6D7AT0B01_BS060WKM_hdp_video.dtsi",
    "drivers/video/fbdev/msm/samsung/HX83102D_BS060WKM/dsi_panel_HX83102D_BS060WKM_hdp_video.dtsi",
    "drivers/video/fbdev/msm/samsung/PBA_BOOTING/dsi_panel_PBA_BOOTING_fhd_video.dtsi",
)


def fetch_file(path: str) -> bytes:
    url = f"https://api.github.com/repos/{SOURCE_REPO}/contents/{path}?ref={SOURCE_COMMIT}"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "j4primelte-k419-port"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=45) as response:
        item = json.load(response)
    if item.get("encoding") != "base64" or item.get("path") != path:
        raise SystemExit(f"unexpected GitHub contents response for {path}")
    return base64.b64decode(item["content"], validate=False)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("kernel_tree", type=Path)
    args = parser.parse_args()
    tree = args.kernel_tree.resolve()

    qcom = tree / "arch/arm64/boot/dts/vendor-legacy/qcom"
    for required in ("msm8917.dtsi", "msm8917-audio.dtsi"):
        if not (qcom / required).is_file():
            raise SystemExit(f"4.19 shared SoC DTS is missing: {qcom / required}")

    for source_path in FILES:
        data = fetch_file(source_path)
        rel = source_path.removeprefix("arch/arm64/boot/dts/")
        destination = tree / "arch/arm64/boot/dts" / rel if source_path.startswith("arch/") else tree / source_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        text = data.decode("utf-8")
        if source_path.endswith("msm8917-sec-j4primelte-sea-open-r06.dts"):
            text = text.replace('../qcom/msm8917.dtsi', '../vendor-legacy/qcom/msm8917.dtsi')
            text = text.replace('../qcom/msm8917-audio.dtsi', '../vendor-legacy/qcom/msm8917-audio.dtsi')
        if source_path.endswith("msm8917-sec-j4primelte-display-r03.dtsi"):
            text = text.replace('"samsung/msm8917-mdss-panels.dtsi"', '"msm8917-mdss-panels.dtsi"')
        if source_path.endswith("msm8917-sec-lte-common.dtsi"):
            # Riva's 4.19 GCC DTS lacks the old clock_gcc phandle used by
            # Samsung's optional crypto-acceleration node; omit that node.
            mcd = re.compile(r"^\tmcd: mcd \{.*?^\t\};\n", re.M | re.S)
            text, count = mcd.subn(
                "\t/* Samsung MCD node omitted: legacy clock_gcc phandle. */\n",
                text,
                count=1,
            )
            if count != 1:
                raise SystemExit("could not identify the Samsung MCD node")
        if source_path.endswith("msm8917-sec-j4primelte-sea-open-r06.dtsi"):
            # Camera sensors are not part of the first initramfs/RNDIS boot test;
            # Riva 4.19 labels its I2C buses differently from the Samsung 4.9 DTS.
            text = text.replace(
                '#include "msm8917-camera-sensor-cdp-j4primelte-r04.dtsi"',
                '/* camera sensor DTS omitted for initial 4.19 bring-up */',
            )
            # The 4.19 reference has no Samsung sec_ts driver. Keep the I2C bus
            # but suppress this nonessential device for the initial boot test.
            pattern = re.compile(r'(\btouchscreen@48\s*\{)(.*?)(\n\s*\};)', re.S)
            matches = list(pattern.finditer(text))
            if len(matches) != 1 or 'compatible = "sec,sec_ts";' not in matches[0].group(2):
                raise SystemExit("could not identify exactly one J4+ sec_ts node")
            if 'status = "disabled";' not in matches[0].group(2):
                text = pattern.sub(
                    lambda match: match.group(1)
                    + '\n\t\t\tstatus = "disabled"; /* initial 4.19 bring-up */'
                    + match.group(2)
                    + match.group(3),
                    text,
                    count=1,
                )
        destination.write_text(text)
        print(f"imported {source_path} -> {destination.relative_to(tree)}")

    dts_makefile = tree / "arch/arm64/boot/dts/Makefile"
    makefile = dts_makefile.read_text()
    if "subdir-y += samsung" not in makefile:
        dts_makefile.write_text(makefile + "\nsubdir-y += samsung\n")
    samsung_makefile = tree / "arch/arm64/boot/dts/samsung/Makefile"
    samsung_makefile.write_text(
        "# J4+ Rev06 board DTB imported from the pinned Samsung Linux 4.9 tree.\n"
        "dtb-y += msm8917-sec-j4primelte-sea-open-r06.dtb\n"
    )
    print("registered the Samsung DTS subdirectory and J4+ Rev06 DTB")


if __name__ == "__main__":
    main()
