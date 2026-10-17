"""The service's admission limit: at most N subscription runs are active at a time.

A ``Lease`` is the holder's proof of one place. It frees that place once, and only while
it still holds it, so a late release by an old response never frees the place of a newer run.
"""

from __future__ import annotations


class AdmissionSlot:
    def __init__(self, places: int) -> None:
        self._places = places
        self._holders: set[Lease] = set()

    def try_take(self) -> Lease | None:
        if len(self._holders) >= self._places:
            return None
        lease = Lease(self)
        self._holders.add(lease)
        return lease

    def free(self, lease: Lease) -> None:
        self._holders.discard(lease)


class Lease:
    def __init__(self, slot: AdmissionSlot) -> None:
        self._slot = slot

    def release(self) -> None:
        self._slot.free(self)
