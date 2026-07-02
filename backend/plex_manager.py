"""Plex Manager Module - Improved Plex library management.

Provides:
- Auto-detection of libraries from Plex server
- Library type detection (movie/show)
- Library metadata caching
- Path mapping for remote/Docker setups
- Validation of library selections
"""

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class LibraryType(Enum):
    """Plex library types."""
    MOVIE = "movie"
    SHOW = "show"
    MUSIC = "artist"
    PHOTO = "photo"
    OTHER = "other"

    @classmethod
    def from_plex_type(cls, plex_type: str) -> 'LibraryType':
        """Convert Plex type string to LibraryType."""
        mapping = {
            'movie': cls.MOVIE,
            'show': cls.SHOW,
            'artist': cls.MUSIC,
            'photo': cls.PHOTO
        }
        return mapping.get(plex_type.lower(), cls.OTHER)


@dataclass
class PlexLibrary:
    """Information about a Plex library."""
    key: str  # Plex library key/id
    title: str  # Library name
    type: LibraryType
    scanner: str = ""  # Scanner type (e.g., "Plex Movie")
    agent: str = ""  # Metadata agent
    location: List[str] = field(default_factory=list)  # File paths
    item_count: int = 0
    last_scanned: Optional[datetime] = None
    uuid: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            'key': self.key,
            'title': self.title,
            'type': self.type.value,
            'scanner': self.scanner,
            'agent': self.agent,
            'location': self.location,
            'item_count': self.item_count,
            'last_scanned': self.last_scanned.isoformat() if self.last_scanned else None,
            'uuid': self.uuid
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PlexLibrary':
        """Create from dictionary."""
        return cls(
            key=data.get('key', ''),
            title=data.get('title', ''),
            type=LibraryType(data.get('type', 'other')),
            scanner=data.get('scanner', ''),
            agent=data.get('agent', ''),
            location=data.get('location', []),
            item_count=data.get('item_count', 0),
            last_scanned=datetime.fromisoformat(data['last_scanned']) if isinstance(data.get('last_scanned'), str) else None,
            uuid=data.get('uuid', '')
        )

    @classmethod
    def from_plex_section(cls, section) -> 'PlexLibrary':
        """Create from PlexAPI LibrarySection object."""
        locations = []
        try:
            locations = [loc.path for loc in section.locations]
        except Exception as e:
            logger.debug("Failed to get section locations for %s: %s", section.title, e)

        last_scanned = None
        try:
            if section.scannedAt:
                last_scanned = section.scannedAt
        except Exception as e:
            logger.debug("Failed to get scannedAt for %s: %s", section.title, e)

        return cls(
            key=str(section.key),
            title=section.title,
            type=LibraryType.from_plex_type(section.type),
            scanner=getattr(section, 'scanner', ''),
            agent=getattr(section, 'agent', ''),
            location=locations,
            item_count=section.totalSize if hasattr(section, 'totalSize') else 0,
            last_scanned=last_scanned,
            uuid=getattr(section, 'uuid', '')
        )


@dataclass
class PathMapping:
    """Path mapping for remote/Docker Plex setups."""
    plex_path: str  # Path as seen by Plex server
    local_path: str  # Corresponding local path
    enabled: bool = True

    def translate(self, path: str) -> str:
        """Translate a Plex path to local path."""
        if not self.enabled:
            return path
        if path.startswith(self.plex_path):
            return path.replace(self.plex_path, self.local_path, 1)
        return path


class PlexManager:
    """Manages Plex server connection and library operations."""

    def __init__(self, url: str = "", token: str = ""):
        self._url = url.rstrip('/') if url else ""
        self._token = token
        self._server = None
        self._account = None  # MyPlexAccount instance for remote connections
        self._connection_mode = "direct"  # "direct" or "account"
        self._username = ""
        self._password = ""
        self._server_name = ""  # target server name for account mode
        self._libraries: Dict[str, PlexLibrary] = {}
        self._path_mappings: List[PathMapping] = []
        self._cache_timestamp: Optional[datetime] = None
        self._cache_duration = timedelta(hours=1)
        self._lock = threading.Lock()
        self._callbacks: List[Callable[[str, Any], None]] = []

    @property
    def is_configured(self) -> bool:
        """Check if Plex server is configured."""
        if self._connection_mode == "account":
            return bool(self._username and self._password)
        return bool(self._url and self._token)

    @property
    def is_connected(self) -> bool:
        """Check if connected to Plex server."""
        return self._server is not None

    def configure(self, url: str, token: str, connection_mode: str = "direct",
                  username: str = "", password: str = "", server_name: str = ""):
        """Configure Plex server connection.

        Args:
            url: Direct URL for LAN mode
            token: Plex token for direct mode
            connection_mode: "direct" for LAN, "account" for plex.tv remote
            username: Plex account username (for account mode)
            password: Plex account password (for account mode)
            server_name: Target server name (for account mode)
        """
        self._url = url.rstrip('/') if url else ""
        self._token = token
        self._connection_mode = connection_mode
        self._username = username
        self._password = password
        self._server_name = server_name
        self._server = None
        self._account = None
        self._libraries.clear()
        self._cache_timestamp = None

    def connect(self, timeout: int = 10) -> Tuple[bool, str]:
        """Connect to Plex server using configured mode.

        Returns:
            Tuple of (success, message)
        """
        if self._connection_mode == "account":
            return self.connect_via_account(timeout=timeout)
        return self._connect_direct(timeout=timeout)

    def _connect_direct(self, timeout: int = 10) -> Tuple[bool, str]:
        """Connect directly to Plex server via URL (LAN mode)."""
        if not self._url or not self._token:
            return False, "Plex server URL and token required"

        try:
            from plexapi.server import PlexServer
            from plexapi.exceptions import Unauthorized, NotFound

            self._server = PlexServer(self._url, self._token, timeout=timeout)
            server_name = self._server.friendlyName
            logger.info(f"Connected to Plex server (direct): {server_name}")
            self._notify('connected', server_name)
            return True, f"Connected to {server_name}"

        except Unauthorized:
            self._server = None
            return False, "Invalid Plex token"
        except NotFound:
            self._server = None
            return False, "Plex server not found"
        except Exception as e:
            self._server = None
            logger.error(f"Plex connection failed: {e}")
            return False, f"Connection failed: {str(e)}"

    def connect_via_account(self, timeout: int = 10) -> Tuple[bool, str]:
        """Connect to Plex via plex.tv account (works over internet).

        Uses MyPlexAccount to discover and connect to the server,
        which handles NAT traversal via Plex relay automatically.

        Returns:
            Tuple of (success, message)
        """
        if not self._username or not self._password:
            return False, "Plex username and password required"

        try:
            from plexapi.myplex import MyPlexAccount

            logger.info("Signing in to plex.tv...")
            self._account = MyPlexAccount(self._username, self._password, timeout=timeout)

            # Get available servers
            resources = [r for r in self._account.resources() if 'server' in r.provides]

            if not resources:
                return False, "No Plex servers found on this account"

            # Find the target server
            target = None
            if self._server_name:
                for r in resources:
                    if r.name == self._server_name:
                        target = r
                        break
                if not target:
                    names = ", ".join(r.name for r in resources)
                    return False, f"Server '{self._server_name}' not found. Available: {names}"
            else:
                # Use first server
                target = resources[0]

            logger.info(f"Connecting to server '{target.name}' via plex.tv...")
            self._server = target.connect(timeout=timeout)
            server_name = self._server.friendlyName
            self._server_name = server_name  # remember for next time
            logger.info(f"Connected to Plex server (account): {server_name}")
            self._notify('connected', server_name)
            return True, f"Connected to {server_name} (via plex.tv)"

        except Exception as e:
            self._server = None
            self._account = None
            err = str(e)
            if "401" in err or "unauthorized" in err.lower():
                return False, "Invalid Plex username or password"
            logger.error(f"Plex account connection failed: {e}")
            return False, f"Connection failed: {err}"

    def discover_servers(self, username: str, password: str,
                         timeout: int = 10) -> Tuple[bool, str, List[str]]:
        """Discover available Plex servers on an account.

        Returns:
            Tuple of (success, message, server_names)
        """
        try:
            from plexapi.myplex import MyPlexAccount

            account = MyPlexAccount(username, password, timeout=timeout)
            resources = [r for r in account.resources() if 'server' in r.provides]
            names = [r.name for r in resources]
            return True, f"Found {len(names)} server(s)", names

        except Exception as e:
            err = str(e)
            if "401" in err or "unauthorized" in err.lower():
                return False, "Invalid username or password", []
            return False, f"Failed: {err}", []

    def disconnect(self):
        """Disconnect from Plex server."""
        self._server = None
        self._account = None
        self._notify('disconnected', None)

    def add_callback(self, callback: Callable[[str, Any], None]):
        """Add callback for events (connected, disconnected, libraries_updated)."""
        self._callbacks.append(callback)

    def _notify(self, event: str, data: Any):
        """Notify callbacks."""
        for callback in self._callbacks:
            try:
                callback(event, data)
            except Exception as e:
                logger.error(f"Callback error: {e}")

    def refresh_libraries(self, force: bool = False) -> List[PlexLibrary]:
        """Refresh library list from Plex server.

        Args:
            force: Force refresh even if cache is valid

        Returns:
            List of PlexLibrary objects
        """
        libraries = []
        notify_payload = None

        # Check cache under lock, then release for network I/O
        with self._lock:
            if not force and self._cache_timestamp:
                if datetime.now(timezone.utc) - self._cache_timestamp < self._cache_duration:
                    return list(self._libraries.values())

            if not self.is_connected:
                success, _ = self.connect()
                if not success:
                    return list(self._libraries.values())

            server = self._server  # snapshot for use outside lock

        # Network I/O outside lock to avoid blocking UI thread
        try:
            sections = server.library.sections()
            new_libraries = {}
            for section in sections:
                lib = PlexLibrary.from_plex_section(section)
                new_libraries[lib.title] = lib
                logger.debug(f"Found library: {lib.title} ({lib.type.value})")

            # Re-acquire lock for state mutation
            with self._lock:
                self._libraries = new_libraries
                self._cache_timestamp = datetime.now(timezone.utc)
                libraries = list(self._libraries.values())
                notify_payload = list(libraries)

        except Exception as e:
            logger.error(f"Failed to refresh libraries: {e}")
            with self._lock:
                libraries = list(self._libraries.values())

        if notify_payload is not None:
            self._notify('libraries_updated', notify_payload)
        return libraries

    def get_libraries(self) -> List[PlexLibrary]:
        """Get all libraries (from cache or refresh if needed)."""
        if not self._libraries:
            return self.refresh_libraries()
        return list(self._libraries.values())

    def get_library(self, name: str) -> Optional[PlexLibrary]:
        """Get a specific library by name."""
        if not self._libraries:
            self.refresh_libraries()
        return self._libraries.get(name)

    def get_movie_libraries(self) -> List[PlexLibrary]:
        """Get all movie libraries."""
        return [lib for lib in self.get_libraries() if lib.type == LibraryType.MOVIE]

    def get_tv_libraries(self) -> List[PlexLibrary]:
        """Get all TV show libraries."""
        return [lib for lib in self.get_libraries() if lib.type == LibraryType.SHOW]

    def validate_library_names(self, names: List[str]) -> Tuple[List[str], List[str]]:
        """Validate library names exist.

        Args:
            names: List of library names to validate

        Returns:
            Tuple of (valid_names, invalid_names)
        """
        libraries = self.get_libraries()
        known_names = {lib.title for lib in libraries}

        valid = [n for n in names if n in known_names]
        invalid = [n for n in names if n not in known_names]

        return valid, invalid

    def get_library_section(self, name: str):
        """Get the PlexAPI library section object.

        Args:
            name: Library name

        Returns:
            PlexAPI LibrarySection or None
        """
        if not self.is_connected:
            success, _ = self.connect()
            if not success:
                return None

        try:
            return self._server.library.section(name)
        except Exception as e:
            logger.error(f"Failed to get library section '{name}': {e}")
            return None

    def add_label(self, rating_key, label):
        """Add a Plex label to the item with ``rating_key`` (TEXT-safe)."""
        self._server.fetchItem(int(rating_key)).addLabel(label)

    def remove_label(self, rating_key, label):
        """Remove a Plex label from the item with ``rating_key`` (TEXT-safe)."""
        self._server.fetchItem(int(rating_key)).removeLabel(label)

    # Path mapping methods
    def add_path_mapping(self, plex_path: str, local_path: str):
        """Add a path mapping."""
        mapping = PathMapping(plex_path=plex_path, local_path=local_path)
        self._path_mappings.append(mapping)
        logger.info(f"Added path mapping: {plex_path} -> {local_path}")

    def remove_path_mapping(self, index: int):
        """Remove a path mapping by index."""
        if 0 <= index < len(self._path_mappings):
            del self._path_mappings[index]

    def clear_path_mappings(self):
        """Clear all path mappings."""
        self._path_mappings.clear()

    def translate_path(self, plex_path: str) -> str:
        """Translate a Plex path to local path using mappings.

        Tries longest plex_path prefix first so overlapping mappings
        (e.g. /media vs /media/movies) resolve deterministically.
        """
        sorted_mappings = sorted(
            self._path_mappings, key=lambda m: len(m.plex_path), reverse=True
        )
        for mapping in sorted_mappings:
            translated = mapping.translate(plex_path)
            if translated != plex_path:
                return translated
        return plex_path

    def get_path_mappings(self) -> List[PathMapping]:
        """Get all path mappings."""
        return self._path_mappings.copy()

    # Serialization
    def save_to_dict(self) -> Dict[str, Any]:
        """Save state to dictionary."""
        return {
            'url': self._url,
            'token': self._token,
            'libraries': {name: lib.to_dict() for name, lib in self._libraries.items()},
            'path_mappings': [
                {'plex_path': m.plex_path, 'local_path': m.local_path, 'enabled': m.enabled}
                for m in self._path_mappings
            ],
            'cache_timestamp': self._cache_timestamp.isoformat() if self._cache_timestamp else None
        }

    def load_from_dict(self, data: Dict[str, Any]):
        """Load state from dictionary."""
        self._url = data.get('url', '')
        self._token = data.get('token', '')

        self._libraries.clear()
        for name, lib_data in data.get('libraries', {}).items():
            self._libraries[name] = PlexLibrary.from_dict(lib_data)

        self._path_mappings.clear()
        for mapping_data in data.get('path_mappings', []):
            self._path_mappings.append(PathMapping(
                plex_path=mapping_data.get('plex_path', ''),
                local_path=mapping_data.get('local_path', ''),
                enabled=mapping_data.get('enabled', True)
            ))

        if data.get('cache_timestamp'):
            ts = datetime.fromisoformat(data['cache_timestamp'])
            # Ensure timezone-aware (handle legacy naive timestamps)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            self._cache_timestamp = ts

    def get_server_info(self) -> Optional[Dict[str, Any]]:
        """Get Plex server information."""
        if not self.is_connected:
            return None

        try:
            return {
                'name': self._server.friendlyName,
                'version': self._server.version,
                'platform': self._server.platform,
                'machine_id': self._server.machineIdentifier,
                'my_plex_username': getattr(self._server, 'myPlexUsername', None),
                'transcoder_active': getattr(self._server, 'transcoderActiveVideoSessions', 0)
            }
        except Exception as e:
            logger.error(f"Failed to get server info: {e}")
            return None

    def scan_library(self, name: str) -> bool:
        """Trigger a library scan on Plex server.

        Args:
            name: Library name to scan

        Returns:
            True if scan started successfully
        """
        section = self.get_library_section(name)
        if section:
            try:
                section.update()  # Triggers scan
                logger.info(f"Started scan for library: {name}")
                return True
            except Exception as e:
                logger.error(f"Failed to start scan for '{name}': {e}")
        return False

    def get_recently_added(self, since: datetime) -> List[Any]:
        """Get items added since the given timestamp.

        Args:
            since: Timestamp to filter by (datetime object)

        Returns:
            List of Plex items (Movie/Show objects)
        """
        if not self.is_connected:
            success, _ = self.connect()
            if not success:
                return []

        recent_items = []
        try:
            # Check libraries
            for lib in self.get_libraries():
                if lib.type not in (LibraryType.MOVIE, LibraryType.SHOW):
                    continue

                section = self.get_library_section(lib.title)
                if not section:
                    continue

                # Use Plex filter 'addedAt__gte' (greater than or equal to timestamp)
                # Timestamp must be unix epoch int or datetime
                try:
                    # Plex API expects kwargs for filters, and 'addedAt' supports '>>=' (gte)
                    # Convert datetime to unix timestamp (int) to avoid string comparison errors
                    ts = int(since.timestamp())
                    items = section.search(addedAt__gte=ts)
                    recent_items.extend(items)
                    logger.info(f"Incrementally synced {len(items)} items from '{lib.title}' (since {since})")
                except Exception as e:
                    logger.error(f"Failed to fetch recent items from '{lib.title}': {e}")

        except Exception as e:
            logger.error(f"Error getting recently added items: {e}")

        return recent_items


def migrate_library_config(old_config: Dict[str, Any]) -> Dict[str, Any]:
    """Migrate old library configuration to new format.

    Handles the mismatch between:
    - selected_movie_libraries / selected_tv_libraries (settings_dialog.py)
    - movie_libs / tv_libs (movie_app.py, config.py)

    Args:
        old_config: Old configuration dictionary

    Returns:
        Updated configuration dictionary
    """
    new_config = old_config.copy()

    # Merge movie library selections
    movie_libs = set(old_config.get('movie_libs', []))
    selected_movie = set(old_config.get('selected_movie_libraries') or [])
    known_movie = set(old_config.get('known_movie_libraries') or [])
    if selected_movie:
        movie_libs.update(selected_movie)
    # If movie_libs only has generic defaults but known_movie_libraries has real names, use those
    # Only apply this fallback once — skip if config already has the migration flag
    if not old_config.get('_library_config_migrated'):
        if known_movie and movie_libs <= {'Movies', ''}:
            movie_libs = known_movie
            new_config['_library_config_migrated'] = True
    new_config['movie_libs'] = list(movie_libs - {''})

    # Merge TV library selections
    tv_libs = set(old_config.get('tv_libs', []))
    selected_tv = set(old_config.get('selected_tv_libraries') or [])
    known_tv = set(old_config.get('known_tv_libraries') or [])
    if selected_tv:
        tv_libs.update(selected_tv)
    if not old_config.get('_library_config_migrated'):
        if known_tv and tv_libs <= {'TV Shows', ''}:
            tv_libs = known_tv
    new_config['tv_libs'] = list(tv_libs - {''})

    # Remove old keys
    new_config.pop('selected_movie_libraries', None)
    new_config.pop('selected_tv_libraries', None)

    return new_config


# Global Plex manager instance
_plex_manager: Optional[PlexManager] = None
_plex_manager_lock = threading.Lock()


def get_plex_manager() -> PlexManager:
    """Get the global Plex manager instance (thread-safe)."""
    global _plex_manager
    if _plex_manager is None:
        with _plex_manager_lock:
            if _plex_manager is None:
                _plex_manager = PlexManager()
    return _plex_manager


def configure_plex(url: str, token: str, connection_mode: str = "direct",
                    username: str = "", password: str = "",
                    server_name: str = "") -> PlexManager:
    """Configure and return the global Plex manager."""
    manager = get_plex_manager()
    manager.configure(url, token, connection_mode=connection_mode,
                      username=username, password=password,
                      server_name=server_name)
    return manager
