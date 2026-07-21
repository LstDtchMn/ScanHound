"""Qualified official HDEncode RSS feed registry."""
from dataclasses import dataclass


@dataclass(frozen=True)
class HDEncodeFeedSpec:
    key: str
    url: str
    role: str
    media_type: str
    expected_max_entries: int = 50


FEEDS = (
    HDEncodeFeedSpec("movies_all", "https://hdencode.org/tag/movies/feed/", "normal", "movie"),
    HDEncodeFeedSpec("tv_all", "https://hdencode.org/tag/tv-shows/feed/", "normal", "tv"),
    HDEncodeFeedSpec("movies_2160p", "https://hdencode.org/quality/2160p/feed/?tag=movies", "catchup", "movie"),
    HDEncodeFeedSpec("movies_1080p", "https://hdencode.org/quality/1080p/feed/?tag=movies", "catchup", "movie"),
    HDEncodeFeedSpec("movies_720p", "https://hdencode.org/quality/720p/feed/?tag=movies", "catchup", "movie"),
    HDEncodeFeedSpec("movies_remux", "https://hdencode.org/quality/remux/feed/?tag=movies", "catchup", "movie"),
    HDEncodeFeedSpec("movies_bluray_disc", "https://hdencode.org/quality/full-blu-ray-disc/feed/?tag=movies", "catchup", "movie"),
    HDEncodeFeedSpec("tv_2160p", "https://hdencode.org/quality/2160p/feed/?tag=tv-shows", "catchup", "tv"),
    HDEncodeFeedSpec("tv_1080p", "https://hdencode.org/quality/1080p/feed/?tag=tv-shows", "catchup", "tv"),
    HDEncodeFeedSpec("tv_720p", "https://hdencode.org/quality/720p/feed/?tag=tv-shows", "catchup", "tv"),
    HDEncodeFeedSpec("tv_webdl", "https://hdencode.org/quality/web-dl/feed/?tag=tv-shows", "catchup", "tv"),
    HDEncodeFeedSpec("tv_webrip", "https://hdencode.org/quality/webrip/feed/?tag=tv-shows", "catchup", "tv"),
)
_BY_KEY = {feed.key: feed for feed in FEEDS}


def get_feed(key):
    try:
        return _BY_KEY[key]
    except KeyError as exc:
        raise ValueError(f"Unknown HDEncode RSS feed: {key}") from exc


def normal_feeds():
    return tuple(feed for feed in FEEDS if feed.role == "normal")


def catchup_feeds():
    return tuple(feed for feed in FEEDS if feed.role == "catchup")
