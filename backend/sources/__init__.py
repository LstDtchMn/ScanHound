"""Sources Package - Multi-source plugin architecture for media release sites.

This package provides:
- Abstract base class for source plugins
- Source registry for managing plugins
- Built-in HDEncode implementation
"""

from .base import (
    SourceBase,
    SourceConfig,
    SourceCapability,
    ParsedRelease,
    PageResult
)
from .registry import SourceRegistry, get_registry

__all__ = [
    'SourceBase',
    'SourceConfig',
    'SourceCapability',
    'ParsedRelease',
    'PageResult',
    'SourceRegistry',
    'get_registry'
]
