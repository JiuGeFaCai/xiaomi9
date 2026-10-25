#!/usr/bin/env python3
"""Adapt the downloaded SUSFS namespace patch to kernels without the blk hook."""

import argparse
from pathlib import Path
import re


def adapt_patch(kernel_dir, patch_path):
    hook_include = "#include <trace/hooks/blk.h>"
    namespace = (kernel_dir / "fs/namespace.c").read_text()
    if hook_include in namespace:
        print("SUSFS namespace patch: blk hook context already exists")
        return

    lines = patch_path.read_text().splitlines(keepends=True)
    file_marker = "diff --git a/fs/namespace.c b/fs/namespace.c\n"
    if file_marker not in lines:
        print("SUSFS namespace patch: no namespace section to adapt")
        return

    file_start = lines.index(file_marker)
    file_end = next(
        (i for i in range(file_start + 1, len(lines)) if lines[i].startswith("diff --git ")),
        len(lines),
    )
    hunk_start = next(
        (i for i in range(file_start, file_end) if lines[i].startswith("@@ ")),
        None,
    )
    if hunk_start is None:
        raise SystemExit("SUSFS namespace patch has no hunk")
    hunk_end = next(
        (i for i in range(hunk_start + 1, file_end) if lines[i].startswith("@@ ")),
        file_end,
    )

    body = lines[hunk_start + 1:hunk_end]
    context = " " + hook_include + "\n"
    if context not in body or "+#include <linux/susfs_def.h>\n" not in body:
        print("SUSFS namespace patch: no missing blk hook context to adapt")
        return

    header = re.fullmatch(
        r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)",
        lines[hunk_start].rstrip("\n"),
    )
    if header is None:
        raise SystemExit("SUSFS namespace patch has an invalid hunk header")

    # Remove only the absent context line; preserve every SUSFS addition.
    body.remove(context)
    old_count = sum(line.startswith((" ", "-")) for line in body)
    new_count = sum(line.startswith((" ", "+")) for line in body)
    new_header = (
        f"@@ -{header[1]},{old_count} +{header[3]},{new_count} @@{header[5]}\n"
    )
    lines[hunk_start:hunk_end] = [new_header] + body
    patch_path.write_text("".join(lines))
    print("SUSFS namespace patch: removed absent blk hook context and updated hunk counts")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kernel_dir", type=Path)
    parser.add_argument("patch_path", type=Path)
    args = parser.parse_args()
    adapt_patch(args.kernel_dir, args.patch_path)
