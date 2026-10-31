#!/usr/bin/env python3
"""Guard new SukiSU priority UID calls when the selected SUSFS lacks the API."""

import argparse
from pathlib import Path
import re


COMMAND = "CMD_SUSFS_SET_PRIORITY_UID_FILTER"
PARSE = "susfs_parse_priority_app_strings"
SET = "susfs_set_priority_uid_filter"
GUARD = f"#if defined(CONFIG_KSU_SUSFS) && defined({COMMAND})"


def without_comments(text):
    # Preserve offsets so checks against C code can also locate edits in the source.
    return re.sub(
        r"/\*.*?\*/|//[^\n]*",
        lambda match: re.sub(r"[^\n]", " ", match.group()),
        text,
        flags=re.S,
    )


def read_if_exists(path):
    return path.read_text() if path.is_file() else ""


def c_code(text):
    return re.sub(r"(?m)^[ \t]*#.*$", lambda match: " " * len(match.group()), without_comments(text))


def has_function(text, name, ending):
    return bool(re.search(rf"\b{re.escape(name)}\s*\([^;{{}}]*\)\s*{ending}", text))


def requires_command(expression):
    # Accept positive conjunctions only; an OR or a negated comparison is not a guard.
    expression = re.sub(r"\bdefined\s*\(\s*(\w+)\s*\)", r"\1", expression)
    expression = re.sub(r"\bdefined\s+(\w+)", r"\1", expression)
    terms = [term.replace("(", "").replace(")", "").strip()
             for term in expression.split("&&")]
    return COMMAND in terms and all(re.fullmatch(r"\w+", term) for term in terms)


def guarded(text, offset):
    """Recognize existing command guards, including guards around a whole function."""
    stack = []
    for line in without_comments(text[:offset]).splitlines():
        match = re.match(r"\s*#\s*(ifdef|ifndef|if|elif|else|endif)\b\s*(.*)", line)
        if not match:
            continue
        directive, expression = match.groups()
        if directive in ("ifdef", "ifndef"):
            is_command = expression.strip() == COMMAND
            stack.append((is_command and directive == "ifdef",
                          is_command and directive == "ifndef"))
        elif directive in ("if", "elif"):
            inactive = expression.strip() == "0" or requires_command(expression)
            if directive == "elif" and stack:
                stack[-1] = (inactive, False)
            elif directive == "if":
                stack.append((inactive, False))
        elif directive == "else" and stack:
            stack[-1] = (stack[-1][1], stack[-1][0])
        elif directive == "endif" and stack:
            stack.pop()
    return any(inactive for inactive, _ in stack)


def protect(text, pattern):
    matches = list(re.finditer(pattern, without_comments(text), re.M))
    for match in reversed(matches):
        if guarded(text, match.start()):
            continue
        block = text[match.start():match.end()]
        text = text[:match.start()] + GUARD + "\n" + block + "#endif\n" + text[match.end():]
    return text


def fix(kernel_root):
    ksu_dir = kernel_root / "drivers/kernelsu"
    if not ksu_dir.is_dir():
        raise SystemExit(f"SukiSU source directory not found: {ksu_dir}")

    paths = (ksu_dir / "manager/pkg_observer.c", ksu_dir / "supercall/dispatch.c")
    originals = {path: read_if_exists(path) for path in paths}
    references = re.compile(rf"\b(?:{PARSE}|{SET}|{COMMAND})\b")
    if not any(references.search(c_code(text)) for text in originals.values()):
        print("SUSFS priority UID: SukiSU has no priority UID calls; no changes needed")
        return []

    header = without_comments(read_if_exists(kernel_root / "include/linux/susfs.h"))
    definitions = without_comments(read_if_exists(kernel_root / "include/linux/susfs_def.h"))
    implementation = without_comments(read_if_exists(kernel_root / "fs/susfs.c"))
    has_command = bool(re.search(rf"(?m)^\s*#\s*define\s+{COMMAND}\b", definitions))
    missing = [name for name in (PARSE, SET)
               if not (has_function(header, name, ";") and has_function(implementation, name, r"\{"))]
    if has_command and not missing:
        print("SUSFS priority UID: selected SUSFS provides the complete API; no changes needed")
        return []
    if has_command:
        raise SystemExit("SUSFS priority UID: command exists but declarations/implementations are "
                         f"incomplete ({', '.join(missing)}); refusing to hide an inconsistent API")

    patterns = (
        rf"^[ \t]*{PARSE}\s*\(\s*\)\s*;[^\n]*\n",
        rf"^[ \t]*case\s+{COMMAND}\s*:[ \t]*\n"
        rf"[ \t]*{SET}\s*\(\s*arg\s*\)\s*;[^\n]*\n"
        r"[ \t]*return\s+0\s*;[^\n]*\n",
    )
    updated = {path: protect(originals[path], pattern)
               for path, pattern in zip(paths, patterns)}
    # Validate every reference before writing either file. Unknown upstream layouts fail early.
    for path, text in updated.items():
        for match in references.finditer(c_code(text)):
            if not guarded(text, match.start()):
                raise SystemExit(f"SUSFS priority UID: unsupported unguarded {match.group()} in {path}; "
                                 "upstream layout changed, no files written")

    changed = []
    for path, text in updated.items():
        if text != originals[path]:
            path.write_text(text)
            changed.append(path)
            print(f"SUSFS priority UID: guarded unsupported calls in {path}")
    if not changed:
        print("SUSFS priority UID: upstream guards already protect unsupported calls; no changes needed")
    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kernel_root", type=Path, help="actual kernel source root, e.g. kernel_workspace/common")
    args = parser.parse_args()
    fix(args.kernel_root.resolve())


if __name__ == "__main__":
    main()
