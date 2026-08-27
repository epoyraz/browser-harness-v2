"""Job-application workflow built on the browser harness, kept out of it.

Import what you need — `from applications import run_application` — or merge the whole
surface into a `bh` script namespace with `install(globals())`. The harness has no
knowledge of this package, so a core-only measurement stays honest.
"""
from __future__ import annotations

from functools import partial
from typing import Any

from applications.state import wait_for_application_state
from applications.workflow import (
    application_skills,
    follow_application,
    locate_application,
    prepare_application,
    run_application,
)

__all__ = ["application_skills", "follow_application", "install", "locate_application",
           "prepare_application", "run_application", "wait_for_application_state"]


def install(namespace: dict[str, Any]) -> list[str]:
    """Bind this layer into a `bh` script namespace, curried on its `session`.

    `bh` scripts used to call `prepare_application()` bare because it was a `Session`
    method. It is a function taking a session now, so binding it here keeps those scripts
    reading the same way without the harness having to know the domain exists.
    """
    session = namespace.get("session")
    if session is None:
        raise RuntimeError("install() needs a `bh` namespace holding `session`")
    bound = []
    for name in __all__:
        if name == "install":
            continue
        namespace[name] = partial(globals()[name], session)
        bound.append(name)
    return bound
