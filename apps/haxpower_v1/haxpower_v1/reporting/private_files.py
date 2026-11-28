"""Per-site private storage for generated files and future attachment adapters."""

from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from stat import S_ISREG
import os
import tempfile

import frappe

from ..inventory.operations.dto import StockError

MAX_SIZE = 10 * 1024 * 1024


class Area(StrEnum):
    EXPORTS = "exports"
    ATTACHMENTS = "attachments"
    MIGRATION = "migration"


def directory(area=Area.EXPORTS):
    # Frappe's public and private file handlers serve separate files/ roots.
    base = Path(frappe.get_site_path("private", "haxpower_v1"))
    base.mkdir(mode=0o700, exist_ok=True)
    if base.is_symlink() or not base.is_dir():
        raise StockError("PRIVATE_FILE_UNAVAILABLE")
    path = base / Area(area)
    path.mkdir(mode=0o700, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise StockError("PRIVATE_FILE_UNAVAILABLE")
    return path


def filename(identity, *, area=Area.EXPORTS):
    from ..access.policy import _record_id

    return directory(area) / (_record_id(identity) + ".xlsx")


def store(identity, write, *, area=Area.EXPORTS):
    path = filename(identity, area=area)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".generating-")
    try:
        with os.fdopen(descriptor, "w+b") as stream:
            write(stream)
            size = stream.tell()
            if not 0 < size <= MAX_SIZE:
                raise StockError("EXPORT_FILE_LIMIT")
            stream.flush()
            os.fsync(stream.fileno())
            stream.seek(0)
            checksum = sha256(stream.read(MAX_SIZE + 1)).hexdigest()
        os.link(temporary, path, follow_symlinks=False)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return size, checksum
    finally:
        Path(temporary).unlink(missing_ok=True)


def read(identity, expected_size, checksum, *, area=Area.EXPORTS):
    if not 0 < expected_size <= MAX_SIZE:
        raise StockError("PRIVATE_FILE_UNAVAILABLE")
    try:
        descriptor = os.open(filename(identity, area=area), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            status = os.fstat(stream.fileno())
            if not S_ISREG(status.st_mode) or status.st_size != expected_size:
                raise StockError("PRIVATE_FILE_UNAVAILABLE")
            data = stream.read(MAX_SIZE + 1)
    except OSError:
        raise StockError("PRIVATE_FILE_UNAVAILABLE") from None
    if len(data) != expected_size or sha256(data).hexdigest() != checksum:
        raise StockError("PRIVATE_FILE_UNAVAILABLE")
    return data
