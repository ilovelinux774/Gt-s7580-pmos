#!/usr/bin/env python3
"""Disable only the Rev06 sec_ts node for the K49 boot-isolation test."""
from pathlib import Path
import sys

if len(sys.argv) != 2:
    raise SystemExit(f"usage: {sys.argv[0]} <Rev06 board dtsi>")

path = Path(sys.argv[1])
source = path.read_text()
needle = '\ttouchscreen@48 {\n\t\t\tcompatible = "sec,sec_ts";'
replacement = (
    '\ttouchscreen@48 {\n'
    '\t\t\tstatus = "disabled"; /* K49 boot-isolation test: sec_ts */\n'
    '\t\t\tcompatible = "sec,sec_ts";'
)
if source.count(needle) != 1:
    raise SystemExit("expected exactly one unmodified Rev06 sec_ts node")
path.write_text(source.replace(needle, replacement, 1))
print("disabled Rev06 sec_ts node at I2C address 0x48; I2C adapter unchanged")
