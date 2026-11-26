"""Per-agent HTTP CONNECT proxy enforcing the provider's network allowlist."""

from __future__ import annotations

import ctypes
import select
import socket
import socketserver
import sys
import threading
from collections.abc import Callable, Sequence
from contextlib import suppress
from urllib.parse import urlsplit

# macOS libproc, used to find which process owns the client end of a loopback
# connection. Only the stable struct prefix is mirrored here; the kernel writes
# the full socket_fdinfo into a buffer we size generously.
_PROC_PIDLISTFDS = 1
_PROX_FDTYPE_SOCKET = 2
_PROC_PIDFDSOCKETINFO = 3
_SOCKET_FDINFO_BUFFER = 4096


class _VinfoStat(ctypes.Structure):
    _fields_ = [
        ("vst_dev", ctypes.c_uint32),
        ("vst_mode", ctypes.c_uint16),
        ("vst_nlink", ctypes.c_uint16),
        ("vst_ino", ctypes.c_uint64),
        ("vst_uid", ctypes.c_uint32),
        ("vst_gid", ctypes.c_uint32),
        ("vst_atime", ctypes.c_int64),
        ("vst_atimensec", ctypes.c_int64),
        ("vst_mtime", ctypes.c_int64),
        ("vst_mtimensec", ctypes.c_int64),
        ("vst_ctime", ctypes.c_int64),
        ("vst_ctimensec", ctypes.c_int64),
        ("vst_birthtime", ctypes.c_int64),
        ("vst_birthtimensec", ctypes.c_int64),
        ("vst_size", ctypes.c_int64),
        ("vst_blocks", ctypes.c_int64),
        ("vst_blksize", ctypes.c_int32),
        ("vst_flags", ctypes.c_uint32),
        ("vst_gen", ctypes.c_uint32),
        ("vst_rdev", ctypes.c_uint32),
        ("vst_qspare", ctypes.c_int64 * 2),
    ]


class _ProcFileInfo(ctypes.Structure):
    _fields_ = [
        ("fi_openflags", ctypes.c_uint32),
        ("fi_status", ctypes.c_uint32),
        ("fi_offset", ctypes.c_int64),
        ("fi_type", ctypes.c_int32),
        ("fi_guardflags", ctypes.c_uint32),
    ]


class _In4In6Addr(ctypes.Structure):
    _fields_ = [("i46a_pad32", ctypes.c_uint32 * 3), ("i46a_addr4", ctypes.c_uint32)]


class _In6Addr(ctypes.Structure):
    _fields_ = [("i46a_addr6", ctypes.c_uint8 * 16)]


class _Addr(ctypes.Union):
    _fields_ = [("ina_46", _In4In6Addr), ("ina_6", _In6Addr)]


class _SockbufInfo(ctypes.Structure):
    _fields_ = [
        ("sbi_cc", ctypes.c_uint32),
        ("sbi_hiwat", ctypes.c_uint32),
        ("sbi_mbcnt", ctypes.c_uint32),
        ("sbi_mbmax", ctypes.c_uint32),
        ("sbi_lowat", ctypes.c_uint32),
        ("sbi_flags", ctypes.c_int16),
        ("sbi_timeo", ctypes.c_int16),
    ]


class _In6SockInfo(ctypes.Structure):
    _fields_ = [
        ("in6_hlim", ctypes.c_uint8),
        ("in6_cksum", ctypes.c_int),
        ("in6_ifindex", ctypes.c_uint16),
        ("in6_hops", ctypes.c_int16),
    ]


class _InSockInfo(ctypes.Structure):
    _fields_ = [
        ("insi_fport", ctypes.c_int),
        ("insi_lport", ctypes.c_int),
        ("insi_gencnt", ctypes.c_uint64),
        ("insi_flags", ctypes.c_uint32),
        ("insi_flow", ctypes.c_uint32),
        ("insi_vflag", ctypes.c_uint8),
        ("insi_ip_ttl", ctypes.c_uint8),
        ("rfu_1", ctypes.c_uint32),
        ("insi_faddr", _Addr),
        ("insi_laddr", _Addr),
        ("insi_v4", ctypes.c_uint8),
        ("insi_v6", _In6SockInfo),
    ]


class _SocketInfo(ctypes.Structure):
    # The C union at soi_proto; the inet member shares the offset needed here.
    _fields_ = [
        ("soi_stat", _VinfoStat),
        ("soi_so", ctypes.c_uint64),
        ("soi_pcb", ctypes.c_uint64),
        ("soi_type", ctypes.c_int),
        ("soi_protocol", ctypes.c_int),
        ("soi_family", ctypes.c_int),
        ("soi_options", ctypes.c_int16),
        ("soi_linger", ctypes.c_int16),
        ("soi_state", ctypes.c_int16),
        ("soi_qlen", ctypes.c_int16),
        ("soi_incqlen", ctypes.c_int16),
        ("soi_qlimit", ctypes.c_int16),
        ("soi_timeo", ctypes.c_int16),
        ("soi_error", ctypes.c_uint16),
        ("soi_oobmark", ctypes.c_uint32),
        ("soi_rcv", _SockbufInfo),
        ("soi_snd", _SockbufInfo),
        ("soi_kind", ctypes.c_int),
        ("rfu_1", ctypes.c_uint32),
        ("soi_proto", _InSockInfo),
    ]


class _SocketFdInfo(ctypes.Structure):
    _fields_ = [("pfi", _ProcFileInfo), ("psi", _SocketInfo)]


class _ProcFdInfo(ctypes.Structure):
    _fields_ = [("proc_fd", ctypes.c_int32), ("proc_fdtype", ctypes.c_uint32)]


def _libproc() -> ctypes.CDLL | None:
    if sys.platform != "darwin":
        return None
    try:
        return ctypes.CDLL("/usr/lib/libproc.dylib")
    except (OSError, AttributeError):
        return None


def _all_pids(libproc: ctypes.CDLL) -> list[int]:
    fn = libproc.proc_listallpids
    fn.argtypes = [ctypes.c_void_p, ctypes.c_int]
    fn.restype = ctypes.c_int
    capacity = max(int(fn(None, 0)), 64) + 64
    for _ in range(4):
        pids = (ctypes.c_int * capacity)()
        count = fn(pids, ctypes.sizeof(pids))
        if count <= 0:
            return []
        if count < capacity:
            return [int(pids[index]) for index in range(count) if pids[index] > 0]
        capacity *= 2
    return []


def _socket_fds(libproc: ctypes.CDLL, pid: int) -> list[int]:
    fn = libproc.proc_pidinfo
    fn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    fn.restype = ctypes.c_int
    capacity = 64 * 1024
    for _ in range(2):
        buffer = ctypes.create_string_buffer(capacity)
        size = fn(pid, _PROC_PIDLISTFDS, 0, buffer, capacity)
        if size <= 0:
            return []
        if size <= capacity:
            break
        capacity = size
    else:
        return []
    entry = ctypes.sizeof(_ProcFdInfo)
    return [
        int(_ProcFdInfo.from_buffer_copy(buffer, offset).proc_fd)
        for offset in range(0, size - entry + 1, entry)
        if _ProcFdInfo.from_buffer_copy(buffer, offset).proc_fdtype == _PROX_FDTYPE_SOCKET
    ]


def _owns_port(libproc: ctypes.CDLL, pid: int, port: int) -> bool:
    fn = libproc.proc_pidfdinfo
    fn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    fn.restype = ctypes.c_int
    for fd in _socket_fds(libproc, pid):
        buffer = ctypes.create_string_buffer(_SOCKET_FDINFO_BUFFER)
        size = fn(pid, fd, _PROC_PIDFDSOCKETINFO, buffer, len(buffer))
        if size < ctypes.sizeof(_SocketFdInfo):
            continue
        info = _SocketFdInfo.from_buffer_copy(buffer)
        if info.psi.soi_family != socket.AF_INET:
            continue
        if info.psi.soi_type != socket.SOCK_STREAM:
            continue
        # libproc copies the kernel's network-order port into an int field.
        if socket.ntohs(int(info.psi.soi_proto.insi_lport) & 0xFFFF) != port:
            continue
        local = ctypes.string_at(ctypes.addressof(info.psi.soi_proto.insi_laddr) + 12, 4)
        if local and local[0] == 127:
            return True
    return False


def client_pid(port: int) -> int | None:
    """The pid owning TCP socket 127.0.0.1:<port>, or None when unknown.

    Used as the egress proxy's owner lookup. Off macOS, or on any failure of
    the libproc walk, it returns None so callers fail closed.
    """
    libproc = _libproc()
    if libproc is None:
        return None
    try:
        for pid in _all_pids(libproc):
            if _owns_port(libproc, pid, int(port)):
                return pid
    except Exception:  # noqa: BLE001 - a failed lookup must refuse, not guess
        return None
    return None


def _authority(value: str, default_port: int) -> tuple[str, int]:
    parsed = urlsplit("//" + value)
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host:
        raise ValueError("missing destination host")
    try:
        port = parsed.port or default_port
    except ValueError as exc:
        raise ValueError("invalid destination port") from exc
    if not 1 <= port <= 65535:
        raise ValueError("invalid destination port")
    return host, port


def _rule(value: str) -> tuple[str, int]:
    return _authority(value, 443)


def _matches(host: str, port: int, allow: Sequence[tuple[str, int]]) -> bool:
    host = host.rstrip(".").lower()
    for candidate, candidate_port in allow:
        if port != candidate_port:
            continue
        if candidate.startswith("*."):
            suffix = candidate[1:]
            if host.endswith(suffix) and host != suffix[1:]:
                return True
        elif host == candidate:
            return True
    return False


class EgressProxy:
    """A loopback proxy that forwards only destinations in ``allow``."""

    def __init__(
        self,
        allow: Sequence[str],
        on_refusal: Callable[[str, int], None] | None = None,
        allowed_pids: Callable[[], set[int]] | None = None,
    ):
        self.allow = tuple(_rule(value.strip()) for value in allow)
        self.on_refusal = on_refusal
        self.allowed_pids = allowed_pids
        self._server: socketserver.ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> int:
        if self._server is not None:
            return int(self._server.server_address[1])
        owner = self

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

            def __init__(self):
                super().__init__(("127.0.0.1", 0), Handler)

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                owner._handle(self.request)

        server = Server()
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name="subagent-egress",
            daemon=True,
        )
        self._thread.start()
        return int(self._server.server_address[1])

    def stop(self) -> None:
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=3)

    def _refuse(self, client: socket.socket, host: str, port: int) -> None:
        if self.on_refusal is not None:
            with suppress(Exception):
                self.on_refusal(host, port)
        with suppress(OSError):
            client.sendall(
                b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n"
                b"Content-Length: 0\r\n\r\n"
            )

    def _client_allowed(self, client: socket.socket) -> bool:
        """Whether the accepted connection's client pid is in ``allowed_pids()``.

        The lookup is the module-level ``client_pid`` so a caller or test can
        replace it. Any failure -- no owner, a raising callable, a platform
        without libproc -- refuses; allowed_pids=None keeps the old behaviour.
        """
        if self.allowed_pids is None:
            return True
        try:
            allowed = self.allowed_pids()
            port = int(client.getpeername()[1])
            owner = client_pid(port)
            return owner is not None and owner in allowed
        except Exception:  # noqa: BLE001 - an owner lookup that fails must refuse
            return False

    def _handle(self, client: socket.socket) -> None:
        upstream: socket.socket | None = None
        client.settimeout(15)
        try:
            data = bytearray()
            while b"\r\n\r\n" not in data and len(data) <= 65536:
                chunk = client.recv(4096)
                if not chunk:
                    return
                data.extend(chunk)
            if len(data) > 65536:
                client.sendall(b"HTTP/1.1 431 Request Header Fields Too Large\r\n\r\n")
                return
            boundary = data.find(b"\r\n\r\n") + 4
            header, remainder = bytes(data[:boundary]), bytes(data[boundary:])
            request_line = header.split(b"\r\n", 1)[0].decode("latin-1")
            fields = request_line.split()
            if len(fields) != 3:
                client.sendall(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
                return
            method, target, _version = fields
            if method.upper() == "CONNECT":
                host, port = _authority(target, 443)
                if not self._client_allowed(client):
                    self._refuse(client, host, port)
                    return
                if not _matches(host, port, self.allow):
                    self._refuse(client, host, port)
                    return
                upstream = socket.create_connection((host, port), timeout=15)
                upstream.settimeout(None)
                client.settimeout(None)
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if remainder:
                    upstream.sendall(remainder)
                self._relay(client, upstream)
                return

            parsed = urlsplit(target)
            if parsed.scheme.lower() != "http" or not parsed.hostname:
                client.sendall(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
                return
            host = parsed.hostname.rstrip(".").lower()
            port = parsed.port or 80
            if not self._client_allowed(client):
                self._refuse(client, host, port)
                return
            if not _matches(host, port, self.allow):
                self._refuse(client, host, port)
                return
            upstream = socket.create_connection((host, port), timeout=15)
            upstream.settimeout(None)
            client.settimeout(None)
            upstream.sendall(header + remainder)
            self._relay(client, upstream)
        except (OSError, ValueError, UnicodeError):
            if upstream is not None:
                with suppress(OSError):
                    client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
        finally:
            if upstream is not None:
                upstream.close()

    @staticmethod
    def _relay(client: socket.socket, upstream: socket.socket) -> None:
        peers = (client, upstream)
        ended: set[socket.socket] = set()
        while len(ended) < 2:
            readable, _, _ = select.select(
                [sock for sock in peers if sock not in ended], [], [], 30
            )
            if not readable:
                return
            for source in readable:
                destination = upstream if source is client else client
                try:
                    data = source.recv(65536)
                except OSError:
                    return
                if not data:
                    ended.add(source)
                    with suppress(OSError):
                        destination.shutdown(socket.SHUT_WR)
                    continue
                try:
                    destination.sendall(data)
                except OSError:
                    return
