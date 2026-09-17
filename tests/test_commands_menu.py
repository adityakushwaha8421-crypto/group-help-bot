"""The Telegram "/" menu is published on every start and matches the handlers in the code."""

import re

from app.telegram import input_bot


def test_menu_matches_the_registered_handlers():
    handled = set(re.findall(r'Command\("([a-z]+)"', open(input_bot.__file__, encoding="utf-8").read()))
    handled |= set(re.findall(r'Command\("[a-z]+", "([a-z]+)"', open(input_bot.__file__, encoding="utf-8").read()))
    menu = {c for c, _ in input_bot.BOT_COMMANDS}
    assert menu <= handled, menu - handled  # every menu entry has a handler
    assert handled - menu <= {"start"}  # every handler is in the menu (except /start)
    assert all(1 <= len(d) <= 256 for _, d in input_bot.BOT_COMMANDS)


async def test_register_commands_calls_the_bot_api():
    calls = []

    class Bot:
        async def set_my_commands(self, cmds, scope=None):
            calls.append(("set", len(cmds), type(scope).__name__))

        async def delete_my_commands(self, scope=None):
            calls.append(("delete", type(scope).__name__))

    assert await input_bot.register_commands(Bot()) == len(input_bot.BOT_COMMANDS)
    # menu ONLY in private chats; every group-facing scope is cleared
    assert calls[0] == ("set", len(input_bot.BOT_COMMANDS), "BotCommandScopeAllPrivateChats")
    assert set(calls[1:]) == {
        ("delete", "BotCommandScopeDefault"),
        ("delete", "BotCommandScopeAllGroupChats"),
        ("delete", "BotCommandScopeAllChatAdministrators"),
    }


async def test_input_router_only_serves_private_chats():
    from types import SimpleNamespace

    filters = input_bot.router.message._handler.filters or []
    assert filters, "the input router must carry a chat-type filter"
    msg = lambda kind: SimpleNamespace(chat=SimpleNamespace(type=kind))  # noqa: E731
    assert await filters[0].call(msg("private"))
    assert not await filters[0].call(msg("supergroup"))
    assert not await filters[0].call(msg("group"))
