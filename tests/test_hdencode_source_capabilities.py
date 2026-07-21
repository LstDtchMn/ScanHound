"""HDEncode plugin capabilities must match its fail-closed direct-link method."""
from backend.sources.base import SourceCapability
from backend.sources.hdencode import HDEncodeSource


def test_hdencode_plugin_does_not_claim_unimplemented_direct_links():
    config = HDEncodeSource.get_config()
    assert not (config.capabilities & SourceCapability.DIRECT_LINKS)
    assert not (config.capabilities & SourceCapability.CLOUDFLARE_BYPASS)
    assert config.requires_cloudflare_bypass is False
