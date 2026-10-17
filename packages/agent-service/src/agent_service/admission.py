"""The service's one admission slot: at most one subscription run is active at a time.

A ``Lease`` is the holder's proof. It frees the slot once, and only while it still
holds it, so a late release by an old response never frees the slot of a newer run.
"""

from __future__ import annotations


class AdmissionSlot:
    def __init__(self) -> None:
        self._holder: Lease | None = None

    def try_take(self) -> Lease | None:
        if self._holder is not None:
            return None
        self._holder = Lease(self)
        return self._holder

    def free(self, lease: Lease) -> None:
        if self._holder is lease:
            self._holder = None


class Lease:
    def __init__(self, slot: AdmissionSlot) -> None:
        self._slot = slot

    def release(self) -> None:
        self._slot.free(self)
