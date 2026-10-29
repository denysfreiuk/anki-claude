"""MCP tools of the Anki&Claude plugin."""

from __future__ import annotations

import logging
import sys
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from . import __version__
from .anki import Anki
from .ankiconnect import AnkiConnect
from .config import load_config
from .errors import AnkiError
from .notes import NoteIn

T = TypeVar("T")

INSTRUCTIONS = """\
Інструменти для карток Anki через AnkiConnect (add-on у desktop-Anki).
- Якщо інструмент повертає помилку про недоступну Anki — передай користувачу підказку з помилки дослівно.
- sync сам відкладається, коли користувач повторює картки в desktop-Anki; не обходь це через force=true без його згоди.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITES = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)


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

    @server.tool(annotations=READ_ONLY)
    async def list_decks() -> dict[str, Any]:
        """Колоди під кореневою: пресет, ліміти нових/повторень на день, скільки карток
        нових / у навчанні / на повторення сьогодні, усього карток."""
        return await run(lambda anki: anki.decks.overview())

    @server.tool(annotations=WRITES)
    async def setup_decks() -> dict[str, Any]:
        """Створює стандартну структуру колод (корінь, English::EN-UA/UA-EN/Distinctions, ML, Uni)
        і окремий пресет для кожної гілки з лімітами нових карток. Ідемпотентно: наявне не ламає,
        змінені користувачем ліміти не скидає. Ліміт кореня й English = сума підколод."""
        return await run(lambda anki: anki.decks.setup())

    @server.tool(annotations=WRITES)
    async def create_deck(deck: str) -> dict[str, Any]:
        """Створює колоду під кореневою (напр. Code::Python, Uni::Calculus) з пресетом її гілки.
        Створюй лише тоді, коли в неї вже йдуть картки — порожніх колод не треба."""

        async def call(anki: Anki) -> dict[str, Any]:
            name, created = await anki.decks.ensure(deck)
            return {"deck": name, "created": created}

        return await run(call)

    @server.tool(annotations=WRITES)
    async def set_deck_options(
        deck: str,
        new_per_day: Annotated[int | None, Field(ge=0, le=9999)] = None,
        reviews_per_day: Annotated[int | None, Field(ge=0, le=9999)] = None,
    ) -> dict[str, Any]:
        """Змінює денні ліміти колоди (нових і повторень). Зміна йде в пресет колоди: якщо його
        поділяють інші колоди гілки (also_affects), ліміт зміниться і в них. Ліміт кореня
        й English перераховується автоматично."""
        return await run(lambda anki: anki.decks.set_options(deck, new_per_day, reviews_per_day))

    @server.tool(annotations=READ_ONLY)
    async def list_note_types() -> dict[str, Any]:
        """Типи нотаток AC: чи є в колекції і чи актуальні, їхні поля; інші типи нотаток колекції."""
        return await run(lambda anki: anki.note_types.overview())

    @server.tool(annotations=WRITES)
    async def ensure_note_types(dry_run: bool = False) -> dict[str, Any]:
        """Створює відсутні типи нотаток AC (Concept, Cloze, Formula, Code, Vocab, Distinction) і оновлює
        застарілі: шаблони, CSS, колоду шаблону, медіафайли. Поля не видаляє. dry_run=true — лише показати,
        що зміниться. Якщо full_sync_required=true — попередь користувача: наступна синхронізація буде повною."""
        return await run(lambda anki: anki.note_types.ensure(dry_run=dry_run))

    @server.tool(annotations=WRITES)
    async def add_notes(notes: list[NoteIn], known: bool = False) -> dict[str, Any]:
        """Додає нотатки пакетом і повертає звіт: added / duplicates / errors / decks_created.

        Сервер перевіряє правила (≥2 приклади з <b>слово</b> у vocab, пропуск ___ у distinction,
        {{c1::…}} у cloze, короткі пояснення, теги kebab-case), додає теги gen::claude і added::YYYY-MM,
        кладе картки у свої колоди. У vocab картка UA→EN створюється призупиненою, доки EN→UA не
        закріпиться (known=true — одразу активна); картка форм завжди чекає UA→EN.
        Дублікати не додаються — покажи їх користувачу. Після додавання сам синхронізує, якщо ввімкнено auto_sync.
        """
        return await run(lambda anki: anki.add_notes(notes, known=known))

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
