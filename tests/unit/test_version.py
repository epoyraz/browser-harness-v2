from harness.version import PROTOCOL_VERSION, VERSION


def test_release_and_protocol_versions_are_explicit():
    assert VERSION == "0.1.0"
    assert isinstance(PROTOCOL_VERSION, int) and PROTOCOL_VERSION >= 1
