"""Package and IPC protocol versions shared by CLI, client, and daemon."""

from hashlib import sha256
from pathlib import Path

VERSION = "0.1.0"
#: Bumped to 2 when the `adopt` meta's compatibility fallback was deleted. `adopt` itself
#: shipped under protocol 1, so a daemon predating it passed the handshake and then refused
#: the meta — which the client answered by silently degrading to the client-side scan that
#: `adopt` exists to replace. A version is the honest way to say "restart me".
PROTOCOL_VERSION = 2


def source_digest() -> str:
    """Identify this installed core, including edits that keep the package version."""
    root = Path(__file__).parent
    digest = sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
