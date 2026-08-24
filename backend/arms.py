"""Arm identity: one stable name, one immutable evidence revision.

Round 20. This replaces the round-19 three-part `arm_key` string after peer
review established that a stable name alone cannot be a durable evidence
identity.

WHY A NAME IS NOT ENOUGH
------------------------
Two request definitions can share one stable name:

    arm_id = arm.hdencode.4k-2160p
    v1     = /quality/2160p/?tag=movies
    v2     = /quality/2160p/?tag=restored-movies

The name is deliberately unchanged; the thing examined is different. Keying the
ledger on the name alone lets v2 writes refresh the v1 row, merge sightings and
dates, and erase which request definition made which claim. A contract that
correctly refuses v2 cannot repair evidence already aggregated across both.

The same applies to the parser: a listing-membership claim is *produced by* a
parser, so an observation must retain the parser version even when policy later
groups versions under one human-facing arm.

So there are two objects, and they are not interchangeable:

    ArmId        stable, declared, OPAQUE      -- what policy names
    ArmRevision  (arm_id, request_definition_version, parser_version)
                                               -- what evidence carries

`arm_id` is opaque. Nothing may split it, count its separators, or reconstruct
source/category from it. It uses dots rather than colons specifically so that
code still splitting on ':' -- the round-19 separator -- fails loudly instead of
silently producing a wrong answer. `tests/test_round20_arm_identity.py` carries
a static guard that greps the backend for exactly that.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

#: Bumped when the NORMALISER changes -- that is, when the same feed would
#: produce a different canonical form. Every stored digest carries it, so a
#: normaliser change invalidates contracts loudly instead of silently comparing
#: digests computed under different rules.
REQUEST_NORMALIZER_VERSION = "request-v1"


class PaginationForm(str, Enum):
    """How page N is built from the base, declared rather than executed.

    A serialised callable or function name would hash to something that changes
    on a rename and stays identical across a behaviour change -- exactly
    backwards. These are the branches `_crawl_pages` actually contains.

    The ADITHD form is why this belongs in the request definition at all: it
    DROPS the query suffix on page N. Two feeds identical in path and query but
    paginated this way are not the same request, and a two-branch summary of
    pagination would hide that.
    """

    #: page 1: {base}{suffix}   page N: {base}page/{n}/{suffix}
    BASE_PAGE_N_SLASH_SUFFIX = "base+page/N/+suffix"
    #: page 1: {base}{suffix}   page N: {base}/page/{n}{suffix}
    BASE_SLASH_PAGE_N_SUFFIX = "base+/page/N+suffix"
    #: page 1: {base}{suffix}   page N: {base}page/{n}/      SUFFIX DROPPED
    BASE_PAGE_N_SLASH_NO_SUFFIX = "base+page/N/+NO-suffix"


@dataclass(frozen=True)
class RequestDefinition:
    """Everything that can change which stream is selected, or its order.

    Anything omitted here is, by construction, being asserted not to affect the
    selected or ordered stream. That assertion is the point: the digest is only
    as honest as this field list.
    """

    method: str
    scheme: str
    host: str
    port: Optional[int]
    path: str
    query_suffix: str
    pagination: PaginationForm
    #: Present and empty by default. A future cookie/header mode that SELECTS
    #: content belongs here; one that merely authenticates does not.
    selecting_headers: Tuple[Tuple[str, str], ...] = ()

    def canonical(self) -> Dict[str, object]:
        """The exact structure that is hashed. Key order is fixed by sort_keys
        at serialisation, never by insertion order."""
        return {
            "normalizer": REQUEST_NORMALIZER_VERSION,
            "method": self.method.upper(),
            "scheme": self.scheme.lower(),
            "host": self.host.lower(),
            "port": self.port,
            "path": self.path,
            "query_suffix": self.query_suffix,
            "pagination": self.pagination.value,
            "selecting_headers": [list(h) for h in self.selecting_headers],
        }

    def preimage(self) -> str:
        """The canonical JSON, STORED beside the digest.

        A digest without its preimage is not auditable: when a normaliser change
        invalidates a contract, nothing would explain why. Peer review made this
        a requirement rather than a nicety.
        """
        return json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"))

    @property
    def version(self) -> str:
        return "%s:%s" % (
            REQUEST_NORMALIZER_VERSION,
            hashlib.sha256(self.preimage().encode("utf-8")).hexdigest(),
        )


@dataclass(frozen=True)
class ArmRevision:
    """What every durable observation and every proof carries.

    Frozen and hashable so it can key a dict and compare by value. Deliberately
    NOT a string: a string invites parsing, and this project already spent a
    round on identities that were parsed.
    """

    arm_id: str
    request_definition_version: str
    parser_version: str

    def as_row(self) -> Tuple[str, str, str]:
        return (self.arm_id, self.request_definition_version, self.parser_version)

    def __str__(self) -> str:  # diagnostics only; never parsed
        return "%s@%s/%s" % (
            self.arm_id, self.request_definition_version[:19], self.parser_version)


@dataclass(frozen=True)
class ArmSpec:
    """One declared feed. Describes; grants nothing.

    Naming an arm is not evidence about it, and a registry entry is not an
    ordering contract.
    """

    arm_id: str
    source: str
    category: str
    listing_type: str
    request: RequestDefinition
    parser_version: str
    #: Legacy keys this spec supersedes, for migration only. Never consulted at
    #: runtime -- a legacy key must not resolve to a live arm by accident.
    supersedes: Tuple[str, ...] = ()
    #: False for arms that exist but are not part of the scheduled crawl
    #: (Site Search). Excluded from migration resolution.
    scheduled: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "arm_id", self.arm_id.strip().lower())

    @property
    def revision(self) -> ArmRevision:
        return ArmRevision(
            arm_id=self.arm_id,
            request_definition_version=self.request.version,
            parser_version=self.parser_version,
        )


class ArmRegistryError(ValueError):
    """A registry that cannot be built is a configuration error, not a warning.

    Refused rather than merged. A silent merge is invisible: the crawl runs, the
    ledger fills, and two feeds quietly share one identity -- which is the exact
    defect this module exists to remove.
    """


class ArmRegistry:
    """The complete set of declared arms, validated as a whole.

    FOUR refusals, not one. Peer review found that duplicate-name checking alone
    leaves three other ways for two feeds to become one identity.
    """

    def __init__(self, specs: Iterable[ArmSpec]):
        self._by_id: Dict[str, ArmSpec] = {}
        by_request: Dict[str, str] = {}
        supersedes_seen: Dict[str, str] = {}

        for spec in specs:
            # 1. duplicate arm_id
            existing = self._by_id.get(spec.arm_id)
            if existing is not None:
                if existing == spec:
                    continue          # the same feed declared twice is a wart
                raise ArmRegistryError(
                    "two different feeds declare arm_id %r: %r and %r"
                    % (spec.arm_id, existing.request.preimage(),
                       spec.request.preimage()))

            # 2. duplicate request definition under two names
            rv = spec.request.version
            if rv in by_request:
                raise ArmRegistryError(
                    "arm_id %r and %r declare the SAME request definition %s; "
                    "one feed cannot have two identities"
                    % (spec.arm_id, by_request[rv], spec.request.preimage()))
            by_request[rv] = spec.arm_id

            # 3. ambiguous supersedes -- one legacy key claimed by two arms
            for legacy in spec.supersedes:
                key = legacy.strip().lower()
                if key in supersedes_seen:
                    raise ArmRegistryError(
                        "legacy key %r is claimed by both %r and %r; migration "
                        "cannot attribute its rows" % (key, supersedes_seen[key],
                                                       spec.arm_id))
                supersedes_seen[key] = spec.arm_id

            self._by_id[spec.arm_id] = spec

        # 4. a supersedes entry equal to a live arm_id
        #
        # Without this a legacy key silently resolves to a live arm at runtime
        # and its rows are stranded -- the failure is invisible because both
        # names look valid.
        for legacy, owner in supersedes_seen.items():
            if legacy in self._by_id:
                raise ArmRegistryError(
                    "%r is both a live arm_id and a legacy key superseded by "
                    "%r; that is ambiguous by construction" % (legacy, owner))

        self._supersedes = supersedes_seen

    # -- reading -----------------------------------------------------------

    def __len__(self) -> int:
        return len(self._by_id)

    def __contains__(self, arm_id: object) -> bool:
        return str(arm_id).strip().lower() in self._by_id

    def get(self, arm_id: str) -> Optional[ArmSpec]:
        return self._by_id.get(str(arm_id).strip().lower())

    def ids(self) -> List[str]:
        return sorted(self._by_id)

    def specs(self) -> List[ArmSpec]:
        return [self._by_id[k] for k in sorted(self._by_id)]

    def revision_of(self, arm_id: str) -> Optional[ArmRevision]:
        spec = self.get(arm_id)
        return spec.revision if spec else None

    def is_active_revision(self, revision: ArmRevision) -> bool:
        """Does this revision match a CURRENTLY declared arm?

        Evidence carrying a superseded revision is still evidence; it is simply
        not proof-eligible under the active registry.

        NOTE: `covers_release()` does not yet consult this. It still takes
        stable arm ids and can only refuse when one id appears under two
        revisions in a run; a LONE retired revision is not ambiguous inside the
        report and is not rejected. Resolving required ids to active revisions
        before the evaluator is R22-1, and is not done. An earlier version of
        this docstring claimed the comparison already happened.
        """
        spec = self.get(revision.arm_id)
        return bool(spec) and spec.revision == revision

    # -- migration ---------------------------------------------------------

    def resolve_legacy(self, legacy_key: str) -> Optional[ArmSpec]:
        """The arm a pre-round-20 ledger key belongs to, or None.

        None means the answer is not KNOWN, not that it is merely hard. A None
        result sends the rows to quarantine, where they remain true records of a
        sighting without acquiring an attribution they never carried.
        """
        key = str(legacy_key or "").strip().lower()
        if not key:
            return None
        owner = self._supersedes.get(key)
        if owner is None:
            return None
        spec = self._by_id[owner]
        return spec if spec.scheduled else None

    def legacy_migration_plan(
            self, legacy_keys: Iterable[str]
    ) -> Tuple[Dict[str, ArmSpec], List[str]]:
        """(resolvable, unresolvable) over keys actually present in the ledger.

        Keys already in the modern shape are neither -- they are done.
        """
        resolvable: Dict[str, ArmSpec] = {}
        unresolvable: List[str] = []
        for raw in legacy_keys:
            key = str(raw or "").strip().lower()
            if not key or key in self._by_id:
                continue
            spec = self.resolve_legacy(key)
            if spec is None:
                unresolvable.append(key)
            else:
                resolvable[key] = spec
        return resolvable, sorted(set(unresolvable))


# ---------------------------------------------------------------------------
# The declared arms
# ---------------------------------------------------------------------------

#: Site Search: its base URL is the configured HOST, so its request definition
#: is not static. Declared unscheduled and excluded from migration resolution.
SEARCH_CATEGORY = "search"

#: `select_posts/1` for every arm below. Justified for the migrated rows by
#: BYTE IDENTITY of the parser to commit ef2fb188, not by a recorded constant --
#: the deployed writer had no parser-version constant at all. The migration
#: audit records this as provenance_class='reconstructed'; see
#: docs/reviews/peer-rounds/ROUND-20-PRODUCER-AUDIT.md.
PARSER_SELECT_POSTS_1 = "select_posts/1"


def _hdencode(path: str, suffix: str) -> RequestDefinition:
    return RequestDefinition(
        method="GET", scheme="https", host="hdencode.org", port=None,
        path=path, query_suffix=suffix,
        pagination=PaginationForm.BASE_PAGE_N_SLASH_SUFFIX)


def _ddlbase(path: str) -> RequestDefinition:
    return RequestDefinition(
        method="GET", scheme="https", host="ddlbase.com", port=None,
        path=path, query_suffix="",
        pagination=PaginationForm.BASE_SLASH_PAGE_N_SUFFIX)


def _adithd(path: str) -> RequestDefinition:
    return RequestDefinition(
        method="GET", scheme="https", host="adit-hd.com", port=None,
        path=path, query_suffix="",
        pagination=PaginationForm.BASE_PAGE_N_SLASH_NO_SUFFIX)


KNOWN_ARMS: Tuple[ArmSpec, ...] = (
    ArmSpec(arm_id="arm.hdencode.4k-2160p", source="hdencode", category="4k",
            listing_type="movie", request=_hdencode("/quality/2160p/", "?tag=movies"),
            parser_version=PARSER_SELECT_POSTS_1, supersedes=("hdencode:4k",)),
    ArmSpec(arm_id="arm.hdencode.remux", source="hdencode", category="remux",
            listing_type="movie", request=_hdencode("/quality/remux/", "?tag=movies"),
            parser_version=PARSER_SELECT_POSTS_1, supersedes=("hdencode:remux",)),
    ArmSpec(arm_id="arm.hdencode.tv-packs", source="hdencode", category="tv",
            listing_type="tv", request=_hdencode("/tag/tv-packs/", ""),
            parser_version=PARSER_SELECT_POSTS_1, supersedes=("hdencode:tv",)),

    # ddlbase:remux is claimed by NEITHER of these. Two feeds share that legacy
    # key, so it is unresolvable BY DESIGN and its rows quarantine permanently.
    ArmSpec(arm_id="arm.ddlbase.webdl-4k", source="ddlbase", category="4k",
            listing_type="movie", request=_ddlbase("/cat/movie-webdl-2160p"),
            parser_version=PARSER_SELECT_POSTS_1),
    ArmSpec(arm_id="arm.ddlbase.remux-4k", source="ddlbase", category="remux",
            listing_type="movie", request=_ddlbase("/cat/movie-remux-2160p"),
            parser_version=PARSER_SELECT_POSTS_1),
    ArmSpec(arm_id="arm.ddlbase.remux-1080p", source="ddlbase", category="remux",
            listing_type="movie", request=_ddlbase("/cat/movie-remux-1080p"),
            parser_version=PARSER_SELECT_POSTS_1),

    ArmSpec(arm_id="arm.adithd.4k", source="adithd", category="4k",
            listing_type="movie", request=_adithd("/forums/4k-uhd-movies/"),
            parser_version=PARSER_SELECT_POSTS_1),
    ArmSpec(arm_id="arm.adithd.remux", source="adithd", category="remux",
            listing_type="movie", request=_adithd("/forums/remux-movies/"),
            parser_version=PARSER_SELECT_POSTS_1),
    ArmSpec(arm_id="arm.adithd.tv-packs", source="adithd", category="tv",
            listing_type="tv", request=_adithd("/forums/tv-packs/"),
            parser_version=PARSER_SELECT_POSTS_1),
)


class UndeclaredArmRequired(ArmRegistryError):
    """Policy named an arm the registry does not declare.

    Fail closed. A requirement naming something undeclared cannot be satisfied
    by any evidence, and silently dropping it from the required set would make
    the remaining proof look complete.
    """


def active_revisions_for(arm_ids, registry=None):
    """Stable policy ids -> the exact revisions currently required.

    THE RESOLUTION BOUNDARY. Round 22 (R22-1).

    Policy names arms by stable id, because that is the thing a human decides
    about. A coverage proof, however, belongs to a REVISION. Something has to
    turn one into the other, and where it happens matters:

      * inside the evaluator, it would give `coverage.py` a dependency on the
        registry and make a pure function consult global declaration state;
      * left undone, the evaluator can only compare stable ids, so a proof for
        a RETIRED revision satisfies a requirement for the active one -- and a
        lone retired revision is not ambiguous inside the report, so the
        duplicate guard never fires.

    So it happens HERE, and the evaluator receives exact revisions as data.

    Returns a list of `(arm_id, request_definition_version, parser_version)`
    tuples in the order given.
    """
    reg = registry if registry is not None else default_registry()
    out = []
    for arm_id in (arm_ids or ()):
        spec = reg.get(arm_id)
        if spec is None:
            raise UndeclaredArmRequired(
                "policy requires %r, which the registry does not declare; "
                "no evidence could satisfy it" % arm_id)
        out.append(spec.revision.as_row())
    return out


def default_registry() -> ArmRegistry:
    """The COMPLETE registry.

    Never build one from the sources selected for a single scan: a partial view
    resolves an ambiguous legacy key to whichever half it happens to know about,
    which fabricates an attribution instead of declining to guess.
    """
    return ArmRegistry(KNOWN_ARMS)


# ---------------------------------------------------------------------------
# Bridge: runtime descriptor -> declared arm
# ---------------------------------------------------------------------------
#
# `_build_sources()` produces dicts at runtime; the registry above is declared.
# Resolution runs descriptor -> RequestDefinition -> digest -> registry, so two
# feeds match only when the thing actually requested is the same. It is NOT a
# name comparison: hdencode's base URL comes from config, so pointing the app at
# a mirror changes the request definition and correctly fails to match the arm
# declared for the canonical host.

#: Mirrors the branches in `ScannerService._crawl_pages`. Drift here is a wrong
#: digest, not a crash, so `test_round20_arm_identity.py` reconstructs every
#: page-2 URL from these forms and asserts equality with the crawler's own
#: f-strings.
_PAGINATION_BY_SOURCE: Dict[str, PaginationForm] = {
    "ddlbase": PaginationForm.BASE_SLASH_PAGE_N_SUFFIX,
    "adithd": PaginationForm.BASE_PAGE_N_SLASH_NO_SUFFIX,
}
_PAGINATION_DEFAULT = PaginationForm.BASE_PAGE_N_SLASH_SUFFIX

#: Site Search collapses to ONE label instead of one per query.
#:
#: Its suffix carries the user's search text, so a per-request label would mint
#: an unbounded number of distinct strings. Safe to collapse only because this
#: is never an arm_id and never attributed: a search observation is recorded as
#: UNATTRIBUTED, so it can contradict but can never prove.
UNSCHEDULED_SEARCH_LABEL = "unscheduled:search"

#: Prefix for a feed the crawler produces but the registry does not declare.
#:
#: R21-6/R21-7. This used to be "arm.unregistered.<16 hex>", which was the same
#: type error as the phantom quarantine arm: a value that LOOKS like a valid
#: arm_id, is not in the registry, and destroys the distinction between "we know
#: this arm" and "we could not establish one". It is now unmistakably not an
#: arm_id -- it carries a colon, which declared ids never do -- and it never
#: reaches the arm_id column. It appears only as a coverage LABEL and as
#: provenance in legacy_arm_key.
#:
#: The digest is carried in FULL. Truncating to 64 bits bought nothing: the cost
#: of the whole digest is zero, and a collision here would merge evidence.
UNREGISTERED_PREFIX = "unregistered:"


def pagination_for_source(source_id: object) -> PaginationForm:
    return _PAGINATION_BY_SOURCE.get(
        str(source_id or "").strip().lower(), _PAGINATION_DEFAULT)


def build_page_url(request: RequestDefinition, base: str, page_num: int) -> str:
    """Build a page URL from the DECLARED form.

    THE crawler calls this. Round 21 (R21-8): the four branches used to live
    inline in `_crawl_pages` while the tests kept their own copy, so the two
    could drift into exactly the mismatch the request digest exists to catch.
    There is now one implementation, checked against literal golden vectors and
    against the URLs a real crawl actually requests.
    """
    if page_num == 1:
        return "%s%s" % (base, request.query_suffix)
    if request.pagination is PaginationForm.BASE_SLASH_PAGE_N_SUFFIX:
        return "%s/page/%d%s" % (base, page_num, request.query_suffix)
    if request.pagination is PaginationForm.BASE_PAGE_N_SLASH_NO_SUFFIX:
        return "%spage/%d/" % (base, page_num)
    return "%spage/%d/%s" % (base, page_num, request.query_suffix)


def request_definition_from_descriptor(descriptor: Mapping) -> RequestDefinition:
    """The RequestDefinition a runtime descriptor actually describes."""
    from urllib.parse import urlsplit

    parts = urlsplit(str(descriptor.get("base") or ""))
    return RequestDefinition(
        method="GET",
        scheme=parts.scheme or "https",
        host=parts.hostname or "",
        port=parts.port,
        path=parts.path or "/",
        query_suffix=str(descriptor.get("suffix") or ""),
        pagination=pagination_for_source(descriptor.get("source")),
    )


def resolve_descriptor(descriptor: Mapping,
                       registry: Optional[ArmRegistry] = None
                       ) -> Optional[ArmSpec]:
    """The declared arm this descriptor IS, or None.

    None is a real answer: an undeclared feed. It still crawls and still records
    sightings; those sightings are simply recorded as unattributed.
    """
    if str(descriptor.get("category") or "").strip().lower() == SEARCH_CATEGORY:
        return None
    reg = registry if registry is not None else default_registry()
    want = request_definition_from_descriptor(descriptor).version
    for spec in reg.specs():
        if spec.request.version != want:
            continue
        # THE SEMANTICS MUST AGREE, NOT ONLY THE REQUEST. Round 21 (R21-12).
        #
        # Matching the request digest alone says "this fetches the same bytes".
        # It does NOT say the descriptor means the same thing by them, and the
        # crawler goes on to build its traversal arm and its claim from the
        # DESCRIPTOR's own type/category fields rather than from the spec it
        # matched. So a descriptor with the real TV Packs URL and type "movie"
        # would resolve to arm.hdencode.tv-packs and then record listing_type
        # "movie" -- a declared arm id stamped on contradictory evidence.
        #
        # Refuse instead. An unrecognised descriptor is recorded unattributed
        # and can never prove anything, which is the safe direction; inventing
        # an attribution for a feed whose meaning we cannot confirm is not.
        for field, declared in (("source", spec.source),
                                ("category", spec.category),
                                ("type", spec.listing_type)):
            if str(descriptor.get(field) or "").strip().lower() != declared:
                return None
        return spec
    return None


def arm_label_from_descriptor(descriptor: Mapping,
                              registry: Optional[ArmRegistry] = None) -> str:
    """A stable COVERAGE label for any descriptor, declared or not.

    A label, not an identity. For a declared feed it equals the arm_id, so the
    traversal and the ledger name the same object. For anything else it is a
    deliberately non-arm_id string that must never be written to arm_id.

    NEVER raises. A crawl must not die because a feed was added to
    `_build_sources` and not declared here -- that trades a silent gap for an
    outage.
    """
    if str(descriptor.get("category") or "").strip().lower() == SEARCH_CATEGORY:
        return UNSCHEDULED_SEARCH_LABEL
    spec = resolve_descriptor(descriptor, registry)
    if spec is not None:
        return spec.arm_id
    return UNREGISTERED_PREFIX + request_definition_from_descriptor(
        descriptor).version


def is_arm_id(value: object) -> bool:
    """Is this string SHAPED like an arm id?

    A syntax check only. It answers "does this belong to our namespace", not
    "is this a real arm" -- see `is_declared_arm_id`, which is what the writer
    uses to decide attribution.
    """
    text = str(value or "")
    return bool(text) and text.startswith("arm.") and ":" not in text


#: Every declared id, as an immutable set. Built once: the writer consults it
#: per claim, and rebuilding a registry there would be wasteful for a value
#: that cannot change at runtime.
DECLARED_ARM_IDS = frozenset(s.arm_id for s in KNOWN_ARMS)


def is_declared_arm_id(value: object) -> bool:
    """Is this an arm the registry actually DECLARES?

    Round 22 (R22-3). The writer used the shape check above, so

        arm_id = arm.made.up
        request_definition_version = request-v1:anything
        parser_version = parser/whatever

    was stored as attributed and satisfied the CHECK constraint. That made the
    state boundary mean the wrong thing:

        attributed == the caller supplied something in our namespace

    rather than

        attributed == we established a DECLARED arm

    This deliberately does NOT ask whether the revision is active. Evidence
    from a retired request definition or an older parser is real evidence and
    must stay recordable; what must be established is only that the stable arm
    is one we declare.
    """
    return str(value or "").strip().lower() in DECLARED_ARM_IDS


def revision_from_descriptor(descriptor: Mapping,
                             parser_version: str,
                             registry: Optional[ArmRegistry] = None
                             ) -> Optional[ArmRevision]:
    """The full evidence identity, or None when the feed is not declared.

    None rather than a synthesised revision: a revision names a DECLARED arm at
    a known request definition, and manufacturing one for an undeclared feed
    would be exactly the "unknown attribution wearing a known type" defect.

    parser_version is passed in by the CALLER rather than read from the spec:
    the running parser's version is a fact about the process doing the reading,
    not about the declaration.
    """
    spec = resolve_descriptor(descriptor, registry)
    if spec is None:
        return None
    return ArmRevision(
        arm_id=spec.arm_id,
        request_definition_version=spec.request.version,
        parser_version=str(parser_version),
    )


#: Back-compat for `scanner_service.py`. Returns the coverage LABEL.
arm_key_from_descriptor = arm_label_from_descriptor

#: Round-19 name, kept so existing raises/excepts still bind.
ArmKeyCollision = ArmRegistryError
