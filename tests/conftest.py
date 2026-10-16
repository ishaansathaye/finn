"""Keep tests offline even when the checkout has live provider credentials."""

from importlib import import_module
from importlib.util import find_spec
from typing import NoReturn

from httpx import AsyncHTTPTransport, HTTPTransport
import pytest


def _deny_network(*args, **kwargs) -> NoReturn:
    # pytest's failure escapes ordinary provider retry/fallback exception handlers.
    # Suppress traceback locals and never include URLs, headers, or payloads.
    pytest.fail("Network disabled; use MockTransport or ASGITransport", pytrace=False)


async def _deny_async_network(*args, **kwargs) -> NoReturn:
    _deny_network()


def _install_network_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    # Patch actual wire transports, not Client classes or their request methods:
    # MockTransport, ASGITransport, and test-owned fake clients stay usable.
    monkeypatch.setattr(HTTPTransport, "handle_request", _deny_network)
    monkeypatch.setattr(AsyncHTTPTransport, "handle_async_request", _deny_async_network)

    if find_spec("requests") is not None:
        sessions = import_module("requests.sessions")
        adapters = import_module("requests.adapters")
        monkeypatch.setattr(sessions.Session, "request", _deny_network)
        # Also cover callers that send a PreparedRequest directly.
        monkeypatch.setattr(adapters.HTTPAdapter, "send", _deny_network)

    if find_spec("curl_cffi") is not None:
        curl_requests = import_module("curl_cffi.requests")
        monkeypatch.setattr(curl_requests.Session, "request", _deny_network)
        if hasattr(curl_requests, "AsyncSession"):
            monkeypatch.setattr(curl_requests.AsyncSession, "request", _deny_async_network)


def pytest_configure(config: pytest.Config) -> None:
    # An autouse fixture alone starts too late for collection or session setup.
    guard = pytest.MonkeyPatch()
    config.add_cleanup(guard.undo)
    _install_network_guards(guard)


@pytest.fixture(autouse=True)
def deny_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_network_guards(monkeypatch)
