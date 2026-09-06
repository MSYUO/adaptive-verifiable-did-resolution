"""Test fixtures.

Design decision: resolver instances are started as REAL uvicorn servers on
real loopback sockets, so that router->resolver calls traverse an actual HTTP
stack. Injected delays and timeouts are therefore exercised through the same
path used in the docker deployment, rather than through an in-process mock
that could not produce a genuine transport timeout.

The router itself is exercised through an ASGI transport: it needs no socket
of its own, and this keeps the tests fast.

Timeouts and delays here are deliberately short (tens to low hundreds of ms)
so the suite finishes quickly.
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from avdr.config import ResolverEndpoint, RouterConfig  # noqa: E402
from avdr.resolver.app import create_app as create_resolver_app  # noqa: E402
from avdr.resolver.settings import ResolverBehavior, ResolverSettings  # noqa: E402
from avdr.router.app import create_app as create_router_app  # noqa: E402
from avdr.telemetry import TelemetrySink  # noqa: E402

# Short, controlled values. All CONTROLLED INJECTION test knobs.
ATTEMPT_TIMEOUT_MS = 400
INJECTED_DELAY_MS = 150
TIMEOUT_SLEEP_MS = 5000


# --------------------------------------------------------------------------
# Public-network guard
#
# The public resolver endpoint enforces 10 requests / 1800 s. The test suite
# must never spend that budget, so outbound connections to anything other than
# loopback are blocked for the whole session. This is enforced at the socket
# layer rather than by convention, so a new test cannot quietly start calling
# the Internet.
# --------------------------------------------------------------------------

_LOOPBACK = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}


class PublicNetworkBlocked(RuntimeError):
    pass


@pytest.fixture(scope="session", autouse=True)
def block_public_network():
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _check(address):
        if isinstance(address, tuple) and address:
            host = str(address[0])
            if host not in _LOOPBACK:
                raise PublicNetworkBlocked(
                    f"test attempted an outbound connection to {host!r}; the "
                    f"suite must make zero public-network calls (public "
                    f"resolver budget is 10 requests / 1800 s)"
                )

    def guarded_connect(self, address):
        _check(address)
        return real_connect(self, address)

    def guarded_connect_ex(self, address):
        _check(address)
        return real_connect_ex(self, address)

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    try:
        yield
    finally:
        socket.socket.connect = real_connect
        socket.socket.connect_ex = real_connect_ex


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class BackgroundResolver:
    """A mock resolver running in a uvicorn server on a background thread."""

    def __init__(self, resolver_id: str) -> None:
        self.resolver_id = resolver_id
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        settings = ResolverSettings(
            resolver_id=resolver_id,
            port=self.port,
            behavior=ResolverBehavior(),
        )
        self.app = create_resolver_app(settings)
        config = uvicorn.Config(
            self.app,
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            lifespan="on",
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self, timeout_s: float = 15.0) -> None:
        self.thread.start()
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.server.started:
                return
            time.sleep(0.02)
        raise RuntimeError(f"resolver {self.resolver_id} failed to start")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)

    def set_behavior(self, **kwargs) -> dict:
        """Apply an injected behaviour. Resolver-side, never router-side."""
        behavior = ResolverBehavior(**kwargs)
        response = httpx.post(
            f"{self.url}/admin/behavior",
            json=behavior.model_dump(),
            timeout=5.0,
        )
        response.raise_for_status()
        return response.json()

    def reset(self) -> None:
        httpx.post(f"{self.url}/admin/reset", timeout=5.0).raise_for_status()


@pytest.fixture(scope="session")
def resolver_cluster():
    """Three mock resolvers on real loopback sockets, for the whole session."""
    resolvers = {}
    for resolver_id in ("resolver-a", "resolver-b", "resolver-c"):
        resolver = BackgroundResolver(resolver_id)
        resolver.start()
        resolvers[resolver_id] = resolver
    yield resolvers
    for resolver in resolvers.values():
        resolver.stop()


@pytest.fixture
def healthy_cluster(resolver_cluster):
    """Reset every resolver to healthy before and after each test."""
    for resolver in resolver_cluster.values():
        resolver.reset()
    yield resolver_cluster
    for resolver in resolver_cluster.values():
        resolver.reset()


@pytest.fixture
def router_config(healthy_cluster, tmp_path) -> RouterConfig:
    return RouterConfig(
        resolvers=[
            ResolverEndpoint(id=rid, url=r.url)
            for rid, r in healthy_cluster.items()
        ],
        default_policy="sequential-failover",
        single_static_target="resolver-a",
        attempt_timeout_ms=ATTEMPT_TIMEOUT_MS,
        policy_version="test-baseline-v1",
        telemetry_dir=str(tmp_path / "telemetry"),
    )


@pytest.fixture
def sink(router_config) -> TelemetrySink:
    return TelemetrySink(router_config.telemetry_dir)


@pytest.fixture
async def router_client(router_config, sink):
    """HTTP client bound to a fresh router app (fresh round-robin state)."""
    app = create_router_app(config=router_config, sink=sink)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://router"
    ) as client:
        # Drive the lifespan so app.state.client exists.
        async with httpx.AsyncClient() as outbound:
            app.state.client = outbound
            yield client
