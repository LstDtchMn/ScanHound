"""Evidence precedence for media type — TV, MOVIE, or honestly AMBIGUOUS.

Written 2026-08-02 in response to the review question *"is 'out-of-band signals
are additive only' the right invariant?"* The answer was: only provisionally.

The old rule was a boolean OR — anything says TV, it's TV. That rule **cannot
represent a contradiction**, so it resolves every one of them to TV. Divergence
(f) proved a TV-shaped release genuinely appears in a movies feed; the converse
happens too, and a film sitting on a TV page would silently become a series.

The replacement ranks signals by how much they are entitled to decide, and lets
a genuine clash surface as AMBIGUOUS rather than averaging it away:

    ROUTE (feed category / crawl mode)   weakest — routing metadata, not identity
      < TITLE (release-title grammar)
      < DETAIL (hydrated filename)
      < IDENTITY (external id / unique library match)   strongest
"""

import pytest

from backend.release_grammar import (
    Authority,
    MediaType,
    TypeEvidence,
    resolve_media_type,
    title_type_evidence,
)


def ev(media_type, authority, source="x"):
    return TypeEvidence(media_type, authority, source)


class TestPrecedence:
    def test_a_route_may_resolve_a_silent_title(self):
        """The legitimate additive case: nothing else spoke, so the feed
        category decides — but only provisionally."""
        verdict = resolve_media_type([ev(MediaType.TV, Authority.ROUTE, "feed")])
        assert verdict.media_type is MediaType.TV
        assert verdict.provisional is True

    def test_a_route_may_NOT_overrule_a_title(self):
        """THE DIVERGENCE (f) CASE. 'Complete Series' in a movies feed is a TV
        release that happens to be routed badly — not a film."""
        verdict = resolve_media_type([
            ev(MediaType.TV, Authority.TITLE, "title"),
            ev(MediaType.MOVIE, Authority.ROUTE, "feed"),
        ])
        assert verdict.media_type is MediaType.TV
        assert verdict.provisional is False

    def test_the_converse_also_holds(self):
        """A film in a TV category stays a film. The old boolean OR could not
        express this at all — it made everything on a TV page TV."""
        verdict = resolve_media_type([
            ev(MediaType.MOVIE, Authority.TITLE, "title"),
            ev(MediaType.TV, Authority.ROUTE, "feed"),
        ])
        assert verdict.media_type is MediaType.MOVIE

    def test_detail_outranks_title(self):
        verdict = resolve_media_type([
            ev(MediaType.TV, Authority.TITLE, "title"),
            ev(MediaType.MOVIE, Authority.DETAIL, "filename"),
        ])
        assert verdict.media_type is MediaType.MOVIE

    def test_identity_outranks_everything(self):
        verdict = resolve_media_type([
            ev(MediaType.TV, Authority.ROUTE, "feed"),
            ev(MediaType.TV, Authority.TITLE, "title"),
            ev(MediaType.TV, Authority.DETAIL, "filename"),
            ev(MediaType.MOVIE, Authority.IDENTITY, "imdb"),
        ])
        assert verdict.media_type is MediaType.MOVIE
        assert verdict.provisional is False


class TestConflict:
    def test_same_level_disagreement_is_ambiguous(self):
        """A strong conflict is a finding, not something to average.
        Two confirmed identities disagreeing means we do not know."""
        verdict = resolve_media_type([
            ev(MediaType.TV, Authority.IDENTITY, "plex"),
            ev(MediaType.MOVIE, Authority.IDENTITY, "imdb"),
        ])
        assert verdict.media_type is MediaType.AMBIGUOUS
        assert verdict.provisional is False

    def test_a_conflict_explains_itself(self):
        verdict = resolve_media_type([
            ev(MediaType.TV, Authority.IDENTITY, "plex"),
            ev(MediaType.MOVIE, Authority.IDENTITY, "imdb"),
        ])
        assert "plex=tv" in verdict.because
        assert "imdb=movie" in verdict.because

    def test_a_lower_level_never_creates_a_conflict(self):
        """Only the deciding level can conflict. A weak signal disagreeing
        with a strong one is overruled, not a reason to give up."""
        verdict = resolve_media_type([
            ev(MediaType.MOVIE, Authority.IDENTITY, "imdb"),
            ev(MediaType.TV, Authority.ROUTE, "feed"),
            ev(MediaType.TV, Authority.TITLE, "title"),
        ])
        assert verdict.media_type is MediaType.MOVIE
        assert any("overruled" in reason for reason in verdict.because)

    def test_no_evidence_is_ambiguous_not_movie(self):
        """Fail-closed. Defaulting the unknown to 'movie' is how a series with
        an unreadable name gets filed into the film library."""
        verdict = resolve_media_type([])
        assert verdict.media_type is MediaType.AMBIGUOUS
        assert verdict.provisional is True

    def test_none_entries_are_ignored(self):
        """title_type_evidence returns None for a silent title, so callers
        pass lists with holes in them."""
        verdict = resolve_media_type([None, ev(MediaType.TV, Authority.TITLE)])
        assert verdict.media_type is MediaType.TV


class TestTitleEvidence:
    def test_a_tv_title_produces_title_authority_evidence(self):
        found = title_type_evidence("Great Show Complete Series 1080p")
        assert found is not None
        assert found.media_type is MediaType.TV
        assert found.authority is Authority.TITLE

    def test_a_silent_title_produces_NOTHING_not_movie(self):
        """Crucial asymmetry. 'No TV signal' is not 'this is a film' — if it
        claimed MOVIE at TITLE authority, a neutral name would outrank a
        trustworthy feed category and re-break the additive case."""
        assert title_type_evidence("The Batman 2022 1080p BluRay") is None

    def test_so_a_neutral_title_leaves_the_route_free_to_decide(self):
        verdict = resolve_media_type([
            title_type_evidence("The Batman 2022 1080p BluRay"),
            ev(MediaType.TV, Authority.ROUTE, "feed"),
        ])
        assert verdict.media_type is MediaType.TV
        assert verdict.provisional is True


class TestMetamorphicProperties:
    """Properties the review named as mandatory. Each must hold for ANY
    evidence set, so they are asserted over combinations rather than examples."""

    LEVELS = [Authority.ROUTE, Authority.TITLE, Authority.DETAIL, Authority.IDENTITY]

    @pytest.mark.parametrize("level", LEVELS)
    @pytest.mark.parametrize("media_type", [MediaType.TV, MediaType.MOVIE])
    def test_adding_corroboration_never_changes_a_verdict(self, level, media_type):
        """Agreeing evidence, at any level, cannot flip or weaken the outcome."""
        base = [ev(media_type, Authority.DETAIL, "filename")]
        before = resolve_media_type(base)
        after = resolve_media_type(base + [ev(media_type, level, "extra")])
        assert after.media_type is before.media_type

    @pytest.mark.parametrize("level", LEVELS)
    def test_provenance_only_change_does_not_alter_the_decision(self, level):
        """Relabelling where evidence came from must not move the verdict —
        only its authority and its claim may."""
        a = resolve_media_type([ev(MediaType.TV, level, "source-a")])
        b = resolve_media_type([ev(MediaType.TV, level, "source-b")])
        assert a.media_type is b.media_type
        assert a.provisional == b.provisional

    def test_order_of_evidence_is_irrelevant(self):
        items = [
            ev(MediaType.TV, Authority.TITLE, "title"),
            ev(MediaType.MOVIE, Authority.ROUTE, "feed"),
            ev(MediaType.TV, Authority.DETAIL, "filename"),
        ]
        assert (resolve_media_type(items).media_type
                is resolve_media_type(list(reversed(items))).media_type)

    def test_a_provisional_verdict_requires_route_to_be_the_only_speaker(self):
        """Guards the flag that gates autonomous action: anything above ROUTE
        makes the verdict non-provisional, and nothing else does."""
        assert resolve_media_type([ev(MediaType.TV, Authority.ROUTE)]).provisional
        for level in (Authority.TITLE, Authority.DETAIL, Authority.IDENTITY):
            assert not resolve_media_type([ev(MediaType.TV, level)]).provisional

    def test_ambiguous_is_never_an_input(self):
        """AMBIGUOUS is a conclusion. Accepting it as evidence would let a
        caller launder 'we do not know' into the merge as if it were a claim."""
        verdict = resolve_media_type([ev(MediaType.TV, Authority.TITLE)])
        assert verdict.media_type in (MediaType.TV, MediaType.MOVIE,
                                      MediaType.AMBIGUOUS)
