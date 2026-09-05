"""Save-name sanitisation (section 6/24)."""

import re

from .const import MAX_FILENAME_LEN
from .errors import FileError

_ALLOWED = re.compile(r"^[A-Za-z0-9._-]+$")


def sanitize_filename(name: str) -> str:
    """Return a safe base name, or raise FileError.

    Any path component is stripped first, so "../../etc/passwd" degrades to
    "passwd" rather than escaping the download directory.
    """
    if not name:
        raise FileError("empty filename")
    candidate = name.replace("\\", "/").split("/")[-1]
    if candidate in ("", ".", ".."):
        raise FileError(f"unsafe filename: {name!r}")
    if len(candidate) > MAX_FILENAME_LEN:
        raise FileError(f"filename too long: {len(candidate)} > {MAX_FILENAME_LEN}")
    if not _ALLOWED.match(candidate):
        raise FileError(f"illegal characters in filename: {name!r}")
    return candidate
