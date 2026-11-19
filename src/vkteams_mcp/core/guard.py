"""The one gate in front of everything other people see: the person's word, the size of the action,
and one bucket per person shared by every kind of action."""

from __future__ import annotations

from vkteams_mcp import observability
from vkteams_mcp.core.errors import VKError
from vkteams_mcp.core.state import StateStore, StateUnavailable

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


class Guard:
    """The bucket lives in the state store, so every replica draws on the same one: a person cannot send
    ten messages through each of three replicas. With the store down nothing is sent (the refusal says so)."""

    def __init__(self, store: StateStore) -> None:
        self._store = store

    async def _take(self, person: str, kind: str) -> int:
        try:
            b = await self._store.bucket_take("bucket", person, CAPACITY, REFILL_PER_SECOND)
        except StateUnavailable:
            observability.inc(observability.GUARD_REFUSALS, "state")
            raise VKError("state_down", outcome=UNDONE.get(kind, "Nothing was done.")) from None
        if not b.ok:
            observability.inc(observability.GUARD_REFUSALS, "rate")
            raise VKError(
                "rate",
                seconds=b.wait,
                capacity=CAPACITY,
                every=round(1 / REFILL_PER_SECOND),
                then=RATE_THEN.get(kind, UNDONE.get(kind, "Nothing was done.")),
            )
        return b.left

    async def spend(self, person: str, *, kind: str) -> dict:
        """An action only the person sees (a new group with no one else in it): no confirmation, but it still
        draws on the same allowance, so a runaway agent cannot create without limit."""
        return {"sends_left": await self._take(person, kind)}

    async def check(
        self, person: str, *, kind: str, user_confirmed: bool | None, people: int = 0, target: str | None = None
    ) -> dict:
        """Call before the upstream request. Returns the receipt the tool puts in its result."""
        if user_confirmed is not True:
            observability.inc(observability.GUARD_REFUSALS, "confirm")
            raise VKError("confirm", target=target, outcome=UNDONE.get(kind, "Nothing was done."))
        if people > MAX_PEOPLE:
            observability.inc(observability.GUARD_REFUSALS, "too_many")
            raise VKError("too_many", limit=MAX_PEOPLE, what="people", count=people)
        return {"sends_left": await self._take(person, kind)}
