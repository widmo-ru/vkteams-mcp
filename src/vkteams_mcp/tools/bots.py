"""bots: start a bot, press a button in a bot's message."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from vkteams_mcp.core.content import content_line
from vkteams_mcp.core.context import call
from vkteams_mcp.core.errors import invalid
from vkteams_mcp.registry import tool
from vkteams_mcp.tools._common import BUTTON_PATTERN, MSG_ID_PATTERN, button_token, fetch_message, keyboard, msg_id


@tool(area="bots", title="Talk to a bot", notifies_others=True)
async def respond_to_bot(
    bot: Annotated[str, Field(description="The bot's id or nick, as list_chats or find_people shows it.")],
    action: Annotated[
        Literal["start", "press"],
        Field(
            description="start: open the bot with an optional parameter; press: press a button of the bot's message."
        ),
    ],
    user_confirmed: Annotated[
        bool, Field(description="true when the person explicitly asked to start this bot or press this button.")
    ] = False,
    parameter: Annotated[str | None, Field(description="For start: the start parameter the bot expects.")] = None,
    message: Annotated[
        str | None, Field(pattern=MSG_ID_PATTERN, description="For press: the id of the bot's message with the button.")
    ] = None,
    button: Annotated[
        str | None,
        Field(
            pattern=BUTTON_PATTERN,
            description='For press: the button exactly as list_messages shows it, such as "1.a3f9c2".',
        ),
    ] = None,
) -> dict[str, Any]:
    """Starts a VK Teams bot or presses a button in one of its messages, as the person. Call it when the person
    asks to use a bot; read the bot's answer afterwards with list_messages on the bot's chat. The bot and its
    owner see this at once, so it needs user_confirmed; press refuses without a message and a button."""
    async with call("respond_to_bot") as c:
        if action == "press" and (message is None or not button):
            raise invalid("press needs message and button. Nothing was done.")
        data = pressed = None
        if action == "press":
            m, _ = await fetch_message(c, bot, msg_id(message))
            keys = keyboard(m)
            n = int(button.split(".")[0])
            if not 1 <= n <= len(keys) or button_token(n, keys[n - 1]) != button:
                raise invalid(
                    "The bot has changed this message's buttons since they were read, or there is no such button. "
                    "Read the message again with list_messages. Nothing was done."
                )
            data = keys[n - 1].get("callbackData")
            if not data:
                raise invalid("That button opens a link; give the person its url instead. Nothing was done.")
            pressed = content_line("bot", str(keys[n - 1].get("text", "")))
        receipt = c.guard(kind="bot", user_confirmed=user_confirmed, target=bot)
        if action == "start":
            await c.vk.rapi("startBot", {"bot": bot, "startParameter": parameter or ""}, write=True)
            return {"bot": bot, "done": "started", **receipt}
        await c.vk.rapi(
            "getBotCallbackAnswer", {"chatId": bot, "msgId": msg_id(message), "callbackData": data}, write=True
        )
        return {
            "bot": bot,
            "done": "pressed",
            "button": pressed,
            "note": "The bot answers in its chat; read it with list_messages.",
            **receipt,
        }
