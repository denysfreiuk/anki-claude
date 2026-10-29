"""MCP tools of the Anki&Claude plugin."""

from __future__ import annotations

import logging
import sys
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from . import __version__
from .anki import Anki
from .ankiconnect import AnkiConnect
from .config import load_config
from .errors import AnkiError

T = TypeVar("T")

INSTRUCTIONS = """\
Інструменти для карток Anki через AnkiConnect (add-on у desktop-Anki).
- Якщо інструмент повертає помилку про недоступну Anki — передай користувачу підказку з помилки дослівно.
- sync сам відкладається, коли користувач повторює картки в desktop-Anki; не обходь це через force=true без його згоди.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)


def build_server(get_anki: Callable[[], Anki]) -> MCPServer:
    server = MCPServer(
        name="anki", title="Anki&Claude", version=__version__, instructions=INSTRUCTIONS, log_level="WARNING"
    )

    async def run(call: Callable[[Anki], Awaitable[T]]) -> T:
        try:
            return await call(get_anki())
        except AnkiError as error:
            raise ToolError(str(error)) from error

    @server.tool(annotations=READ_ONLY)
    async def check_connection() -> dict[str, Any]:
        """Перевіряє, чи запущена Anki з AnkiConnect.

        Повертає версії AnkiConnect і Anki, активний профіль, стан кореневої колоди,
        які типи нотаток AC уже є, чи користувач зараз повторює картки, попередження
        конфігу і наступні кроки. Викликай першим, якщо щось не працює.
        """
        return await run(lambda anki: anki.connection_report())

    @server.tool(annotations=READ_ONLY)
    async def get_config() -> dict[str, Any]:
        """Повертає налаштування плагіна: коренева колода, мова пояснень, рівень англійської,
        складність прикладів, максимум карток за раз, поріг розморожування, автосинхронізація.
        API-ключ не показується. Використовуй ці значення під час генерації карток."""
        return await run(lambda anki: _config(anki))

    @server.tool(annotations=READ_ONLY)
    async def review_status() -> dict[str, Any]:
        """Чи повторює користувач зараз картки в desktop-Anki (лише читання, нічого не перемикає)."""
        return await run(lambda anki: anki.review_status())

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True))
    async def sync(force: bool = False) -> dict[str, Any]:
        """Синхронізує desktop-Anki з AnkiWeb і чекає результату.

        Якщо користувач саме повторює картки в desktop-Anki, синхронізація відкладається
        (status=deferred). force=true — синхронізувати попри це, лише з явної згоди користувача.
        Помилка означає, що синхронізація НЕ відбулася: передай користувачу її текст і підказку.
        """
        return await run(lambda anki: anki.sync(force=force))

    return server


async def _config(anki: Anki) -> dict[str, Any]:
    return anki.config.public_dict()


_anki: Anki | None = None


def _default_anki() -> Anki:
    global _anki
    if _anki is None:
        config = load_config()
        _anki = Anki(config, AnkiConnect(config.anki_connect_url, config.anki_connect_key))
    return _anki


def main() -> None:
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    # httpx logs every request at INFO; the server's stderr ends up in Claude Code's MCP logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    build_server(_default_anki).run("stdio")


if __name__ == "__main__":
    main()
