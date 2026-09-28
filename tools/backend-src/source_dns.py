"""Bounded DNS lookup and HTTP connection support for playback sources."""

from __future__ import annotations

import concurrent.futures
import http.client
import ipaddress
import socket
import threading
import time
import urllib.request


DNS_LOOKUP_TIMEOUT_SECONDS = 1.0
"""How long one ``getaddrinfo`` call may occupy a slot, and how many may run at once.

A single ``/vod/detail`` page probes up to 18 playback lines in parallel, and each
probe resolves its host twice (URL safety pre-check plus the bounded connect). The
old 8-slot pool therefore ran out of capacity *inside one request* and rejected the
overflow instantly, which ``probe_stream`` reported as ``alive=False`` -- healthy
lines were killed by our own limiter rather than by the source station. The pool is
now wide enough for that fan-out, and contention queues for a slot instead of
rejecting outright.
"""
DNS_LOOKUP_MAX_WORKERS = 36
"""Widest lookup burst a single probe pass can create (18 lines x 2 lookups)."""
DNS_LOOKUP_SLOT_WAIT_SECONDS = 0.25
"""How long a lookup may queue for a slot before the caller is told it is over budget."""
_PROXY_FAKE_IP_RANGES = (ipaddress.ip_network("198.18.0.0/15"),)
_deadline_context = threading.local()


def set_deadline(deadline):
    previous = getattr(_deadline_context, "deadline", None)
    _deadline_context.deadline = deadline
    return previous


def restore_deadline(previous):
    if previous is None:
        try:
            del _deadline_context.deadline
        except AttributeError:
            pass
    else:
        _deadline_context.deadline = previous


def _current_deadline():
    return getattr(_deadline_context, "deadline", None)


class DNSCapacityError(TimeoutError):
    """Raised when the local lookup pool is full, not when the host is bad.

    This is a local resource limit. Callers must not turn it into "the source
    station is unreachable" -- the DNS answer was never attempted.
    """


class BoundedDNSResolver:
    """Run blocking platform DNS calls in a fixed number of disposable waiters.

    ``getaddrinfo`` itself cannot be cancelled portably. A timed-out lookup therefore
    keeps one daemon slot until the OS call returns, while its caller can return on
    deadline. The semaphore caps those lingering calls; excess lookups wait briefly
    for a slot and then fail with :class:`DNSCapacityError` rather than a plain
    timeout, so callers can tell "our pool was full" from "the lookup was too slow".
    """

    def __init__(self, max_workers=DNS_LOOKUP_MAX_WORKERS,
                 timeout_seconds=DNS_LOOKUP_TIMEOUT_SECONDS):
        self.max_workers = max(1, int(max_workers))
        self.timeout_seconds = max(0.001, float(timeout_seconds))
        self._slots = threading.BoundedSemaphore(self.max_workers)

    def getaddrinfo(self, host, port, *args, timeout=None, **kwargs):
        if timeout is None or timeout is socket._GLOBAL_DEFAULT_TIMEOUT:
            budget = self.timeout_seconds
        else:
            budget = min(self.timeout_seconds, max(0.0, float(timeout)))
        deadline = _current_deadline()
        if deadline is not None:
            budget = min(budget, deadline - time.monotonic())
        if budget <= 0:
            raise TimeoutError("DNS lookup deadline exceeded")
        # Queue for a slot rather than refusing outright: rejecting made ordinary
        # DNS latency look like a dead source station. The wait stays inside the
        # caller's own budget, so a busy pool still cannot overrun a deadline.
        started = time.monotonic()
        wait_budget = min(budget, DNS_LOOKUP_SLOT_WAIT_SECONDS)
        while not self._slots.acquire(blocking=False):
            if time.monotonic() - started >= wait_budget:
                raise DNSCapacityError("DNS lookup capacity exhausted")
            time.sleep(0.002)
        budget -= time.monotonic() - started
        if budget <= 0:
            self._slots.release()
            raise TimeoutError("DNS lookup deadline exceeded")

        result = concurrent.futures.Future()

        def resolve():
            try:
                addresses = socket.getaddrinfo(host, port, *args, **kwargs)
                result.set_result(addresses)
            except BaseException as exc:  # preserve resolver errors for the caller
                result.set_exception(exc)
            finally:
                self._slots.release()

        worker = threading.Thread(target=resolve, name="source-dns", daemon=True)
        try:
            worker.start()
        except BaseException:
            self._slots.release()
            raise
        try:
            return result.result(timeout=budget)
        except concurrent.futures.TimeoutError as exc:
            raise TimeoutError("DNS lookup deadline exceeded") from exc


_resolver = BoundedDNSResolver()


def getaddrinfo(host, port, *args, timeout=None, **kwargs):
    return _resolver.getaddrinfo(host, port, *args, timeout=timeout, **kwargs)


class UnsafeAddressError(OSError):
    """Raised when a resolved source address is outside the permitted public range."""


def _is_global_address(address):
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped.is_global
    return address.is_global


def _validate_connection_addresses(host, infos):
    addresses = []
    for info in infos:
        try:
            addresses.append(ipaddress.ip_address(info[4][0]))
        except (IndexError, TypeError, ValueError) as exc:
            raise UnsafeAddressError("source resolved to an invalid IP address") from exc

    if not addresses:
        raise UnsafeAddressError("source resolved to no IP addresses")

    try:
        host_address = ipaddress.ip_address(host)
    except (TypeError, ValueError):
        host_address = None

    # The app's URL validation allows this range only for DNS names because local
    # proxy clients may replace public hosts with synthetic addresses in this pool.
    # Literal addresses in the same range remain blocked.
    if host_address is None and all(
            any(address in network for network in _PROXY_FAKE_IP_RANGES)
            for address in addresses):
        return

    if all(_is_global_address(address) for address in addresses):
        return

    raise UnsafeAddressError("source resolved to a non-public IP address")


def _bounded_create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
                               source_address=None):
    host, port = address
    if timeout is socket._GLOBAL_DEFAULT_TIMEOUT:
        socket_timeout = socket.getdefaulttimeout()
        dns_timeout = None
    else:
        socket_timeout = timeout
        dns_timeout = timeout

    deadline = _current_deadline()
    if socket_timeout is not None:
        socket_deadline = time.monotonic() + max(0.0, float(socket_timeout))
        deadline = socket_deadline if deadline is None else min(deadline, socket_deadline)
    infos = getaddrinfo(host, port, 0, socket.SOCK_STREAM, timeout=dns_timeout)
    _validate_connection_addresses(host, infos)
    last_error = None
    for family, socktype, proto, _canonname, sockaddr in infos:
        sock = None
        try:
            sock = socket.socket(family, socktype, proto)
            if socket_timeout is not None:
                sock.settimeout(socket_timeout)
            if source_address:
                sock.bind(source_address)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("source connection deadline exceeded")
                sock.settimeout(remaining)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last_error = exc
            if sock is not None:
                sock.close()
    if last_error is not None:
        raise last_error
    raise OSError("getaddrinfo returned no usable addresses")


class _BoundedDNSConnectionMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _bounded_create_connection


class BoundedHTTPConnection(_BoundedDNSConnectionMixin, http.client.HTTPConnection):
    pass


class BoundedHTTPSConnection(_BoundedDNSConnectionMixin, http.client.HTTPSConnection):
    pass


class BoundedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(BoundedHTTPConnection, req)


class BoundedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(BoundedHTTPSConnection, req, context=self._context)
