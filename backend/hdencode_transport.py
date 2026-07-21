"""Approved construction point for listing/detail HTTP clients."""
from __future__ import annotations

import cloudscraper

from backend.hdencode_coordinator import require_transport_authorization


def create_source_http_client(*, hdencode: bool):
    if hdencode:
        require_transport_authorization()
    return cloudscraper.create_scraper()
