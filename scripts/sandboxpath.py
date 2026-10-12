#!/usr/bin/python3
"""Cross-platform path mapping for the channel tests.

The Action channel is a *Linux* system component. `_check_write_path` and
`file.restore` deliberately reason about target-system path semantics using
`posixpath`, so they must NOT switch to os.path just because the test happens
to run on Windows. What has to be adjustable is the *sandbox*: the tests need
to point BACKUP_DIR / WRITE_ALLOWLIST at a temp dir, and that temp dir has a
native path.

This module is the seam. It gives the tests two views of the same sandbox:

  * `linux(...)`  -- the POSIX-shaped string that the channel itself will see
  * `native(...)` -- the OS-shaped path that Python's file APIs can actually open

On Linux the two are identical, which is why nothing else in the repo needs to
know this exists. On Windows they differ, and the mapping is explicit rather
than a surprise.
"""

from __future__ import annotations

import os
import tempfile

_ROOT: str | None = None


def root() -> str:
    """A sandbox root, in the native form this host can write to."""
    global _ROOT
    if _ROOT is None:
        _ROOT = tempfile.mkdtemp(prefix="aos-sandbox-")
    return _ROOT


def native(posix: str) -> str:
    """Turn a sandbox-relative POSIX path into one this host can open.

    `/etc/autonomic` -> `<root>/etc/autonomic` on any OS.
    """
    if os.name == "nt":
        return root() + posix.replace("/", os.sep)
    return posix if posix.startswith("/") else root() + posix


def posix(native_path: str) -> str:
    """The inverse: a native path -> the POSIX string the channel sees."""
    if os.name == "nt" and native_path.startswith(root()):
        return native_path[len(root()):].replace(os.sep, "/")
    return native_path
