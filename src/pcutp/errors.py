"""PCUTP error types. `code` mirrors the wire ERR codes of section 22."""


class PcutpError(Exception):
    """Base class. `code` is an ERR token such as "URL" or "SIZE"."""

    code = "PROTOCOL"

    def __init__(self, message: str = "", code: str | None = None, detail: str = ""):
        super().__init__(message or (code or self.code))
        if code is not None:
            self.code = code
        self.detail = detail

    def wire(self) -> str:
        """Render as an ERR control line body, e.g. "ERR HTTP 404"."""
        return f"ERR {self.code}" + (f" {self.detail}" if self.detail else "")


class VersionError(PcutpError):
    code = "VERSION"


class UrlError(PcutpError):
    code = "URL"


class HttpError(PcutpError):
    code = "HTTP"


class DnsError(PcutpError):
    code = "DNS"


class SizeError(PcutpError):
    code = "SIZE"


class StorageError(PcutpError):
    code = "STORAGE"


class FileError(PcutpError):
    code = "FILE"


class WriteError(PcutpError):
    code = "WRITE"


class TimeoutError_(PcutpError):
    code = "TIMEOUT"


class RetryError(PcutpError):
    code = "RETRY"


class ProtocolError(PcutpError):
    code = "PROTOCOL"
