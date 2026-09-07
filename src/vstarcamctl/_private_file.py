"""Atomic creation of private local artifacts."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def write_private_file(path: Path, data: bytes, *, overwrite: bool) -> None:
    """Atomically publish *data* at *path* with private-file defaults.

    The complete payload is written beside the destination before it becomes
    visible. Without ``overwrite``, a hard link provides an atomic
    create-if-absent operation; with it, ``os.replace`` provides the atomic
    replacement. POSIX files are forced to mode ``0600``. On Windows, the
    temporary file inherits the destination directory's ACL.
    """

    parent = path.parent.resolve(strict=True)
    destination = parent / path.name
    descriptor, temporary_name = tempfile.mkstemp(
        dir=parent,
        prefix=f".{path.name}.",
    )
    temporary = Path(temporary_name)
    try:
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)

        stream = os.fdopen(descriptor, "wb")
        descriptor = -1
        with stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

        if overwrite:
            os.replace(temporary, destination)
        else:
            os.link(temporary, destination)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
