import pytest

from public_research_os.publication.port import DisabledPublisher


def test_no_live_publishing_is_possible(package):
    with pytest.raises(PermissionError, match="not implemented or authorized"):
        DisabledPublisher().submit(package)
