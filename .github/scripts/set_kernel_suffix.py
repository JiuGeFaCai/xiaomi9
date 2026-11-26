#!/usr/bin/env python3
"""Set the final kernel suffix without rewriting setlocalversion's helpers."""

import argparse
from pathlib import Path
import re
import shlex
import subprocess


def set_suffix(kernel_root, suffix=None, build_number=""):
    if suffix is not None and (not suffix.startswith("-") or any(char in suffix for char in "\n\r\0")):
        raise ValueError("kernel suffix must start with '-' and fit on one line")
    if build_number and not re.fullmatch(r"[0-9]+", build_number):
        raise ValueError("CI build number must contain only digits")
    if suffix is None and not build_number:
        raise ValueError("specify a kernel suffix or a CI build number")

    path = kernel_root / "scripts/setlocalversion"
    lines = path.read_text().splitlines(keepends=True)
    last = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].strip()), None)
    output = lines[last].strip() if last is not None else ""
    if re.fullmatch(r'echo "\$\{KERNELVERSION\}[^"\n]*"', output):
        # Modern kernels print the full release; older kernels print a suffix.
        prefix = '"${KERNELVERSION}"'
    elif output in ('echo "$res"', 'echo "${res}"'):
        prefix = ""
    else:
        raise ValueError(f"{path}: unrecognized final output; no changes written")

    # Treat user input as literal shell data, including quotes, $ and backticks.
    release = output.removeprefix("echo ") if suffix is None else prefix + shlex.quote(suffix)
    if build_number:
        # The native release may already include Android's -ab<id> stamp.
        stamp = "-ab" + build_number
        lines[last] = (f"kernel_release={release}\n"
                       f"printf '%s\\n' \"${{kernel_release%{stamp}}}{stamp}\"\n")
    else:
        lines[last] = f"printf '%s\\n' {release}\n"
    updated = "".join(lines)
    check = subprocess.run(["sh", "-n"], input=updated, text=True, capture_output=True)
    if check.returncode:
        raise ValueError(f"{path}: shell syntax check failed; no changes written:\n{check.stderr}")
    path.write_text(updated)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kernel_root", type=Path)
    parser.add_argument("--suffix")
    parser.add_argument("--build-number", default="")
    args = parser.parse_args()
    set_suffix(args.kernel_root, args.suffix, args.build_number)


if __name__ == "__main__":
    main()
