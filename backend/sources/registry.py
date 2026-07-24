"""Source Registry - Manages source plugins and provides unified interface."""

import asyncio
import importlib
import logging
import os
import pkgutil
import threading
from typing import Any, Callable, Dict, List, Optional, Type

from backend.config import source_enabled

from .base import (
    SourceBase,
    SourceConfig,
    SourceCapability,
    ParsedRelease,
    PageResult
)

logger = logging.getLogger(__name__)


class SourceRegistry:
    """Registry for source plugins.

    Manages source registration, discovery, and provides a unified interface
    for fetching releases from multiple sources.
    """

    def __init__(self):
        self._sources: Dict[str, Type[SourceBase]] = {}
        self._instances: Dict[str, SourceBase] = {}
        self._configs: Dict[str, SourceConfig] = {}
        self._enabled: Dict[str, bool] = {}
        self._callbacks: List[Callable[[str, Any], None]] = []

    def register(self, source_class: Type[SourceBase]) -> bool:
        """Register a source plugin.

        Args:
            source_class: Source class (must inherit from SourceBase)

        Returns:
            True if registered successfully
        """
        try:
            config = source_class.get_config()
            name = config.name

            if name in self._sources:
                logger.warning(f"Source '{name}' already registered, replacing")

            self._sources[name] = source_class
            self._configs[name] = config
            self._enabled[name] = config.enabled

            logger.info(f"Registered source: {config.display_name} ({name})")
            self._notify('registered', name)

            return True

        except Exception as e:
            logger.error(f"Failed to register source: {e}")
            return False

    def unregister(self, name: str):
        """Unregister a source."""
        if name in self._sources:
            del self._sources[name]
            self._configs.pop(name, None)
            self._enabled.pop(name, None)
            self._instances.pop(name, None)
            logger.info(f"Unregistered source: {name}")
            self._notify('unregistered', name)

    def get_source(self, name: str) -> Optional[SourceBase]:
        """Get a source instance by name."""
        if name not in self._sources:
            return None

        # Lazy instantiation
        if name not in self._instances:
            self._instances[name] = self._sources[name]()

        return self._instances[name]

    def get_config(self, name: str) -> Optional[SourceConfig]:
        """Get source configuration."""
        return self._configs.get(name)

    def get_all_sources(self) -> List[SourceBase]:
        """Get all registered source instances."""
        return [self.get_source(name) for name in self._sources]

    def get_enabled_sources(self) -> List[SourceBase]:
        """Get all enabled source instances."""
        return [
            self.get_source(name)
            for name in self._sources
            if self._enabled.get(name, True)
        ]

    def list_sources(self) -> List[Dict[str, Any]]:
        """List all registered sources with their info."""
        return [
            {
                'name': config.name,
                'display_name': config.display_name,
                'base_url': config.base_url,
                'capabilities': str(config.capabilities),
                'enabled': self._enabled.get(config.name, True),
                'priority': config.priority
            }
            for config in sorted(
                self._configs.values(),
                key=lambda c: c.priority,
                reverse=True
            )
        ]

    def enable_source(self, name: str, enabled: bool = True):
        """Enable or disable a source."""
        if name in self._enabled:
            self._enabled[name] = enabled
            logger.info(f"Source '{name}' {'enabled' if enabled else 'disabled'}")
            self._notify('enabled' if enabled else 'disabled', name)

    def disable_source(self, name: str):
        """Disable a source."""
        self.enable_source(name, False)

    def sync_from_config(self, config: dict):
        """Sync enabled/disabled state from app config.

        Maps config keys like ``ddlbase_enabled`` → source name ``ddlbase``.
        """
        for name in list(self._enabled):
            key = f"{name}_enabled"
            if key in config:
                enabled = source_enabled(
                    config,
                    key,
                    missing_default=self._enabled.get(name, True),
                )
                if self._enabled.get(name) != enabled:
                    self.enable_source(name, enabled)

    def add_callback(self, callback: Callable[[str, Any], None]):
        """Add callback for registry events."""
        self._callbacks.append(callback)

    def _notify(self, event: str, data: Any):
        """Notify callbacks of an event."""
        for callback in self._callbacks:
            try:
                callback(event, data)
            except Exception as e:
                logger.error(f"Registry callback error: {e}")

    async def fetch_from_source(
        self,
        source_name: str,
        page: int = 1,
        mode: str = "movies",
        **kwargs
    ) -> PageResult:
        """Fetch releases from a specific source.

        Args:
            source_name: Name of the source
            page: Page number
            mode: Content mode
            **kwargs: Additional parameters

        Returns:
            PageResult from the source
        """
        source = self.get_source(source_name)
        if not source:
            return PageResult(
                releases=[],
                errors=[f"Source '{source_name}' not found"]
            )

        try:
            return await source.fetch_page(page, mode, **kwargs)
        except Exception as e:
            logger.error(f"Error fetching from {source_name}: {e}")
            return PageResult(releases=[], errors=[str(e)])

    async def fetch_from_all(
        self,
        page: int = 1,
        mode: str = "movies",
        parallel: bool = True,
        **kwargs
    ) -> Dict[str, PageResult]:
        """Fetch releases from all enabled sources.

        Args:
            page: Page number
            mode: Content mode
            parallel: Fetch in parallel if True
            **kwargs: Additional parameters

        Returns:
            Dict mapping source name to PageResult
        """
        sources = self.get_enabled_sources()

        if parallel:
            tasks = [
                source.fetch_page(page, mode, **kwargs)
                for source in sources
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            return {
                source.name: (
                    result if isinstance(result, PageResult)
                    else PageResult(releases=[], errors=[str(result)])
                )
                for source, result in zip(sources, results)
            }
        else:
            results = {}
            for source in sources:
                try:
                    results[source.name] = await source.fetch_page(page, mode, **kwargs)
                except Exception as e:
                    results[source.name] = PageResult(releases=[], errors=[str(e)])
            return results

    async def fetch_all_releases(
        self,
        mode: str = "movies",
        max_pages: int = 5,
        **kwargs
    ) -> List[ParsedRelease]:
        """Fetch all releases from all enabled sources.

        Args:
            mode: Content mode
            max_pages: Max pages per source
            **kwargs: Additional parameters

        Returns:
            Combined list of releases, sorted by priority
        """
        sources = self.get_enabled_sources()

        # Fetch from all sources in parallel
        tasks = [
            source.fetch_all_pages(mode, max_pages, **kwargs)
            for source in sources
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_releases = []
        for source, result in zip(sources, results):
            if isinstance(result, list):
                # Add source priority for sorting
                for release in result:
                    release.raw_data['_priority'] = self._configs[source.name].priority
                all_releases.extend(result)
            else:
                logger.error(f"Error from {source.name}: {result}")

        # Sort by source priority (higher first), then by date if available
        all_releases.sort(
            key=lambda r: (
                r.raw_data.get('_priority', 0),
                r.release_date or datetime.min
            ),
            reverse=True
        )

        return all_releases

    async def search_all(
        self,
        query: str,
        mode: str = "all",
        **kwargs
    ) -> Dict[str, PageResult]:
        """Search all sources that support search.

        Args:
            query: Search query
            mode: Content mode
            **kwargs: Additional parameters

        Returns:
            Dict mapping source name to search results
        """
        sources = [
            s for s in self.get_enabled_sources()
            if SourceCapability.SEARCH in s.config.capabilities
        ]

        tasks = [source.search(query, mode, **kwargs) for source in sources]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        return {
            source.name: (
                result if isinstance(result, PageResult)
                else PageResult(releases=[], errors=[str(result)])
            )
            for source, result in zip(sources, results)
        }

    def discover_sources(self, package_path: Optional[str] = None):
        """Discover and register source plugins from a package.

        Args:
            package_path: Path to sources package (defaults to 'sources' subdir)
        """
        if package_path is None:
            package_path = os.path.dirname(__file__)

        for _, name, _ in pkgutil.iter_modules([package_path]):
            if name.startswith('_') or name in ('base', 'registry'):
                continue

            try:
                module = importlib.import_module(f'.{name}', 'backend.sources')

                # Look for SourceBase subclasses
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if (
                        isinstance(attr, type) and
                        issubclass(attr, SourceBase) and
                        attr is not SourceBase
                    ):
                        self.register(attr)

            except Exception as e:
                logger.warning(f"Failed to load source module '{name}': {e}")

    def get_stats(self) -> Dict[str, Any]:
        """Get registry statistics."""
        return {
            'total_sources': len(self._sources),
            'enabled_sources': sum(1 for v in self._enabled.values() if v),
            'sources': self.list_sources()
        }


# Import datetime for sorting
from datetime import datetime

# Global registry instance
_registry: Optional[SourceRegistry] = None
_registry_lock = threading.Lock()


def get_registry() -> SourceRegistry:
    """Get the global source registry."""
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = SourceRegistry()
                # Auto-discover sources
                _registry.discover_sources()
    return _registry
