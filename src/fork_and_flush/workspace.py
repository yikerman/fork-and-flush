"""Filesystem operations on workspaces and snapshots."""

from __future__ import annotations

import os
import shutil
import stat
import sys

# The orchestrator's own directory inside every branch workspace; never copied between them.
FF_DIR = ".ff"


def copy_tree(src: str, dst: str) -> None:
    """Replace dst with a full copy of src, except src's top-level .ff/."""
    remove_tree(dst)
    top = os.path.realpath(src)

    def skip_ff(directory, names):
        return [FF_DIR] if os.path.realpath(directory) == top and FF_DIR in names else []

    try:
        shutil.copytree(src, dst, symlinks=True, ignore=skip_ff)
    except shutil.Error as e:  # sockets, FIFOs and unreadable files; the rest is copied
        print(f"warning: {len(e.args[0])} entries not copied from {src}", file=sys.stderr)


def _chmod_tree(path: str, change) -> None:
    for root, dirs, files in os.walk(path):
        for p in [root] + [os.path.join(root, n) for n in dirs + files]:
            if not os.path.islink(p):
                os.chmod(p, change(stat.S_IMODE(os.lstat(p).st_mode)))


def make_readonly(path: str) -> None:
    _chmod_tree(path, lambda mode: mode & ~0o222)


def remove_tree(path: str | None) -> None:
    if not path or not os.path.lexists(path):
        return
    if os.path.isdir(path) and not os.path.islink(path):
        _chmod_tree(path, lambda mode: mode | 0o700)  # snapshots are read-only
        shutil.rmtree(path)
    else:
        os.remove(path)
