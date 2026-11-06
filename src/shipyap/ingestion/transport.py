"""The ONLY module that opens network connections (a test enforces that nothing else may).

Containment, by construction:
- One fixed host (`api.github.com`) and port; neither is a parameter. The request path is
  checked against a strict pattern (Git Data ids, HEAD, pinned comparisons and recursive trees)
  before sending,
  so no response field and no request value can steer a request elsewhere.
- No redirect is ever followed: any 3xx is returned as-is and refused by the caller. The
  standard-library connection used here does not read proxy environment variables.
- Certificates and host names are verified (`ssl.create_default_context`, TLS 1.2 or newer).
- No credentials: no `Authorization` header is ever sent and no environment variable is read.
- Bounded: the overall deadline is enforced on EVERY blocking socket operation (TCP connect, TLS
  handshake, each send and each receive; see `_Deadline`), with an idle timeout per operation;
  a byte cap enforced while streaming (reading stops one byte past the cap), a `Content-Length`
  pre-check, and `Accept-Encoding: identity` so a compressed response is never inflated.
  NOT bounded by the deadline: host name resolution (`getaddrinfo` cannot be interrupted from
  here; the deadline is checked as soon as it returns).
- Errors carry fixed codes only; response bodies and OS error text are never surfaced. TLS
  context creation and connection construction are inside the same boundary.
"""

import http.client
import io
import re
import socket
import ssl
import time
from collections.abc import Callable
from typing import Any

from shipyap.ingestion.errors import IngestErrorCode, IngestionError
from shipyap.ingestion.github import API_VERSION, Limits, TransportResponse

API_HOST = "api.github.com"
API_PORT = 443
USER_AGENT = "public-research-os-ingestion/0.1.2"

_API_PATH = re.compile(
    r"/repos/[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}(?:"
    r"/git/(?:commits|trees|blobs)/[0-9a-f]{40}"
    r"|/git/trees/[0-9a-f]{40}\?recursive=1"
    r"|/commits/HEAD"
    r"|/compare/[0-9a-f]{40}\.\.\.[0-9a-f]{40}\?per_page=1)"
)
_KEPT_HEADERS = frozenset(
    {"content-length", "content-encoding", "retry-after", "x-ratelimit-remaining"}
)
_ERROR_BODY_CAP = 16 * 1024  # only ever used to classify a non-200 reply
_CHUNK_BYTES = 64 * 1024


class _Deadline:
    """The overall deadline, applied to every blocking socket operation, not between reads.

    Before each connect attempt, TLS handshake, send and receive, the socket timeout is set to
    the idle timeout or the time left, whichever is smaller; once no time is left the next
    operation is refused. So no single operation can outlive the deadline, however http.client
    and `io.BufferedReader` split a status line, a header or a `read(n)` into receives. CPython
    bounds one connect, one handshake and one TLS read or write as a whole by the socket timeout
    (not per TCP segment); a loopback drip probe confirmed this on Windows (docs/working-log.md).
    """

    def __init__(self, deadline: float, idle_seconds: float, clock: Callable[[], float]) -> None:
        self._deadline = deadline
        self._idle_seconds = idle_seconds
        self._clock = clock

    def timeout(self) -> float:
        remaining = self._deadline - self._clock()
        if remaining <= 0:
            raise TimeoutError
        return min(self._idle_seconds, remaining)


class _DeadlineReader(io.RawIOBase):
    """The raw stream under the response's buffer: every receive is re-armed first.

    Like `socket.SocketIO`, it holds a reference on its socket from construction until it is
    closed (explicitly, by http.client at the end of the body, or when garbage collected).
    """

    def __init__(self, owner: "_DeadlineSocket") -> None:
        super().__init__()
        self._owner: _DeadlineSocket | None = owner
        owner._acquire()

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        if self._owner is None:
            raise ValueError("I/O operation on closed reader")
        return self._owner._recv_into(buffer)

    def close(self) -> None:
        owner, self._owner = self._owner, None
        try:
            super().close()
        finally:
            if owner is not None:
                owner._release()


class _DeadlineSocket:
    """All that http.client uses of a socket (`sendall`, `makefile`, `close`), deadline-bound.

    Ownership follows `socket.socket.makefile`: `HTTPConnection.getresponse` closes the connection
    and hands the socket to a response that will close (`Connection: close`, HTTP/1.0, read until
    EOF), so `close()` only requests closure; the socket is physically closed, exactly once, when
    closure was requested and no response reader holds it any more.
    """

    def __init__(self, sock: Any, deadline: _Deadline) -> None:
        self._sock = sock
        self._deadline = deadline
        self._readers = 0
        self._close_requested = False
        self._closed = False

    def sendall(self, data: bytes) -> None:
        with memoryview(data) as view:
            sent = 0
            while sent < len(view):
                self._sock.settimeout(self._deadline.timeout())
                sent += self._sock.send(view[sent:])

    def makefile(self, mode: str) -> io.BufferedReader:
        if mode != "rb":  # http.client only ever reads a response through this
            raise ValueError("unsupported mode")
        if self._closed:
            raise OSError("socket is closed")
        raw = _DeadlineReader(self)
        try:
            return io.BufferedReader(raw)
        except BaseException:
            raw.close()  # gives the reference back
            raise

    def close(self) -> None:
        self._close_requested = True
        self._close_if_unowned()

    def _recv_into(self, buffer: Any) -> int:
        self._sock.settimeout(self._deadline.timeout())
        return self._sock.recv_into(buffer)

    def _acquire(self) -> None:
        self._readers += 1

    def _release(self) -> None:
        self._readers -= 1
        self._close_if_unowned()

    def _close_if_unowned(self) -> None:
        if self._close_requested and self._readers == 0 and not self._closed:
            self._closed = True
            self._sock.close()


class _DeadlineConnection(http.client.HTTPConnection):
    """HTTPS to the fixed host; TCP connect and TLS handshake are bounded by the deadline too."""

    default_port = API_PORT  # so the Host header stays `api.github.com`, as with HTTPSConnection

    def __init__(
        self, host: str, port: int, *, timeout: float, context: Any, deadline: _Deadline
    ) -> None:
        super().__init__(host, port, timeout=timeout)
        self._tls_context = context
        self._deadline = deadline

    def connect(self) -> None:
        raw = _open_tcp(self.host, self.port, self._deadline)
        try:
            raw.settimeout(self._deadline.timeout())  # bounds the whole handshake
            tls = self._tls_context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise
        self.sock = _DeadlineSocket(tls, self._deadline)


def _open_tcp(host: str, port: int, deadline: _Deadline) -> Any:
    # Name resolution cannot be interrupted from here; the deadline is checked once it returns.
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    failure = OSError("no address")
    for family, kind, proto, _, address in addresses:
        timeout = deadline.timeout()  # no attempt starts after the deadline
        sock = socket.socket(family, kind, proto)
        try:
            sock.settimeout(timeout)
            sock.connect(address)
        except OSError as exc:
            sock.close()
            failure = exc
            continue
        return sock
    raise failure


class HttpsTransport:
    def __init__(
        self,
        limits: Limits,
        clock: Callable[[], float] = time.monotonic,
        connection_factory: Callable[..., Any] = _DeadlineConnection,
    ) -> None:
        self._limits = limits
        self._clock = clock
        self._connection_factory = connection_factory

    def get(self, path: str, accept: str, max_bytes: int, deadline: float) -> TransportResponse:
        if _API_PATH.fullmatch(path) is None:  # defence in depth; callers build valid paths
            raise IngestionError(IngestErrorCode.REQUEST_INVALID)
        try:
            context = ssl.create_default_context()
        except OSError:  # includes ssl.SSLError: the local CA store or TLS library failed
            raise IngestionError(IngestErrorCode.TLS_ERROR) from None
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        bound = _Deadline(deadline, self._limits.socket_timeout_seconds, self._clock)
        try:
            connection = self._connection_factory(
                API_HOST,
                API_PORT,
                timeout=self._limits.socket_timeout_seconds,
                context=context,
                deadline=bound,
            )
        except (OSError, http.client.HTTPException) as exc:
            raise _network_error(exc) from None
        try:
            connection.request("GET", path, headers=_request_headers(accept))
            response = connection.getresponse()
            try:
                return self._read(response, max_bytes, deadline)
            finally:
                # A response that will close owns the socket and is not closed by the connection.
                _close_quietly(response)
        except IngestionError:
            raise
        except (OSError, http.client.HTTPException) as exc:
            raise _network_error(exc) from None
        finally:
            _close_quietly(connection)

    def _read(self, response: Any, max_bytes: int, deadline: float) -> TransportResponse:
        status = int(response.status)
        headers = {
            name.lower(): value
            for name, value in response.getheaders()
            if name.lower() in _KEPT_HEADERS
        }
        if status != 200:
            return TransportResponse(status, headers, response.read(_ERROR_BODY_CAP))

        if headers.get("content-encoding", "identity").strip().lower() not in {"", "identity"}:
            raise IngestionError(IngestErrorCode.UNEXPECTED_RESPONSE)
        declared = _declared_length(headers.get("content-length"))
        if declared is not None and declared > max_bytes:
            raise IngestionError(IngestErrorCode.RESPONSE_TOO_LARGE)

        chunks: list[bytes] = []
        total = 0
        while True:
            # Not the deadline's enforcement (that is per receive, in `_Deadline`); this only
            # stops a response object that is not socket-backed from being read on.
            if self._clock() >= deadline:
                raise IngestionError(IngestErrorCode.TIMEOUT)
            chunk = response.read(min(_CHUNK_BYTES, max_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise IngestionError(IngestErrorCode.RESPONSE_TOO_LARGE)
            chunks.append(chunk)
        # http.client does not raise on a body that ends before Content-Length.
        if declared is not None and total != declared:
            raise IngestionError(IngestErrorCode.UNEXPECTED_RESPONSE)
        return TransportResponse(status, headers, b"".join(chunks))


def _network_error(exc: OSError | http.client.HTTPException) -> IngestionError:
    """A fixed code; the exception and its text are dropped."""
    # An expired TLS operation surfaces as TimeoutError or, on CPython's last non-blocking retry,
    # as SSLWantReadError / SSLWantWriteError (seen in the handshake drip probe).
    if isinstance(exc, TimeoutError | ssl.SSLWantReadError | ssl.SSLWantWriteError):
        return IngestionError(IngestErrorCode.TIMEOUT)
    if isinstance(exc, ssl.SSLError):
        timed_out = "timed out" in str(exc).lower()
        return IngestionError(IngestErrorCode.TIMEOUT if timed_out else IngestErrorCode.TLS_ERROR)
    return IngestionError(IngestErrorCode.NETWORK_ERROR)


def _request_headers(accept: str) -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept": accept,
        "X-GitHub-Api-Version": API_VERSION,
        "Accept-Encoding": "identity",
    }


def _declared_length(value: str | None) -> int | None:
    if value is None:
        return None
    if not (value.isascii() and value.isdigit()) or len(value) > 12:
        raise IngestionError(IngestErrorCode.UNEXPECTED_RESPONSE)
    return int(value)


def _close_quietly(closeable: Any) -> None:
    try:
        closeable.close()
    except OSError:
        pass
