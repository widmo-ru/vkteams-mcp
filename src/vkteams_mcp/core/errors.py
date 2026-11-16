"""The one error type a tool raises, and the sentences a person reads.

`str()` of an error is its key alone. Tracebacks and log lines print `str()`, and the parameters
(names, chat titles, counts) are the person's data, so they reach the model through the result
and never the log.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

# key -> sentence. Every sentence names the next step, or says what did not happen.
MESSAGES: dict[str, str] = {
    "reconnect": (
        "VK Teams is not signed in on this connector, or the session ended. Sign in to VK Teams again "
        "in the connector settings. Nothing was done."
    ),
    "area_off": "The {area} tools are switched off on this server.",
    "forbidden": (
        "VK Teams does not let you do this: you are not a member, or you lack the rights. Nothing was changed."
    ),
    "not_found": "It does not exist, or you cannot see it. Find it again with the tool that lists it.",
    "throttled": "VK Teams is limiting requests right now. Try again in {seconds}.",
    "failed": "VK Teams refused the request ({detail}), and nothing was changed. Check the values given.",
    "uncertain": "VK Teams failed while handling this, so it may or may not have happened. Check before trying again.",
    "failed_read": "VK Teams failed while reading this; nothing was changed. Try again in a moment.",
    "ambiguous": 'More than one {kind} matches "{query}". Ask which one is meant: {candidates}.',
    "unresolved": 'No {kind} matches "{query}". {where}',
    "confirm": (
        "Other people see this at once. Ask the person to confirm{target}, then call again with "
        "user_confirmed=true. {outcome}"
    ),
    "too_many": "At most {limit} {what} per action; this has {count}. Nothing was done.",
    "rate": (
        "Too many actions that other people see, in a short time. The allowance is the person's: {capacity} "
        "at once, then one every {every} seconds. Wait {seconds}. {then}"
    ),
    "not_own_message": "Only your own messages can be edited or deleted. Nothing was changed.",
    "no_draft": (
        "That is not a draft save_draft made for this chat, or it is older than 24 hours. Save it again with "
        "save_draft. Nothing was sent."
    ),
    "too_large": "It is too large: {size} against a limit of {limit}. Nothing was done.",
    "invalid": "{reason}",
    "unsupported": "{reason}",
    "unexpected": "Something went wrong on our side; check whether the change happened before trying again.",
}

# Where to look when a name matched nothing.
WHERE = {
    "chat": "list_chats with a query finds chats by part of their title.",
    "person": "find_people finds people by part of a name; an e-mail address always works.",
    "message": "list_messages shows the chat's messages with their ids.",
    "folder": "list_chats with filter=folder lists the folders.",
}


def _sentence_params(key: str, params: dict[str, Any]) -> dict[str, Any]:
    out = dict(params)
    if "seconds" in out:
        out["seconds"] = f"{out['seconds']} second" + ("" if out["seconds"] == 1 else "s")
    if key == "confirm":
        out.setdefault("outcome", "Nothing was sent.")
        out["target"] = f" ({out['target']})" if out.get("target") else ""
    if key == "rate":
        out.setdefault("then", "Nothing was sent.")
    if key == "unresolved":
        out["where"] = WHERE.get(str(out.get("kind")), "Check the spelling.")
    return out


class VKError(ToolError):
    """A refusal or failure the person should read. `isError` is set by the SDK."""

    def __init__(self, key: str, **params: Any) -> None:
        if key not in MESSAGES:
            raise KeyError(f"unknown error key {key!r}")
        self.key = key
        self.params = params
        super().__init__(key)
        self._text = MESSAGES[key].format(**_sentence_params(key, params))

    def __str__(self) -> str:
        return self.key

    @property
    def text(self) -> str:
        return self._text


def invalid(reason: str) -> VKError:
    return VKError("invalid", reason=reason)


def unsupported(reason: str) -> VKError:
    return VKError("unsupported", reason=reason)
