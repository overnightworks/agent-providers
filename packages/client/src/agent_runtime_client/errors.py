"""The one way the client and the provider core raise an error that carries no chain."""

from __future__ import annotations

from typing import NoReturn


def raise_without_chain(error: BaseException) -> NoReturn:
    """Raise ``error`` with no ``__context__`` or ``__cause__``, even from a handler.

    An exception raised while the caller handles another one is chained to it
    implicitly, and a chain can carry the very text the error must not.
    """
    try:
        raise error
    except BaseException:
        error.__context__ = None
        error.__cause__ = None
        raise
