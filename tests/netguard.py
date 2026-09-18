"""Keep the AI test modules off the network.

Every provider call in these tests is stubbed, so a real connection means a stray
unpatched code path. Letting one through is worse than a failing test: it can load
a multi-gigabyte model into a local Ollama server, or spend real money with a
hosted provider, while the suite reports success and pins the machine it runs on.

Each AI test module calls :func:`block` from ``setUpModule``, so that mistake
fails immediately and loudly instead of quietly hammering the computer.

This is test-only tooling and is deliberately not importable by the app.
"""

import socket

_MESSAGE = (
    "this test tried to open a connection to {address}. Stub the provider call "
    "instead of reaching the network."
)

_real_connect = None
_real_create_connection = None


def block():
    """Make every outbound connection raise until :func:`restore` is called."""
    global _real_connect, _real_create_connection
    if _real_connect is not None:
        return
    _real_connect = socket.socket.connect
    _real_create_connection = socket.create_connection

    def blocked_connect(sock, address, *args, **kwargs):
        raise AssertionError(_MESSAGE.format(address=address))

    def blocked_create_connection(address, *args, **kwargs):
        raise AssertionError(_MESSAGE.format(address=address))

    # urllib3 reaches a host through one of these two, and requests sits on top
    # of urllib3, so blocking both covers every provider.
    socket.socket.connect = blocked_connect
    socket.create_connection = blocked_create_connection


def restore():
    """Put the real connecting functions back."""
    global _real_connect, _real_create_connection
    if _real_connect is None:
        return
    socket.socket.connect = _real_connect
    socket.create_connection = _real_create_connection
    _real_connect = None
    _real_create_connection = None
