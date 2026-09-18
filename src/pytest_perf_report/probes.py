"""Instrumentation probes.

Each probe monkeypatches one seam, funnels measurements into
``runtime.record_*``, and restores the original on uninstall. Probes for
optional libraries (Django, SQLAlchemy, requests, httpx) detect availability
at install time and silently no-op when the library is absent — that is the
whole genericity strategy: measure through the narrowest stdlib seam when we
can (http.client, socket, time, io) and through the library's own extension
API when one exists (Django's execute_wrapper, SQLAlchemy's cursor events).

Patching etiquette: wrappers carry ``functools.wraps`` metadata, delegate
inside try/finally (originals' return values and exceptions pass through
untouched), and uninstall restores the original only when the seam still
holds our wrapper — if another library patched on top of us mid-session, we
leave its patch alone rather than clobber it (a stranded wrapper is a
harmless pass-through once the session state is cleared).

Known blind spots, by design rather than accident:

- Database drivers that connect/talk in C (psycopg, mysqlclient) are invisible
  to the socket probe; their queries are still fully measured via the ORM-level
  probes, which is where the useful attribution lives anyway.
- ``asyncio.sleep`` is not measured (the event loop keeps running; wall-time
  attribution would be misleading). ``time.sleep`` is.
- Non-blocking connects (asyncio) are counted but their wait happens in the
  selector, so their connect *time* reads ~0.
- ``os.open`` and ``codecs.open`` bypass the file probe; ``open`` and
  ``io.open`` (which ``pathlib`` uses) are covered.
- Response *body* streaming time after ``getresponse()`` returns is not
  attributed to HTTP.
"""

from __future__ import annotations

import builtins
import functools
import gc
import io
import socket
import time
from time import perf_counter
from typing import Any
from urllib.parse import urlsplit

from pytest_perf_report import runtime


def _restore(obj: Any, attr: str, wrapper: Any, original: Any) -> None:
    """Put ``original`` back only if the seam still holds our ``wrapper``."""
    if getattr(obj, attr, None) is wrapper:
        setattr(obj, attr, original)


class Probe:
    def install(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def uninstall(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def refresh(self) -> None:
        """Re-assert instrumentation between tests (for late-created handles)."""


class SleepProbe(Probe):
    def install(self) -> None:
        self._orig = orig = time.sleep

        @functools.wraps(orig)
        def sleep(seconds: float) -> None:
            t0 = perf_counter()
            try:
                orig(seconds)
            finally:
                runtime.record_sleep(perf_counter() - t0)

        self._wrapper = sleep
        time.sleep = sleep

    def uninstall(self) -> None:
        _restore(time, "sleep", self._wrapper, self._orig)


class FileOpenProbe(Probe):
    """Wraps both ``builtins.open`` and ``io.open``: they start out as the
    same function, but they are separate bindings, and pathlib goes through
    ``io.open`` — patching only builtins would miss Path.read_text() et al."""

    def install(self) -> None:
        self._orig_builtins = builtins.open
        self._orig_io = io.open

        @functools.wraps(self._orig_builtins)
        def open_(file: Any, *args: Any, **kwargs: Any) -> Any:
            runtime.record_file_open(file)
            return self._orig_builtins(file, *args, **kwargs)

        self._wrapper = open_
        builtins.open = open_
        io.open = open_

    def uninstall(self) -> None:
        _restore(builtins, "open", self._wrapper, self._orig_builtins)
        _restore(io, "open", self._wrapper, self._orig_io)


class GcProbe(Probe):
    def install(self) -> None:
        self._t0: float | None = None

        def callback(phase: str, info: dict[str, Any]) -> None:
            if phase == "start":
                self._t0 = perf_counter()
            elif self._t0 is not None:
                runtime.record_gc(perf_counter() - self._t0)
                self._t0 = None

        self._callback = callback
        gc.callbacks.append(callback)

    def uninstall(self) -> None:
        try:
            gc.callbacks.remove(self._callback)
        except ValueError:
            pass


_LOOPBACK_HOSTS = {"localhost", "0.0.0.0", "::", "::1"}


def _is_loopback(host: str) -> bool:
    return (
        host in _LOOPBACK_HOSTS
        or host.startswith("127.")
        or host.startswith("::ffff:127.")
    )


class SocketProbe(Probe):
    """Times TCP/Unix connect() calls and flags non-loopback (real network) ones."""

    def install(self) -> None:
        self._orig = orig = socket.socket.connect

        @functools.wraps(orig)
        def connect(sock: Any, address: Any) -> Any:
            t0 = perf_counter()
            try:
                result = orig(sock, address)
            except socket.gaierror:
                # Name resolution failed — no connection ever left the
                # machine, so it isn't network egress.
                raise
            except Exception:
                # Includes BlockingIOError from non-blocking (asyncio)
                # connects: a real attempt, counted with ~0 duration.
                self._record(address, perf_counter() - t0)
                raise
            self._record(address, perf_counter() - t0)
            return result

        self._wrapper = connect
        socket.socket.connect = connect  # type: ignore[method-assign]

    @staticmethod
    def _record(address: Any, duration: float) -> None:
        try:
            if isinstance(address, tuple) and len(address) >= 2:
                host = str(address[0])
                endpoint = f"{host}:{address[1]}"
                external = not _is_loopback(host)
            else:
                endpoint = f"unix:{str(address)[:120]}"
                external = False
            runtime.record_net_connect(endpoint, duration, external)
        except Exception:
            pass

    def uninstall(self) -> None:
        _restore(socket.socket, "connect", self._wrapper, self._orig)


class HttpClientProbe(Probe):
    """Times requests at the http.client layer (urllib, urllib3, requests, botocore).

    ``putrequest`` is the entry point everything funnels through (urllib3 v2
    reimplements ``request()`` but still calls ``putrequest``); ``getresponse``
    is where the wait happens. Calls already counted by a higher-level client
    probe (requests/httpx) are skipped via the context-local depth counter.
    """

    def install(self) -> None:
        import http.client

        self._cls = http.client.HTTPConnection
        self._orig_putrequest = orig_putrequest = self._cls.putrequest
        self._orig_getresponse = orig_getresponse = self._cls.getresponse

        @functools.wraps(orig_putrequest)
        def putrequest(conn: Any, *args: Any, **kwargs: Any) -> Any:
            conn._perf_report_t0 = perf_counter()
            runtime.record_raw_http_host(str(conn.host))
            return orig_putrequest(conn, *args, **kwargs)

        @functools.wraps(orig_getresponse)
        def getresponse(conn: Any, *args: Any, **kwargs: Any) -> Any:
            start = perf_counter()
            try:
                return orig_getresponse(conn, *args, **kwargs)
            finally:
                t0 = getattr(conn, "_perf_report_t0", None) or start
                conn._perf_report_t0 = None
                if runtime.http_depth() == 0:
                    runtime.record_http_call(str(conn.host), perf_counter() - t0)

        self._wrap_putrequest = putrequest
        self._wrap_getresponse = getresponse
        self._cls.putrequest = putrequest  # type: ignore[method-assign]
        self._cls.getresponse = getresponse  # type: ignore[method-assign]

    def uninstall(self) -> None:
        _restore(self._cls, "putrequest", self._wrap_putrequest, self._orig_putrequest)
        _restore(
            self._cls, "getresponse", self._wrap_getresponse, self._orig_getresponse
        )


class RequestsProbe(Probe):
    """Times requests at the Session level, which also sees calls answered by
    transport-level mocks (responses, requests-mock) — those count as HTTP
    *client* calls but never show up as real network traffic."""

    def install(self) -> None:
        try:
            import requests
        except Exception:
            self._orig = None
            return
        self._cls = requests.Session
        self._orig = orig = requests.Session.send

        @functools.wraps(orig)
        def send(session: Any, request: Any, **kwargs: Any) -> Any:
            with runtime.timed_http_call(lambda: urlsplit(request.url).hostname):
                return orig(session, request, **kwargs)

        self._wrapper = send
        requests.Session.send = send  # type: ignore[method-assign]

    def uninstall(self) -> None:
        if self._orig is not None:
            _restore(self._cls, "send", self._wrapper, self._orig)


class HttpxProbe(Probe):
    def install(self) -> None:
        try:
            import httpx
        except Exception:
            self._mod = None
            return
        self._mod = httpx
        self._orig_sync = orig_sync = httpx.Client.send
        self._orig_async = orig_async = httpx.AsyncClient.send

        @functools.wraps(orig_sync)
        def send(client: Any, request: Any, **kwargs: Any) -> Any:
            with runtime.timed_http_call(lambda: str(request.url.host)):
                return orig_sync(client, request, **kwargs)

        @functools.wraps(orig_async)
        async def send_async(client: Any, request: Any, **kwargs: Any) -> Any:
            with runtime.timed_http_call(lambda: str(request.url.host)):
                return await orig_async(client, request, **kwargs)

        self._wrap_sync = send
        self._wrap_async = send_async
        httpx.Client.send = send  # type: ignore[method-assign]
        httpx.AsyncClient.send = send_async  # type: ignore[method-assign]

    def uninstall(self) -> None:
        if self._mod is not None:
            _restore(self._mod.Client, "send", self._wrap_sync, self._orig_sync)
            _restore(self._mod.AsyncClient, "send", self._wrap_async, self._orig_async)


def _django_execute_hook(
    execute: Any, sql: Any, params: Any, many: Any, context: Any
) -> Any:
    start = perf_counter()
    try:
        return execute(sql, params, many, context)
    finally:
        try:
            vendor = context["connection"].vendor
        except Exception:
            vendor = "django"
        runtime.record_query(
            sql if isinstance(sql, str) else str(sql),
            perf_counter() - start,
            vendor,
        )


_django_execute_hook._is_perf_report_hook = True  # type: ignore[attr-defined]


class DjangoProbe(Probe):
    """Hooks Django's execute_wrapper chain on every connection (any backend)."""

    def install(self) -> None:
        self._signal = None
        self._connections = None
        self._settings = None
        try:
            from django.conf import settings
            from django.db import connections
            from django.db.backends.signals import connection_created
        except Exception:
            return
        self._signal = connection_created
        self._connections = connections
        self._settings = settings
        connection_created.connect(self._on_connection_created)
        self.refresh()

    def _on_connection_created(self, sender: Any, connection: Any, **kw: Any) -> None:
        self._wrap(connection)

    @staticmethod
    def _wrap(connection: Any) -> None:
        wrappers = connection.execute_wrappers
        if any(getattr(w, "_is_perf_report_hook", False) for w in wrappers):
            return
        wrappers.append(_django_execute_hook)

    def refresh(self) -> None:
        """Cover connections opened before install or by other plugins."""
        if self._connections is None or not self._settings.configured:
            return
        for alias in self._connections:
            try:
                self._wrap(self._connections[alias])
            except Exception:
                continue

    def uninstall(self) -> None:
        if self._signal is None:
            return
        self._signal.disconnect(self._on_connection_created)
        try:
            for alias in self._connections:
                try:
                    wrappers = self._connections[alias].execute_wrappers
                    wrappers[:] = [
                        w
                        for w in wrappers
                        if not getattr(w, "_is_perf_report_hook", False)
                    ]
                except Exception:
                    continue
        except Exception:
            pass


class SQLAlchemyProbe(Probe):
    def install(self) -> None:
        self._engine_cls = None
        try:
            from sqlalchemy import event
            from sqlalchemy.engine import Engine
        except Exception:
            return
        self._event = event
        self._engine_cls = Engine

        def before(
            conn: Any,
            cursor: Any,
            statement: Any,
            parameters: Any,
            context: Any,
            executemany: Any,
        ) -> None:
            conn.info.setdefault("_perf_report_t0", []).append(perf_counter())

        def after(
            conn: Any,
            cursor: Any,
            statement: Any,
            parameters: Any,
            context: Any,
            executemany: Any,
        ) -> None:
            stack = conn.info.get("_perf_report_t0")
            t0 = stack.pop() if stack else perf_counter()
            try:
                vendor = conn.dialect.name
            except Exception:
                vendor = "sqlalchemy"
            runtime.record_query(str(statement), perf_counter() - t0, vendor)

        self._before = before
        self._after = after
        event.listen(Engine, "before_cursor_execute", before)
        event.listen(Engine, "after_cursor_execute", after)

    def uninstall(self) -> None:
        if self._engine_cls is None:
            return
        try:
            self._event.remove(self._engine_cls, "before_cursor_execute", self._before)
            self._event.remove(self._engine_cls, "after_cursor_execute", self._after)
        except Exception:
            pass


def build_probes() -> list[Probe]:
    return [
        DjangoProbe(),
        FileOpenProbe(),
        GcProbe(),
        HttpClientProbe(),
        HttpxProbe(),
        RequestsProbe(),
        SleepProbe(),
        SocketProbe(),
        SQLAlchemyProbe(),
    ]
