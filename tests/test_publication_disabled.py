import pytest

from shipyap.publication.port import DisabledPublisher


def test_no_live_publishing_is_possible(package):
    with pytest.raises(PermissionError, match="not implemented or authorized"):
        DisabledPublisher().submit(package)
