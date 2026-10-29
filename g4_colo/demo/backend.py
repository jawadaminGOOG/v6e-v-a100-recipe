"""Backend selection shared by the web app, scenarios and the CLI."""

from __future__ import annotations

import os

_backend = None


def get_backend():
    global _backend
    if _backend is None:
        if os.environ.get("DEMO_MOCK", "0") == "1":
            from mock import MockBackend

            _backend = MockBackend()
        else:
            from cluster import LiveBackend

            _backend = LiveBackend(context=os.environ.get("DEMO_KUBE_CONTEXT") or None)
    return _backend
