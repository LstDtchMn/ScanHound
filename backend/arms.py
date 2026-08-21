"""One arm identity, computed in one place, used by every layer that names an arm.

Round 18 (M18-1) found THREE incompatible identities for the same thing:

    traversal   "%s:%s:%s" % (source, category, endpoint_slug)
    ledger      "%s:%s"    % (source, category)          database.py
    contracts   report.source                            coverage.py

Three namespaces means a policy cannot join a durable claim to a coverage proof
at all -- they are not naming the same objects -- and the widest of the three
silently merges feeds. That is not hypothetical here:

    DDLBase Remux 4K      /cat/movie-remux-2160p    ddlbase:remux
    DDLBase Remux 1080p   /cat/movie-remux-1080p    ddlbase:remux

Two distinct listings collapse into one ledger arm, so the second endpoint's
claim about a release is dropped as a repeat of the first, and a coverage proof
for either would be joined to a mixture of both.

This module is the single definition. It imports nothing from the crawler, the
database or the evaluator, so all three can depend on it without a cycle.

An ArmSpec describes a feed. It does NOT grant anything: naming an arm is not
evidence about it, and a registry entry is not an ordering contract.
"""
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

#: Legacy two-part keys written before round 19. Kept so the ledger written by
#: the deployed container can still be READ and migrated, never so that new
#: rows can be written in the old shape.
LEGACY_KEY_PARTS = 2


def endpoint_slug(base_url: object) -> str:
    """The last path segment of a listing's base URL.

    Distinguishes two feeds of the same source and category, which is precisely
    what the ledger key could not do.
    """
    parts = [x for x in str(base_url or "").split("/") if x]
    return parts[-1].strip().lower() if parts else "root"


@dataclass(frozen=True)
class ArmSpec:
    """One listing feed: where it is read from, and what it claims to list."""

    source: str
    category: str
    endpoint: str
    listing_type: str = ""

    @property
    def arm_key(self) -> str:
        return "%s:%s:%s" % (self.source, self.category, self.endpoint)

    @property
    def legacy_key(self) -> str:
        """What the pre-round-19 ledger would have called this arm."""
        return ("%s:%s" % (self.source, self.category)
                if self.category else self.source)


def spec_from_descriptor(descriptor: Mapping) -> ArmSpec:
    """Build a spec from one of `_build_sources()`'s dictionaries."""
    return ArmSpec(
        source=str(descriptor.get("source") or "hdencode").strip().lower(),
        category=str(descriptor.get("category") or "").strip().lower(),
        endpoint=endpoint_slug(descriptor.get("base")),
        listing_type=str(descriptor.get("type") or "").strip().lower(),
    )


def arm_key_from_descriptor(descriptor: Mapping) -> str:
    return spec_from_descriptor(descriptor).arm_key


class ArmKeyCollision(ValueError):
    """Two descriptors resolve to one arm key.

    Refused rather than merged. A merge is exactly the defect this module
    exists to remove, and it would be invisible: the crawl would run, the
    ledger would fill, and two feeds would quietly share one identity.
    """


class ArmRegistry:
    """The arms a crawl knows about, and the legacy keys they supersede."""

    def __init__(self, specs: Iterable[ArmSpec]):
        self._specs: Dict[str, ArmSpec] = {}
        for spec in specs:
            existing = self._specs.get(spec.arm_key)
            if existing is not None and existing != spec:
                raise ArmKeyCollision(
                    "two feeds resolve to arm key %r: %r and %r -- give them "
                    "distinct base URLs rather than letting one speak for both"
                    % (spec.arm_key, existing, spec))
            self._specs[spec.arm_key] = spec

    @classmethod
    def from_descriptors(cls, descriptors: Iterable[Mapping]) -> "ArmRegistry":
        return cls(spec_from_descriptor(d) for d in descriptors)

    def __len__(self) -> int:
        return len(self._specs)

    def __contains__(self, arm_key: object) -> bool:
        return str(arm_key) in self._specs

    def get(self, arm_key: str) -> Optional[ArmSpec]:
        return self._specs.get(str(arm_key))

    def keys(self) -> List[str]:
        return sorted(self._specs)

    def specs(self) -> List[ArmSpec]:
        return [self._specs[k] for k in sorted(self._specs)]

    # -- migration ---------------------------------------------------------

    def resolve_legacy(self, legacy_key: str) -> Optional[str]:
        """The modern arm key a pre-round-19 ledger key refers to, or None.

        Returns None when the answer is not KNOWN, not when it is merely hard:

          * the legacy key matches no registered arm -- a feed that no longer
            exists, whose rows nobody can attribute
          * the legacy key matches MORE THAN ONE arm -- `ddlbase:remux` is both
            the 2160p and the 1080p feed, and picking either would fabricate an
            attribution that the old row never carried

        A None result means the rows stay where they are and stay legacy. That
        loses nothing: a legacy row is still a true record of a sighting. What
        it must not do is acquire a precision it never had.
        """
        want = str(legacy_key or "").strip().lower()
        if not want:
            return None
        matches = [s for s in self._specs.values() if s.legacy_key == want]
        if len(matches) != 1:
            return None
        return matches[0].arm_key

    def legacy_migration_plan(
            self, legacy_keys: Iterable[str]
    ) -> Tuple[Dict[str, str], List[str]]:
        """(rewrites, unresolved) for a set of keys found in the ledger.

        Keys that are already modern are neither rewritten nor reported as
        unresolved -- they are simply done.
        """
        rewrites: Dict[str, str] = {}
        unresolved: List[str] = []
        for key in legacy_keys:
            k = str(key or "").strip().lower()
            if not k or k in self._specs:
                continue
            target = self.resolve_legacy(k)
            if target is None:
                unresolved.append(k)
            else:
                rewrites[k] = target
        return rewrites, sorted(set(unresolved))


#: The category used by Site Search. Its base URL is the configured HOST, so
#: its endpoint slug varies with configuration and it cannot appear in a static
#: table. Excluded deliberately: a search is a one-off user action, not a
#: scheduled feed, and no coverage argument is ever made from one.
SEARCH_CATEGORY = "search"

#: Every scheduled crawl feed the code can produce.
#:
#: Static, and safe to be static: `endpoint_slug` takes the LAST path segment,
#: and every scheduled feed's path is a literal in `_build_sources` even when
#: its host is configurable. `tests/test_round19_one_arm_identity.py` asserts
#: this table against what `_build_sources` actually emits, so a feed added
#: there and not here fails a test rather than silently becoming unmigratable.
#:
#: This is the ONLY complete view. `_build_sources` returns the feeds selected
#: for ONE scan, gated by source type and per-category flags -- and a migration
#: built from a partial view is worse than none: with only the 2160p remux feed
#: selected, the ambiguous legacy key `ddlbase:remux` would resolve cleanly to
#: it, inventing an attribution for rows that may have come from the 1080p feed.
KNOWN_ARMS = (
    ArmSpec("hdencode", "4k", "2160p", "movie"),
    ArmSpec("hdencode", "remux", "remux", "movie"),
    ArmSpec("hdencode", "tv", "tv-packs", "tv"),
    ArmSpec("ddlbase", "4k", "movie-webdl-2160p", "movie"),
    ArmSpec("ddlbase", "remux", "movie-remux-2160p", "movie"),
    ArmSpec("ddlbase", "remux", "movie-remux-1080p", "movie"),
    ArmSpec("adithd", "4k", "4k-uhd-movies", "movie"),
    ArmSpec("adithd", "remux", "remux-movies", "movie"),
    ArmSpec("adithd", "tv", "tv-packs", "tv"),
)


def default_registry() -> "ArmRegistry":
    """The registry a migration must use: every feed, regardless of what is
    enabled or selected right now."""
    return ArmRegistry(KNOWN_ARMS)
