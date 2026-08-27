"""Package and IPC protocol versions shared by CLI, client, and daemon."""

VERSION = "0.1.0"
#: Bumped to 2 when the `adopt` meta's compatibility fallback was deleted. `adopt` itself
#: shipped under protocol 1, so a daemon predating it passed the handshake and then refused
#: the meta — which the client answered by silently degrading to the client-side scan that
#: `adopt` exists to replace. A version is the honest way to say "restart me".
PROTOCOL_VERSION = 2
