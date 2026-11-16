"""The one gate in front of everything other people see: the person's word, the size of the action,
and one bucket per person shared by every kind of action."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from vkteams_mcp.core.errors import VKError

MAX_PEOPLE = 20
CAPACITY = 10
REFILL_PER_SECOND = 10 / 60

# What a refusal says did not happen, by the kind of action.
UNDONE = {
    "message": "Nothing was sent.",
    "change": "Nothing was changed.",
    "members": "Nothing was changed.",
    "call": "No one was invited.",
    "bot": "The bot was not contacted.",
    "leave": "You are still in the chat.",
    "chat": "Nothing was changed.",
}
# What a refusal by the rate says happens next.
RATE_THEN = {"message": "Nothing was sent and the draft is kept: call send_draft again later; do not save it again."}


@dataclass
class _Bucket:
    tokens: float
    at: float


class Guard:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}

    def _take(self, person: str, kind: str) -> int:
        now = self._clock()
        b = self._buckets.get(person)
        if b is None:
            b = self._buckets[person] = _Bucket(CAPACITY, now)
        b.tokens = min(CAPACITY, b.tokens + (now - b.at) * REFILL_PER_SECOND)
        b.at = now
        if b.tokens < 1:
            wait = math.ceil((1 - b.tokens) / REFILL_PER_SECOND)
            raise VKError(
                "rate",
                seconds=wait,
                capacity=CAPACITY,
                every=round(1 / REFILL_PER_SECOND),
                then=RATE_THEN.get(kind, UNDONE.get(kind, "Nothing was done.")),
            )
        b.tokens -= 1
        return int(b.tokens)

    def spend(self, person: str, *, kind: str) -> dict:
        """An action only the person sees (a new group with no one else in it): no confirmation, but it still
        draws on the same allowance, so a runaway agent cannot create without limit."""
        return {"sends_left": self._take(person, kind)}

    def check(
        self, person: str, *, kind: str, user_confirmed: bool | None, people: int = 0, target: str | None = None
    ) -> dict:
        """Call before the upstream request. Returns the receipt the tool puts in its result."""
        if user_confirmed is not True:
            raise VKError("confirm", target=target, outcome=UNDONE.get(kind, "Nothing was done."))
        if people > MAX_PEOPLE:
            raise VKError("too_many", limit=MAX_PEOPLE, what="people", count=people)
        return {"sends_left": self._take(person, kind)}
