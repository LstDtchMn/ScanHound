"""Tests for DNS-rebinding-resistant HDEncode RSS transport."""
import socket

import pytest

from backend.sources import hdencode_feed_client as client


def test_private_resolution_is_rejected(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ],
    )
    with pytest.raises(ValueError, match="Unsafe"):
        client._validated_target("https://hdencode.org/feed/")


def test_resolved_public_ip_is_carried_to_connection(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    parsed, host, port, addresses = client._validated_target(
        "https://hdencode.org/feed/"
    )
    assert host == "hdencode.org"
    assert port == 443
    assert addresses == ("93.184.216.34",)
    assert parsed.path == "/feed/"
