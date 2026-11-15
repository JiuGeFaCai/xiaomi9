#!/usr/bin/env python3
"""Set the final kernel suffix without rewriting setlocalversion's helpers."""

import argparse
from pathlib import Path
import re
import shlex
import subprocess


def set_suffix(kernel_root, suffix):
    if not suffix.startswith("-") or any(char in suffix for char in "\n\r\0"):
        raise ValueError("kernel suffix must start with '-' and fit on one line")

    path = kernel_root / "scripts/setlocalversion"
    lines = path.read_text().splitlines(keepends=True)
    last = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].strip()), None)
    output = lines[last].strip() if last is not None else ""
    if re.fullmatch(r'echo "\$\{KERNELVERSION\}[^"\n]*"', output):
        # Modern kernels print the full release; older kernels print a suffix.
        replacement = "printf '%s%s\\n' \"${KERNELVERSION}\" "
    elif output in ('echo "$res"', 'echo "${res}"'):
        replacement = "printf '%s\\n' "
    else:
        raise ValueError(f"{path}: unrecognized final output; no changes written")

    # Treat user input as literal shell data, including quotes, $ and backticks.
    lines[last] = replacement + shlex.quote(suffix) + "\n"
    updated = "".join(lines)
    check = subprocess.run(["sh", "-n"], input=updated, text=True, capture_output=True)
    if check.returncode:
        raise ValueError(f"{path}: shell syntax check failed; no changes written:\n{check.stderr}")
    path.write_text(updated)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kernel_root", type=Path)
    parser.add_argument("--suffix", required=True)
    args = parser.parse_args()
    set_suffix(args.kernel_root, args.suffix)


if __name__ == "__main__":
    main()
