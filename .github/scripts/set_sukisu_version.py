#!/usr/bin/env python3
"""Read SukiSU's own version rules and pin matching build metadata."""

import argparse
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import tempfile


VERSION_ASSIGNMENT = re.compile(r"^[ \t]*KSU_VERSION[ \t]*:?=", re.MULTILINE)
PROBE_PREFIX = "__SUKISU_METADATA__"


def check_line(value, label):
    if "\0" in value or any(char in value for char in "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"):
        raise ValueError(f"{label} must fit on one line")


def read_metadata(repo_root, path):
    # Parse the real Kbuild/Makefile without running a kernel compilation.
    # Bound upstream's API calls so unavailable release servers cannot hang CI.
    curl = shutil.which("curl")
    if not curl:
        raise ValueError("curl is required to read upstream release metadata")
    with tempfile.TemporaryDirectory(prefix="sukisu-version-") as directory:
        root = Path(directory)
        wrapper = root / "curl"
        wrapper.write_text(
            "#!/bin/sh\nexec " + shlex.quote(curl)
            + ' --connect-timeout 5 --max-time 10 "$@"\n'
        )
        wrapper.chmod(0o755)
        kernel_tree = repo_root.parent / "common"
        if not kernel_tree.is_dir():
            kernel_tree = root
        include_path = str(path).replace("$", "$$").replace("#", r"\#").replace(" ", r"\ ")
        probe = f"include {include_path}\n"
        for key in ("KSU_VERSION", "KSU_VERSION_FULL", "KSU_PACKAGE_NAME", "REPO_NAME"):
            probe += f"$(info {PROBE_PREFIX}{key}=$({key}))\n"
        probe += ".PHONY: sukisu_version_metadata\nsukisu_version_metadata:\n"
        command = [
            "make", "--no-print-directory", "-s", "-f", "-", "sukisu_version_metadata",
            f"srctree={kernel_tree}", f"src={path.parent}", f"objtree={root}", f"obj={root}",
            "VERSION=6", "PATCHLEVEL=6", "KERNELRELEASE=sukisu-version", "CONFIG_KSU=y",
            f"KSU_SRC={path.parent}", f"CURL_BIN={wrapper}",
        ]
        process = subprocess.Popen(
            command, cwd=repo_root, env={**os.environ, "MAKEFLAGS": "", "MFLAGS": ""},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(probe, timeout=45)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise ValueError("upstream version lookup timed out") from None
        if process.returncode:
            raise ValueError(f"{path}: version evaluation failed:\n{stderr.strip()}")
        return dict(
            line[len(PROBE_PREFIX):].split("=", 1)
            for line in stdout.splitlines() if line.startswith(PROBE_PREFIX)
        )


def make_value(value):
    if not any(char in value for char in "$#\\") and value == value.rstrip():
        return value
    # printf decodes literal bytes; input never becomes shell or Make syntax.
    encoded = "".join(f"\\0{byte:03o}" for byte in value.encode())
    return "$(shell printf '%b' '" + encoded + "')"


def version_flag(value):
    # Quote one complete compiler argument. Octal escapes preserve UTF-8 and
    # punctuation through both Make and the shell, including quotes and '$'.
    safe = b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-@+/:=,"
    encoded = "".join(chr(byte) if byte in safe else f"\\{byte:03o}" for byte in value.encode())
    return shlex.quote('-DKSU_VERSION_FULL="' + encoded + '"')


def patch_metadata(source, version, full):
    result = []
    logical_line = ""
    full_flags = 0
    full_definitions = 0
    for line in source.splitlines(keepends=True):
        logical_line += line
        if line.rstrip("\r\n").endswith("\\"):
            continue
        assignment = re.match(
            r"^([ \t]*)(KSU_VERSION|KSU_VERSION_FULL|KSU_VERSION_FALLBACK)[ \t]*:?=",
            logical_line,
        )
        flag = re.fullmatch(
            r"([ \t]*(?:ccflags-y|CFLAGS_core/init\.o)[ \t]*\+=[ \t]*)"
            r"(?:-DKSU_VERSION_FULL=\S+|'-DKSU_VERSION_FULL=\S+')\s*",
            logical_line,
        )
        if assignment:
            indent, name = assignment.groups()
            value = make_value(full) if name == "KSU_VERSION_FULL" else str(version)
            result.append(f"{indent}{name} := {value}\n")
            full_definitions += name == "KSU_VERSION_FULL"
        elif flag:
            result.append(flag.group(1) + version_flag(full) + "\n")
            full_flags += 1
        else:
            result.append(re.sub(r"(-DKSU_VERSION=)[0-9]+\b", rf"\g<1>{version}", logical_line))
        logical_line = ""
    result.append(logical_line)
    if full_definitions and not full_flags:
        raise ValueError("unrecognized KSU_VERSION_FULL compiler flag; no changes written")
    return "".join(result)


def set_version(repo_root, ref, version="", suffix=""):
    repo_root = repo_root.resolve()
    check_line(ref, "SukiSU ref")
    check_line(suffix, "SukiSU suffix")
    if not ref:
        raise ValueError("SukiSU ref must not be empty")
    version = "".join(version.split())
    if version and not re.fullmatch(r"[0-9]+", version):
        raise ValueError("SukiSU version override must contain only digits")
    kernel = repo_root / "kernel"
    kbuild = kernel / "Kbuild"
    version_fragment = kernel / "kbuild-config/version.mk"
    sources = {
        path: path.read_text()
        for path in (kbuild, version_fragment, kernel / "Makefile") if path.is_file()
    }
    path = next((path for path, source in sources.items() if VERSION_ASSIGNMENT.search(source)), None)
    if path is None:
        raise ValueError("SukiSU KSU_VERSION definition not found")
    # Kbuild sets MDIR and loads the manager package alongside version.mk.
    build_file = kbuild if path == version_fragment else path
    metadata = read_metadata(repo_root, build_file)
    number = version or metadata.get("KSU_VERSION", "")
    if not re.fullmatch(r"[0-9]+", number):
        raise ValueError(f"{path}: invalid upstream KSU_VERSION: {number!r}")
    number = int(number)
    full = metadata.get("KSU_VERSION_FULL", "")
    check_line(full, "upstream SukiSU version")
    if not full or "@" not in full:
        raise ValueError(f"{path}: unrecognized upstream KSU_VERSION_FULL")
    # Detached commits should show the requested ref, and repeated runs should
    # replace the workflow suffix rather than appending it again.
    full = full.split("@", 1)[0] + "@" + ref
    if suffix:
        full += "-" + suffix
    is_nova = metadata.get("KSU_PACKAGE_NAME", "").startswith("com.sukisu.nova")
    is_nova |= metadata.get("REPO_NAME") == "SukiSU-Nova"
    name = "SukiSU_Nova" if is_nova else "SukiSU_Ultra"
    updates = {path: patch_metadata(source, number, full) for path, source in sources.items()}
    for path, updated in updates.items():
        if updated != sources[path]:
            path.write_text(updated)
    return {"name": name, "version": number, "full": full}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo_root", type=Path)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--version", default="")
    parser.add_argument("--suffix", default="")
    args = parser.parse_args()
    try:
        metadata = set_version(args.repo_root, args.ref, args.version, args.suffix)
    except (OSError, ValueError) as error:
        parser.exit(1, f"SukiSU version error: {error}\n")
    for key in ("name", "version", "full"):
        print(metadata[key])


if __name__ == "__main__":
    main()
