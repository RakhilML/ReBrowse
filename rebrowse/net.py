"""Shared HTTP client settings."""

from __future__ import annotations

import functools
import ssl

import certifi
import httpx


@functools.cache
def ssl_context() -> ssl.SSLContext:
    # Loading the CA bundle costs ~0.4s per client on Windows; build it once.
    return ssl.create_default_context(cafile=certifi.where())


def client(timeout: float, **kwargs) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, verify=ssl_context(), **kwargs)
