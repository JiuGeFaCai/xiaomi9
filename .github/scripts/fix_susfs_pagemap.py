#!/usr/bin/env python3
"""Adapt SUSFS pagemap patch context and repair missing local declarations."""

import argparse
from pathlib import Path
import re


DECLARATIONS = {
    "nr_subpages": (
        re.compile(
            r"\b(?:unsigned(?:\s+(?:int|long(?:\s+long)?))?|int|long|u32|u64|size_t)"
            r"\s+nr_subpages\b"
        ),
        "unsigned int nr_subpages = __PAGE_SIZE / PAGE_SIZE;",
    ),
    "res": (
        re.compile(r"\bpagemap_entry_t\s*\*\s*(?:const\s+)?res\b"),
        "pagemap_entry_t *res = NULL;",
    ),
}


def pagemap_body(source):
    # Keep offsets and newlines while excluding comments and string literals.
    code = re.sub(
        r"""/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'""",
        lambda match: re.sub(r"[^\n]", " ", match[0]),
        source,
        flags=re.S,
    )
    signature = re.search(
        r"\bstatic\s+ssize_t\s+pagemap_read\s*\([^;{}]*\)\s*\{", code
    )
    if signature is None:
        return None

    start = signature.end()
    depth = 1
    for end in range(start, len(code)):
        if code[end] == "{":
            depth += 1
        elif code[end] == "}":
            depth -= 1
            if depth == 0:
                return start, end, code[start:end]
    raise SystemExit("SUSFS pagemap: cannot locate the end of pagemap_read()")


def adapt_patch(kernel_dir, patch_path):
    bounds = pagemap_body((kernel_dir / "fs/proc/task_mmu.c").read_text())
    if bounds is None:
        print("SUSFS pagemap patch: no pagemap_read() to adapt")
        return False

    missing = {
        symbol for symbol, (pattern, _) in DECLARATIONS.items()
        if not pattern.search(bounds[2])
    }
    lines = patch_path.read_text().splitlines(keepends=True)
    marker = "diff --git a/fs/proc/task_mmu.c b/fs/proc/task_mmu.c\n"
    if not missing or marker not in lines:
        print("SUSFS pagemap patch: no missing declaration context to adapt")
        return False

    file_start = lines.index(marker)
    file_end = next(
        (i for i in range(file_start + 1, len(lines)) if lines[i].startswith("diff --git ")),
        len(lines),
    )
    hunks = [
        i for i in range(file_start, file_end) if lines[i].startswith("@@ ")
    ]
    absent_context = {DECLARATIONS[symbol][1] for symbol in missing}
    removed = 0
    for start, end in reversed(list(zip(hunks, hunks[1:] + [file_end]))):
        body = lines[start + 1:end]
        if not any("pagemap_read(" in line for line in [lines[start], *body]):
            continue
        # Drop only absent context, never declarations added by an upstream fix.
        kept = [
            line for line in body
            if not (
                line.startswith(" ")
                and line[1:].strip() in absent_context
            )
        ]
        if kept == body:
            continue
        header = re.fullmatch(
            r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)",
            lines[start].rstrip("\n"),
        )
        if header is None:
            raise SystemExit("SUSFS pagemap patch: invalid hunk header")
        old_count = sum(line.startswith((" ", "-")) for line in kept)
        new_count = sum(line.startswith((" ", "+")) for line in kept)
        new_header = (
            f"@@ -{header[1]},{old_count} +{header[3]},{new_count} @@{header[5]}\n"
        )
        removed += len(body) - len(kept)
        lines[start:end] = [new_header, *kept]

    if not removed:
        print("SUSFS pagemap patch: no missing declaration context to adapt")
        return False
    patch_path.write_text("".join(lines))
    print(f"SUSFS pagemap patch: removed {removed} absent context lines")
    return True


def repair_declarations(kernel_dir):
    path = kernel_dir / "fs/proc/task_mmu.c"
    source = path.read_text()
    bounds = pagemap_body(source)
    if bounds is None:
        print("SUSFS pagemap: no pagemap_read() to repair")
        return False

    start, _, body = bounds
    missing = [
        symbol for symbol, (pattern, _) in DECLARATIONS.items()
        if re.search(rf"\b{symbol}\b", body) and not pattern.search(body)
    ]
    if not missing:
        print("SUSFS pagemap: declarations already exist or variables are unused")
        return False

    anchor = re.search(
        r"(?m)^([ \t]*)int\s+ret\s*=\s*0\s*,\s*copied\s*=\s*0\s*;[ \t]*\n",
        body,
    )
    if anchor is None:
        raise SystemExit("SUSFS pagemap: missing local declaration insertion anchor")

    position = start + anchor.end()
    additions = "".join(
        f"{anchor[1]}{DECLARATIONS[symbol][1]}\n" for symbol in missing
    )
    path.write_text(source[:position] + additions + source[position:])
    print("SUSFS pagemap: restored missing declarations: " + ", ".join(missing))
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kernel_dir", type=Path)
    parser.add_argument("--patch", dest="patch_path", type=Path)
    args = parser.parse_args()
    if args.patch_path is not None:
        adapt_patch(args.kernel_dir, args.patch_path)
    else:
        repair_declarations(args.kernel_dir)
