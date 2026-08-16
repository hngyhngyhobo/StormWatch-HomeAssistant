"""/healthz endpoint on :8099 — JSON status: source connectivity, current
state, quota usage, config validity (DESIGN.md §10). The status payload
itself is built by ``Supervisor._status()`` in ``__main__.py``; this module
only serves whatever that callable returns. ``sources.lightning`` and
``state.swim_status`` appear once Blitzortung wiring is active
(``BLITZORTUNG_ENABLED``); ``sources.rain`` and ``state.rain_last_24h``
appear once rain wiring is active (``RAIN_ENABLED``, task D2) -- both are
simply absent when their feature isn't. Unlike NWS/lightning, rain
unavailability never flips the overall ``status`` to ``degraded`` -- it's a
watering-decision feature, not a safety one (see ``Supervisor._status``'s
docstring).
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger("stormwatch.health")

StatusProvider = Callable[[], dict]

_HEALTHZ_PATH = "/healthz"


class _HealthRequestHandler(BaseHTTPRequestHandler):
    """Serves GET /healthz as JSON from ``self.server.status_provider()``.

    Everything else 404s. Access logging is routed through the stdlib
    logging module (at DEBUG) instead of stderr.
    """

    server: _HealthHTTPServer

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path.split("?", 1)[0] != _HEALTHZ_PATH:
            self.send_response(404)
            self.end_headers()
            return

        body = json.dumps(self.server.status_provider()).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        logger.debug("%s - " + format, self.address_string(), *args)


class _HealthHTTPServer(ThreadingHTTPServer):
    """A ThreadingHTTPServer carrying the injected status_provider callback."""

    def __init__(self, server_address: tuple[str, int], status_provider: StatusProvider) -> None:
        super().__init__(server_address, _HealthRequestHandler)
        self.status_provider = status_provider


class HealthServer:
    """Handle to a running health endpoint; stop() joins its daemon thread."""

    def __init__(self, server: _HealthHTTPServer, thread: threading.Thread) -> None:
        self._server = server
        self._thread = thread

    @property
    def port(self) -> int:
        """The bound port — useful when started with port=0 (tests)."""
        return self._server.server_address[1]

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def start_health_server(
    status_provider: StatusProvider, port: int = 8099, host: str = "0.0.0.0"
) -> HealthServer:
    """Start the health endpoint in a background daemon thread.

    ``status_provider`` is invoked on every request and its return value
    (a JSON-serializable dict) becomes the /healthz body:
    ``{status, sources: {nws: {available}, lightning: {available}?,
    rain: {available}?}, state: {active_alerts, swim_status?,
    rain_last_24h?}, config_ok, version}`` — the ``lightning``/
    ``swim_status`` entries are present only when Blitzortung wiring is
    active, and the ``rain``/``rain_last_24h`` entries only when rain
    wiring is active. Passing ``port=0`` binds an OS-assigned ephemeral
    port — tests read it back via the returned ``HealthServer.port``.
    """
    server = _HealthHTTPServer((host, port), status_provider)
    thread = threading.Thread(target=server.serve_forever, name="stormwatch-health", daemon=True)
    thread.start()
    return HealthServer(server, thread)
