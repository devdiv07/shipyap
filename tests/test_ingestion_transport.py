import _socket
import http.client
import socket
import ssl
import time
from contextlib import ExitStack, contextmanager, nullcontext

import pytest
from github_fake import OWNER, REPO, REVISION, request_bytes

from shipyap.ingestion import transport
from shipyap.ingestion.errors import IngestErrorCode, IngestionError
from shipyap.ingestion.github import DEFAULT_LIMITS, GithubReader, Limits
from shipyap.ingestion.request import parse_request
from shipyap.ingestion.transport import (
    API_HOST,
    API_PORT,
    API_VERSION,
    USER_AGENT,
    HttpsTransport,
)

COMMIT_PATH = f"/repos/{OWNER}/{REPO}/git/commits/{REVISION}"
LONG_AWAY = 10**9  # a deadline that never triggers


class FakeResponse:
    def __init__(self, status=200, headers=(), body=b"", on_read=None, max_chunk=None):
        self.status = status
        self._headers = list(headers)
        self._body = body
        self._on_read = on_read
        self._max_chunk = max_chunk  # a server that drips: never more than this per read
        self.read_sizes: list[int] = []
        self.closed = False

    def getheaders(self):
        return self._headers

    def read(self, amt=None):
        self.read_sizes.append(amt)
        if self._on_read is not None:
            self._on_read()
        size = amt if self._max_chunk is None else min(amt, self._max_chunk)
        chunk, self._body = self._body[:size], self._body[size:]
        return chunk

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, factory, host, port, timeout, context, deadline):
        self.factory = factory
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.deadline = deadline
        self.requests = []
        self.closed = False

    def request(self, method, url, headers=None):
        if self.factory.request_error is not None:
            raise self.factory.request_error
        self.requests.append((method, url, dict(headers or {})))

    def getresponse(self):
        if self.factory.response_error is not None:
            raise self.factory.response_error
        return self.factory.response

    def close(self):
        self.closed = True


class ConnectionFactory:
    def __init__(self, response=None, request_error=None, response_error=None):
        self.response = response
        self.request_error = request_error
        self.response_error = response_error
        self.connections: list[FakeConnection] = []

    def __call__(self, host, port, *, timeout, context, deadline):
        connection = FakeConnection(self, host, port, timeout, context, deadline)
        self.connections.append(connection)
        return connection


def transport_for(factory, clock=lambda: 0.0, limits=DEFAULT_LIMITS):
    return HttpsTransport(limits, clock=clock, connection_factory=factory)


def get(factory, max_bytes=1024, deadline=LONG_AWAY, clock=lambda: 0.0, path=COMMIT_PATH):
    return transport_for(factory, clock).get(path, "application/test", max_bytes, deadline)


def error_of(factory, **kwargs) -> IngestionError:
    with pytest.raises(IngestionError) as caught:
        get(factory, **kwargs)
    assert caught.value.__cause__ is None
    return caught.value


# --- what a request looks like ---------------------------------------------------------------


def test_request_targets_only_the_fixed_host_with_the_exact_header_set(monkeypatch):
    for name in ("GITHUB_TOKEN", "GH_TOKEN", "HTTPS_PROXY", "https_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "SENTINEL-env-value")
    factory = ConnectionFactory(FakeResponse(200, [("Content-Length", "2")], b"{}"))
    response = get(factory)
    (connection,) = factory.connections
    assert (connection.host, connection.port) == (API_HOST, API_PORT) == ("api.github.com", 443)
    assert connection.timeout == DEFAULT_LIMITS.socket_timeout_seconds
    assert connection.closed
    ((method, url, headers),) = connection.requests
    assert (method, url) == ("GET", COMMIT_PATH)
    assert headers == {
        "User-Agent": USER_AGENT,
        "Accept": "application/test",
        "X-GitHub-Api-Version": API_VERSION,
        "Accept-Encoding": "identity",
    }
    assert "SENTINEL" not in repr(connection.requests)
    context = connection.context
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert (response.status, response.body) == (200, b"{}")


@pytest.mark.parametrize(
    "path",
    [
        "https://evil.example/repos/o/r/git/commits/" + REVISION,
        "//evil.example" + COMMIT_PATH,
        COMMIT_PATH + "?token=x",
        COMMIT_PATH + "#frag",
        COMMIT_PATH + "/extra",
        COMMIT_PATH[:-1],  # 39 hex digits
        COMMIT_PATH.upper(),
        f"/repos/{OWNER}/{REPO}/contents/README.md",
        f"/repos/{OWNER}/../{REPO}/git/commits/{REVISION}",
        COMMIT_PATH + "\r\nHost: evil.example",
        "",
    ],
    ids=[
        "absolute-url",
        "network-path",
        "query",
        "fragment",
        "extra-segment",
        "short-sha",
        "uppercase",
        "contents-api",
        "dotdot",
        "header-injection",
        "empty",
    ],
)
def test_unsafe_request_paths_never_reach_a_connection(path):
    factory = ConnectionFactory(FakeResponse())
    assert error_of(factory, path=path).code == IngestErrorCode.REQUEST_INVALID
    assert factory.connections == []


# --- redirects --------------------------------------------------------------------------------


def test_a_redirect_is_returned_not_followed_and_the_target_is_never_contacted():
    redirect = FakeResponse(
        302, [("Location", "https://evil.example/steal"), ("Set-Cookie", "a=b")], b"moved"
    )
    factory = ConnectionFactory(redirect)
    response = get(factory)
    assert response.status == 302 and "location" not in response.headers
    assert len(factory.connections) == 1 and factory.connections[0].host == API_HOST

    reader = GithubReader(transport_for(factory), DEFAULT_LIMITS)
    with pytest.raises(IngestionError) as caught:
        reader.fetch(parse_request(request_bytes(["README.md"])))
    assert caught.value.code == IngestErrorCode.REDIRECT_REFUSED
    assert {c.host for c in factory.connections} == {API_HOST}  # still only the fixed host


# --- network failures -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        pytest.param(
            {"response_error": TimeoutError("SENTINEL")}, IngestErrorCode.TIMEOUT, id="timeout"
        ),
        pytest.param(
            {"response_error": ssl.SSLError("The read operation timed out SENTINEL")},
            IngestErrorCode.TIMEOUT,
            id="tls-read-timeout",
        ),
        pytest.param(
            {"request_error": ssl.SSLWantReadError("SENTINEL")},
            IngestErrorCode.TIMEOUT,
            id="tls-handshake-expired",  # how CPython can report an expired handshake
        ),
        pytest.param(
            {"response_error": ssl.SSLCertVerificationError("SENTINEL")},
            IngestErrorCode.TLS_ERROR,
            id="certificate",
        ),
        pytest.param(
            {"request_error": ConnectionResetError("SENTINEL")},
            IngestErrorCode.NETWORK_ERROR,
            id="reset-on-send",
        ),
        pytest.param(
            {"response_error": http.client.RemoteDisconnected("SENTINEL")},
            IngestErrorCode.NETWORK_ERROR,
            id="disconnect",
        ),
        pytest.param(
            {"response_error": OSError("SENTINEL C:\\Users\\private")},
            IngestErrorCode.NETWORK_ERROR,
            id="os-error",
        ),
    ],
)
def test_network_failures_become_fixed_codes_and_connections_are_closed(kwargs, expected):
    factory = ConnectionFactory(**kwargs)
    error = error_of(factory)
    assert error.code == expected
    assert "SENTINEL" not in str(error) and "SENTINEL" not in repr(error)
    assert all(connection.closed for connection in factory.connections)


def raise_sentinel(*args, **kwargs):
    raise OSError("SENTINEL-private-path C:\\Users\\private")


def test_tls_context_failure_is_a_fixed_code_and_no_connection_is_constructed(monkeypatch):
    # Codex P2-B: this OSError used to escape the transport (CLI exit 1, sentinel in traceback).
    monkeypatch.setattr(transport.ssl, "create_default_context", raise_sentinel)
    factory = ConnectionFactory(FakeResponse())
    error = error_of(factory)
    assert error.code == IngestErrorCode.TLS_ERROR and "SENTINEL" not in repr(error)
    assert factory.connections == []


def test_connection_construction_failure_is_a_fixed_code_and_nothing_is_closed():
    # Closing a connection that was never created would surface as UnboundLocalError instead.
    def factory(*args, **kwargs):
        raise_sentinel()

    error = error_of(factory)
    assert error.code == IngestErrorCode.NETWORK_ERROR and "SENTINEL" not in repr(error)


def test_programmer_errors_in_connection_setup_are_not_disguised_as_network_errors():
    def factory(*args, **kwargs):
        raise TypeError("a bug, not the network")

    with pytest.raises(TypeError):
        get(factory)


class FakeTcpSocket:
    """A TCP socket whose connect costs `cost` simulated seconds and honors its timeout."""

    def __init__(self, network, family, kind, proto):
        self.network, self.timeout, self.closed = network, None, False
        network.sockets.append(self)

    def settimeout(self, value):
        self.timeout = value

    def gettimeout(self):
        return self.timeout

    def connect(self, address):
        self.network.attempts.append((address, self.timeout))
        if self.network.cost > self.timeout:
            self.network.clock.now += self.timeout
            raise TimeoutError("SENTINEL")
        self.network.clock.now += self.network.cost

    def close(self):
        self.closed = True


class FakeNetwork:
    """Stands in for the transport's `socket` module: three resolved addresses, no real I/O."""

    SOCK_STREAM = 1

    def __init__(self, clock, cost):
        self.clock, self.cost = clock, cost
        self.sockets: list[FakeTcpSocket] = []
        self.attempts: list[tuple[tuple[str, int], float]] = []

    def getaddrinfo(self, host, port, type):
        assert (host, port) == (API_HOST, API_PORT)
        return [(2, 1, 6, "", (f"192.0.2.{n}", port)) for n in (1, 2, 3)]

    def socket(self, family, kind, proto):
        return FakeTcpSocket(self, family, kind, proto)


class RecordingContext:
    def __init__(self, error):
        self.error, self.handshakes = error, []
        self.minimum_version = None

    def wrap_socket(self, sock, server_hostname):
        self.handshakes.append((server_hostname, sock.gettimeout()))
        raise self.error


def connect_with(monkeypatch, clock, cost, context):
    network = FakeNetwork(clock, cost)
    monkeypatch.setattr(transport, "socket", network)
    monkeypatch.setattr(transport.ssl, "create_default_context", lambda: context)
    with pytest.raises(IngestionError) as caught:
        transport_for(transport._DeadlineConnection, clock).get(COMMIT_PATH, "a/b", 10, DEADLINE)
    assert caught.value.__cause__ is None and "SENTINEL" not in str(caught.value)
    return caught.value, network


def test_tcp_connect_attempts_never_run_past_the_deadline(monkeypatch):
    # 45 s already spent; every address hangs for 12 s. Attempt 1 gets 10 s (idle), attempt 2 the
    # 5 s left, attempt 3 is never started.
    clock = Clock(45.0)
    error, network = connect_with(monkeypatch, clock, 12.0, RecordingContext(AssertionError()))
    assert error.code == IngestErrorCode.TIMEOUT and clock.now == DEADLINE
    assert [timeout for _, timeout in network.attempts] == [IDLE, 5.0]
    assert len(network.sockets) == 2 and all(sock.closed for sock in network.sockets)


def test_the_tls_handshake_is_armed_with_the_time_left(monkeypatch):
    # Connected at 52 s: the whole handshake may take at most the 8 s that are left.
    clock = Clock(50.0)
    context = RecordingContext(ssl.SSLWantReadError("SENTINEL"))
    error, network = connect_with(monkeypatch, clock, 2.0, context)
    assert error.code == IngestErrorCode.TIMEOUT
    assert context.handshakes == [(API_HOST, DEADLINE - 52.0)]
    assert len(network.sockets) == 1 and network.sockets[0].closed


def test_a_slow_drip_is_stopped_by_the_overall_deadline():
    now = [0.0]

    def tick():
        now[0] += 1.0

    response = FakeResponse(200, [], b"x" * 1000, on_read=tick, max_chunk=1)
    factory = ConnectionFactory(response)
    error = error_of(factory, deadline=5.0, clock=lambda: now[0], max_bytes=10**6)
    assert error.code == IngestErrorCode.TIMEOUT
    assert len(response.read_sizes) <= 6  # it did not keep reading after the deadline


# --- the overall deadline through the real http.client stack ---------------------------------
# Only the bottom socket is fake. The production connection class, its deadline-bound socket,
# io.BufferedReader and http.client.HTTPResponse (status line, header and body parsing) are real.

DEADLINE = DEFAULT_LIMITS.deadline_seconds  # 60 s, measured on the injected monotonic clock
IDLE = DEFAULT_LIMITS.socket_timeout_seconds  # 10 s


class Clock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


class DripSocket:
    """Delivers the scripted reply. From byte `slow_from` on, every receive delivers one byte and
    costs `step` simulated seconds. Like a real socket, a receive that would take longer than the
    timeout currently set on it fails with TimeoutError after exactly that timeout."""

    def __init__(self, clock, reply, step=0.0, slow_from=0):
        self.clock, self.reply, self.step, self.slow_from = clock, reply, step, slow_from
        self.position = 0
        self.timeouts: list[float] = []
        self.sent = bytearray()
        self.closed = False
        self.close_calls = 0

    def settimeout(self, value):
        self.timeouts.append(value)

    def send(self, data):
        if self.closed:
            raise OSError("SENTINEL closed")
        self.sent += data
        return len(data)

    def recv_into(self, buffer):
        if self.closed:  # like a real descriptor: unusable once physically closed
            raise OSError("SENTINEL closed")
        if self.position >= len(self.reply):
            return 0
        slow = self.position >= self.slow_from and self.step > 0
        if slow and self.step > self.timeouts[-1]:
            self.clock.now += self.timeouts[-1]
            raise TimeoutError("SENTINEL timed out")
        fast_left = self.slow_from - self.position if self.step > 0 else len(self.reply)
        size = 1 if slow else min(len(buffer), fast_left)
        if slow:
            self.clock.now += self.step
        data = self.reply[self.position : self.position + size]
        buffer[: len(data)] = data
        self.position += len(data)
        return len(data)

    def close(self):
        self.closed = True
        self.close_calls += 1


def in_memory(sock):
    """Connection factory: the production connection with only TCP + TLS replaced by `sock`."""

    def factory(host, port, *, timeout, context, deadline):
        connection = transport._DeadlineConnection(
            host, port, timeout=timeout, context=context, deadline=deadline
        )

        def connect():
            connection.sock = transport._DeadlineSocket(sock, deadline)

        connection.connect = connect
        return connection

    return factory


def reply(status_line, body=b"", headers=()):
    head = [status_line, f"Content-Length: {len(body)}", *headers, "", ""]
    return "\r\n".join(head).encode("ascii") + body


def drip_error(clock, data, *, slow_headers=True, step=9.0, deadline=DEADLINE):
    body_start = data.index(b"\r\n\r\n") + 4
    sock = DripSocket(clock, data, step, slow_from=0 if slow_headers else body_start)
    with pytest.raises(IngestionError) as caught:
        transport_for(in_memory(sock), clock).get(COMMIT_PATH, "a/b", 10_000, deadline)
    assert caught.value.__cause__ is None and "SENTINEL" not in str(caught.value)
    return caught.value, sock


@pytest.mark.parametrize(
    ("data", "slow_headers"),
    [
        pytest.param(reply("HTTP/1.1 200 OK", b"x" * 100), False, id="200-body"),
        pytest.param(reply("HTTP/1.1 403 Forbidden", b"y" * 100), False, id="403-body"),
        pytest.param(
            reply("HTTP/1.1 302 Found", b"z" * 100, ["Location: https://evil.example/"]),
            False,
            id="302-body",
        ),
        pytest.param(reply("HTTP/1.1 200 OK", b"x" * 100), True, id="200-headers"),
        pytest.param(reply("HTTP/1.1 302 Found", b"z" * 100), True, id="302-headers"),
    ],
)
def test_a_slow_drip_through_the_real_http_client_stops_at_the_overall_deadline(data, slow_headers):
    # Codex P2-A: one byte per receive, 9 s each (never idle for 10 s). The old transport took
    # 900 simulated seconds here and returned the 403 and 302.
    clock = Clock()
    error, sock = drip_error(clock, data, slow_headers=slow_headers)
    assert error.code == IngestErrorCode.TIMEOUT
    assert clock.now == DEADLINE and sock.closed


def test_the_budget_deadline_is_the_one_enforced_inside_a_response():
    clock = Clock()
    sock = DripSocket(clock, reply("HTTP/1.1 200 OK", b"{}" * 50), step=9.0)
    reader = GithubReader(transport_for(in_memory(sock), clock), DEFAULT_LIMITS, clock)
    with pytest.raises(IngestionError) as caught:
        reader.fetch(parse_request(request_bytes(["README.md"])))
    assert caught.value.code == IngestErrorCode.TIMEOUT and clock.now == DEADLINE
    assert sock.closed


def test_idle_timeout_and_overall_deadline_are_distinct():
    data = reply("HTTP/1.1 200 OK", b"x" * 100)
    # Never idle for 10 s, yet stopped at 60 s: each receive is armed with min(idle, time left).
    clock = Clock()
    error, sock = drip_error(clock, data, step=9.0)
    assert error.code == IngestErrorCode.TIMEOUT and clock.now == DEADLINE
    # The request send at 0 s, receives at 0, 9, ... 45 s armed with 10 s, the one at 54 s with 6 s.
    assert sock.timeouts == [IDLE] * 7 + [DEADLINE - 54.0]
    # Idle for 11 s with the overall deadline far away: the idle timeout alone fires, at 10 s.
    clock = Clock()
    error, sock = drip_error(clock, data, step=11.0, deadline=LONG_AWAY)
    assert error.code == IngestErrorCode.TIMEOUT and clock.now == IDLE
    assert sock.timeouts == [IDLE, IDLE]  # the request send, then the first receive


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(reply("HTTP/1.1 200 OK", b'{"a": 1}'), id="content-length"),
        pytest.param(
            b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\n{"a\r\n5\r\n": 1}\r\n0\r\n\r\n',
            id="chunked",
        ),
        pytest.param(b'HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n{"a": 1}', id="until-close"),
    ],
)
def test_ordinary_fast_responses_work_through_the_real_http_client(data):
    clock = Clock(1000.0)
    sock = DripSocket(clock, data)
    response = transport_for(in_memory(sock), clock).get(
        COMMIT_PATH, "application/test", 1024, 1000.0 + DEADLINE
    )
    assert (response.status, response.body) == (200, b'{"a": 1}')
    assert sock.closed and clock.now == 1000.0
    request_head = bytes(sock.sent).split(b"\r\n")
    assert request_head[0] == f"GET {COMMIT_PATH} HTTP/1.1".encode()
    assert b"Host: api.github.com" in request_head
    assert set(sock.timeouts) == {IDLE}


@pytest.mark.parametrize(("status", "expected"), [(404, 404), (302, 302), (403, 403)])
def test_fast_non_success_replies_are_returned_with_a_capped_body(status, expected):
    clock = Clock()
    sock = DripSocket(clock, reply(f"HTTP/1.1 {status} X", b"e" * 20_000))
    response = transport_for(in_memory(sock), clock).get(COMMIT_PATH, "a/b", 1024, DEADLINE)
    assert response.status == expected and len(response.body) == 16 * 1024


def test_a_streamed_body_over_the_cap_is_refused_through_the_real_http_client():
    data = b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n" + b"x" * 300_000
    sock = DripSocket(Clock(), data)
    with pytest.raises(IngestionError) as caught:
        transport_for(in_memory(sock)).get(COMMIT_PATH, "a/b", 100_000, DEADLINE)
    assert caught.value.code == IngestErrorCode.RESPONSE_TOO_LARGE
    assert sock.position <= 100_001 + 64 * 1024 + 8192
    assert sock.close_calls == 1


# --- socket ownership with real descriptors (Codex R2 P2) ---------------------------------------
# A connected local `socket.socketpair()`: once physically closed, the descriptor is unusable
# (`fileno() == -1`, receives fail). http.client hands the socket to a response that will close;
# the transport must keep it usable until that reader is done, then release it exactly once.

BIG = 20_000  # larger than io.BufferedReader's 8 KiB buffer, so reads continue after handover


class CountingSocket:
    """A real connected socket that also counts physical close calls."""

    def __init__(self, sock):
        self.sock, self.close_calls = sock, 0

    def settimeout(self, value):
        self.sock.settimeout(value)

    def send(self, data):
        return self.sock.send(data)

    def recv_into(self, buffer):
        return self.sock.recv_into(buffer)

    def close(self):
        self.close_calls += 1
        self.sock.close()

    @property
    def released(self):
        return self.sock.fileno() == -1


@contextmanager
def loopback_pair():
    """A connected loopback TCP pair, which is what `socket.socketpair()` builds on Windows. The
    autouse offline guard patches `socket.socket.connect`; this one connection to the test's own
    127.0.0.1 listener deliberately uses the C-level method. Name lookups and every other connect
    stay blocked. Each socket is registered for closing as soon as it exists, so a failure at any
    step (bind, listen, connect, accept or the test body) closes everything opened so far."""
    with ExitStack() as opened:
        listener = opened.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        address = listener.getsockname()
        assert address[0] == "127.0.0.1"
        client = opened.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
        _socket.socket.connect(client, address)
        peer = opened.enter_context(listener.accept()[0])
        listener.close()
        yield CountingSocket(client), peer


@pytest.fixture
def socket_pair():
    with loopback_pair() as pair:
        yield pair


def assert_offline_guard_active():
    """The autouse `no_network` guard is still installed. The arguments are invalid on purpose, so
    nothing could reach the network even if the guard were missing."""
    for attempt in (
        lambda: socket.socket.connect(None, None),
        lambda: socket.getaddrinfo(None, None),
        lambda: socket.create_connection(None),
    ):
        with pytest.raises(AssertionError, match="network access attempted"):
            attempt()


def test_the_loopback_pair_closes_every_socket_it_opened(monkeypatch):
    # Codex R3 Gate 1: a failed `accept()` used to leave the listener and the client open.
    created: list[socket.socket] = []
    stage = ""

    class Tracked(socket.socket):  # real sockets; one setup step fails on request
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

        def bind(self, address):
            if stage == "bind":
                raise OSError("injected bind failure")
            super().bind(address)

        def listen(self, backlog):
            if stage == "listen":
                raise OSError("injected listen failure")
            super().listen(backlog)

        def getsockname(self):
            name = super().getsockname()
            return ("127.0.0.1", 0) if stage == "connect" else name  # refused at once

        def accept(self):
            if stage == "accept":
                raise OSError("injected accept failure")
            return super().accept()

    with monkeypatch.context() as patch:
        patch.setattr(socket, "socket", Tracked)  # `accept()` builds its socket from this too
        for stage, opened_before_failure in (
            ("bind", 1),
            ("listen", 1),
            ("connect", 2),
            ("accept", 2),
        ):
            created.clear()
            with pytest.raises(OSError), loopback_pair():
                pass
            assert len(created) == opened_before_failure, stage
            assert all(sock.fileno() == -1 for sock in created), stage

        stage = ""
        for body_fails in (False, True):
            created.clear()
            with pytest.raises(RuntimeError) if body_fails else nullcontext():
                with loopback_pair() as (client, peer):
                    assert client.sock.fileno() != -1 and peer.fileno() != -1
                    if body_fails:
                        raise RuntimeError("test body failed")
            assert len(created) == 3  # listener, client and the accepted peer
            assert all(sock.fileno() == -1 for sock in created)
    assert_offline_guard_active()


def serve(peer, data, *, end_stream=False):
    peer.sendall(data)
    if end_stream:
        peer.shutdown(socket.SHUT_WR)  # the client sees EOF after `data`


@pytest.mark.parametrize(
    ("data", "end_stream"),
    [
        pytest.param(
            reply("HTTP/1.1 200 OK", b"c" * BIG, ["Connection: close"]), False, id="http11-close"
        ),
        pytest.param(reply("HTTP/1.0 200 OK", b"z" * BIG), False, id="http10-length"),
        pytest.param(
            b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n" + b"e" * BIG, True, id="until-eof"
        ),
        pytest.param(reply("HTTP/1.1 200 OK", b"k" * BIG), False, id="keep-alive"),
    ],
)
def test_real_socket_responses_are_read_fully_and_the_socket_is_released_once(
    socket_pair, data, end_stream
):
    client, peer = socket_pair
    serve(peer, data, end_stream=end_stream)
    response = transport_for(in_memory(client)).get(COMMIT_PATH, "a/b", BIG, LONG_AWAY)
    assert (response.status, len(response.body)) == (200, BIG)
    assert response.body == data[-BIG:]
    assert client.released and client.close_calls == 1


def owned_reader(client):
    """The connection's socket and one response reader on it, as http.client creates them."""
    deadline = transport._Deadline(LONG_AWAY, IDLE, lambda: 0.0)
    connection_socket = transport._DeadlineSocket(client, deadline)
    return connection_socket, connection_socket.makefile("rb")


def test_closing_the_connection_keeps_the_socket_usable_until_the_reader_closes(socket_pair):
    client, peer = socket_pair
    serve(peer, b"a" * BIG)
    connection_socket, reader = owned_reader(client)
    assert reader.read(10) == b"a" * 10
    connection_socket.close()  # what getresponse() does for a response that will close
    assert not client.released and client.close_calls == 0
    assert reader.read(BIG - 10) == b"a" * (BIG - 10)  # receives after the handover still work
    reader.close()
    assert client.released and client.close_calls == 1


def test_a_reader_closed_first_leaves_closure_to_the_connection(socket_pair):
    client, peer = socket_pair
    serve(peer, b"a" * 10)
    connection_socket, reader = owned_reader(client)
    assert reader.read(10) == b"a" * 10
    reader.close()
    assert not client.released  # the connection has not asked for closure
    connection_socket.close()
    assert client.released and client.close_calls == 1


def test_repeated_closes_close_the_socket_exactly_once(socket_pair):
    client, _ = socket_pair
    connection_socket, reader = owned_reader(client)
    for _ in range(3):
        connection_socket.close()
        reader.close()
    connection_socket.close()
    assert client.released and client.close_calls == 1
    with pytest.raises(ValueError):
        reader.read(1)  # a closed reader cannot reach the socket
    with pytest.raises(OSError):
        connection_socket.makefile("rb")  # nor can a new one


def test_a_failed_reader_construction_does_not_keep_the_socket_open(socket_pair, monkeypatch):
    client, _ = socket_pair
    deadline = transport._Deadline(LONG_AWAY, IDLE, lambda: 0.0)
    connection_socket = transport._DeadlineSocket(client, deadline)

    def broken(raw):
        raise MemoryError

    # Scoped: undoing the shared `monkeypatch` would also remove the autouse offline guard.
    with monkeypatch.context() as patch:
        patch.setattr(transport.io, "BufferedReader", broken)
        with pytest.raises(MemoryError):
            connection_socket.makefile("rb")
    assert transport.io.BufferedReader is not broken
    assert_offline_guard_active()
    connection_socket.close()
    assert client.released and client.close_calls == 1


def test_an_expired_deadline_on_a_handed_over_reader_still_releases_the_socket(socket_pair):
    # `Connection: close`: the response owns the socket. The deadline expires mid-body; the
    # transport must close the response itself, since the connection no longer references it.
    client, peer = socket_pair
    serve(peer, reply("HTTP/1.1 200 OK", b"d" * BIG, ["Connection: close"]))
    clock = Clock()
    readings = []

    def ticking():  # free for the request and the first receive, then 25 s per reading
        readings.append(clock.now)
        clock.now += 25.0 if len(readings) > 2 else 0.0
        return clock.now

    with pytest.raises(IngestionError) as caught:
        transport_for(in_memory(client), ticking).get(COMMIT_PATH, "a/b", BIG, DEADLINE)
    assert caught.value.code == IngestErrorCode.TIMEOUT
    assert client.released and client.close_calls == 1


def test_a_real_idle_timeout_on_a_handed_over_reader_still_releases_the_socket(socket_pair):
    # The server announces 20 kB, sends 100 bytes and stalls: a real 0.2 s socket timeout fires.
    client, peer = socket_pair
    serve(peer, reply("HTTP/1.1 200 OK", b"d" * BIG, ["Connection: close"])[: -(BIG - 100)])
    limits = Limits(socket_timeout_seconds=0.2)
    with pytest.raises(IngestionError) as caught:
        HttpsTransport(limits, connection_factory=in_memory(client)).get(
            COMMIT_PATH, "a/b", BIG, time.monotonic() + DEADLINE
        )
    assert caught.value.code == IngestErrorCode.TIMEOUT
    assert client.released and client.close_calls == 1


# --- byte caps and body integrity -------------------------------------------------------------


def test_declared_length_over_the_cap_is_refused_before_reading():
    response = FakeResponse(200, [("Content-Length", "2000")], b"x" * 2000)
    error = error_of(ConnectionFactory(response), max_bytes=1000)
    assert error.code == IngestErrorCode.RESPONSE_TOO_LARGE and response.read_sizes == []


def test_streamed_body_over_the_cap_stops_one_byte_past_it():
    response = FakeResponse(200, [], b"x" * 500_000)
    error = error_of(ConnectionFactory(response), max_bytes=100_000)
    assert error.code == IngestErrorCode.RESPONSE_TOO_LARGE
    assert sum(size for size in response.read_sizes) <= 100_001 + 65_536
    assert max(response.read_sizes) <= 65_536


@pytest.mark.parametrize("size", [0, 1, 1000])
def test_body_exactly_at_the_cap_is_accepted(size):
    response = FakeResponse(200, [("Content-Length", str(size))], b"x" * size)
    assert get(ConnectionFactory(response), max_bytes=size).body == b"x" * size


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param([("Content-Encoding", "gzip")], id="gzip"),
        pytest.param([("Content-Length", "10")], id="truncated-body"),
        pytest.param([("Content-Length", "ten")], id="bad-length"),
        pytest.param([("Content-Length", "-1")], id="negative-length"),
    ],
)
def test_responses_that_do_not_match_their_own_headers_are_rejected(headers):
    response = FakeResponse(200, headers, b"abcd")
    assert error_of(ConnectionFactory(response)).code == IngestErrorCode.UNEXPECTED_RESPONSE


def test_error_bodies_are_capped_and_headers_are_reduced_to_a_reviewed_subset():
    response = FakeResponse(
        403,
        [("Retry-After", "5"), ("Set-Cookie", "SENTINEL"), ("X-RateLimit-Remaining", "0")],
        b"y" * 100_000,
    )
    result = get(ConnectionFactory(response))
    assert result.status == 403 and len(result.body) == 16 * 1024
    assert dict(result.headers) == {"retry-after": "5", "x-ratelimit-remaining": "0"}


def test_limits_are_code_constants_not_request_fields():
    assert Limits() == DEFAULT_LIMITS
    assert not any(
        "limit" in name for name in type(parse_request(request_bytes(["a"]))).model_fields
    )
