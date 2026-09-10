"""HTTP(S) fetching for the uConsole side (sections 7, 23, 24)."""

import socket
import time
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
    deadline = time.monotonic() + timeout
    try:
        with opener.open(request, timeout=max(0.0, deadline - time.monotonic())) as response:
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > max_size:
                raise SizeError(f"Content-Length {declared} exceeds {max_size}")
            chunks = []
            total = 0
            while total <= max_size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("HTTP fetch deadline exceeded")
                # urllib's timeout is normally applied per socket operation.
                # Tighten the underlying socket for each read so a slow stream
                # cannot outlive the receiver's FETCH_TIMEOUT window.
                try:
                    response.fp.raw._sock.settimeout(remaining)
                except AttributeError:
                    pass
                chunk = response.read(min(65536, max_size + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            data = b"".join(chunks)
            if time.monotonic() > deadline:
                raise TimeoutError("HTTP fetch deadline exceeded")
            final_url = response.geturl()
    except TimeoutError as exc:
        raise HttpError("HTTP fetch timed out", detail="timeout") from exc
    except urllib.error.HTTPError as exc:
        raise HttpError(f"HTTP {exc.code}", detail=str(exc.code)) from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, socket.gaierror):
            raise DnsError(str(exc.reason)) from exc
        raise HttpError(str(exc.reason)) from exc
    if len(data) > max_size:
        raise SizeError(f"body exceeds {max_size} bytes")
    return Fetched(data=data, url=final_url)
