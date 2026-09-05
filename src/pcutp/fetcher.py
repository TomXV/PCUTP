"""HTTP(S) fetching for the uConsole side (sections 7, 23, 24)."""

import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .const import ALLOWED_SCHEMES, HTTP_TIMEOUT, MAX_FILE_SIZE, MAX_REDIRECTS
from .errors import DnsError, HttpError, SizeError, UrlError


@dataclass
class Fetched:
    data: bytes
    url: str


def validate_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise UrlError(f"scheme not allowed: {parsed.scheme!r}")
    if not parsed.netloc:
        raise UrlError("missing host")
    return url


class _LimitedRedirects(urllib.request.HTTPRedirectHandler):
    max_repeats = MAX_REDIRECTS
    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)  # never follow a redirect off http/https
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str, max_size: int = MAX_FILE_SIZE, timeout: float = HTTP_TIMEOUT) -> Fetched:
    """Download `url` into memory, enforcing the scheme and size limits."""
    validate_url(url)
    opener = urllib.request.build_opener(_LimitedRedirects())
    request = urllib.request.Request(url, headers={"User-Agent": "pcutpd/0.1"})
    try:
        with opener.open(request, timeout=timeout) as response:
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > max_size:
                raise SizeError(f"Content-Length {declared} exceeds {max_size}")
            data = response.read(max_size + 1)
            final_url = response.geturl()
    except urllib.error.HTTPError as exc:
        raise HttpError(f"HTTP {exc.code}", detail=str(exc.code)) from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, socket.gaierror):
            raise DnsError(str(exc.reason)) from exc
        raise HttpError(str(exc.reason)) from exc
    if len(data) > max_size:
        raise SizeError(f"body exceeds {max_size} bytes")
    return Fetched(data=data, url=final_url)
