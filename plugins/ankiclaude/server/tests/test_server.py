from __future__ import annotations

from mcp.client.client import Client

from anki_claude.server import build_server

from .fake_ankiconnect import FakeAnkiConnect


async def test_tools_are_registered(fake: FakeAnkiConnect, make_anki) -> None:
    anki = make_anki(fake)
    async with Client(build_server(lambda: anki)) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert set(tools) == {"check_connection", "get_config", "review_status", "sync"}
    assert tools["check_connection"].annotations.read_only_hint is True
    assert tools["sync"].annotations.read_only_hint is False


async def test_check_connection_returns_structured_report(fake: FakeAnkiConnect, make_anki) -> None:
    anki = make_anki(fake)
    async with Client(build_server(lambda: anki)) as client:
        result = await client.call_tool("check_connection")
    assert result.is_error is False
    assert result.structured_content["profile"] == "Користувач 1"


async def test_anki_errors_reach_claude_as_readable_tool_errors(make_anki) -> None:
    anki = make_anki(FakeAnkiConnect(running=False))
    async with Client(build_server(lambda: anki)) as client:
        result = await client.call_tool("check_connection")
    assert result.is_error is True
    assert "Відкрий Anki" in result.content[0].text


async def test_get_config_never_exposes_the_key(fake: FakeAnkiConnect, make_anki) -> None:
    fake.key = "secret"
    anki = make_anki(fake, ANKI_CONNECT_KEY="secret")
    async with Client(build_server(lambda: anki)) as client:
        result = await client.call_tool("get_config")
    assert "secret" not in str(result.structured_content)
    assert result.structured_content["root_deck"] == "Den"
