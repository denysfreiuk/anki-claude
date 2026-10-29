"""Operations on the user's Anki that the MCP tools expose."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .ankiconnect import AnkiConnect
from .config import Config
from .constants import NOTE_TYPE_NAMES
from .errors import AnkiActionError, AnkiError, AnkiOutdatedError, SyncError

SYNC_TIMEOUT = 180.0
SYNC_POLL_INTERVAL = 0.5


class Anki:
    def __init__(self, config: Config, client: AnkiConnect) -> None:
        self.config = config
        self.client = client

    async def review_status(self) -> dict[str, Any]:
        """Whether the user is reviewing cards in desktop Anki right now. Read-only."""
        caps = await self.client.capabilities()
        if caps.has("reviewer.get"):
            state = await self.client.rpc("reviewer.get")
            return {"reviewing": bool(state.get("active")), "side": state.get("side"), "source": "api-v1"}
        try:
            active = await self.client.invoke("guiReviewActive")
            return {"reviewing": bool(active), "side": None, "source": "api-v6"}
        except AnkiOutdatedError:
            pass
        try:
            await self.client.invoke("guiCurrentCard")
        except AnkiActionError:
            return {"reviewing": False, "side": None, "source": "api-v6"}
        return {"reviewing": True, "side": None, "source": "api-v6"}

    async def sync(self, force: bool = False) -> dict[str, Any]:
        """Sync with AnkiWeb and wait for the result.

        While the user reviews in desktop Anki the sync is deferred unless `force`.
        Raises SyncError when the sync did not complete.
        """
        review = await self.review_status()
        if review["reviewing"] and not force:
            return {
                "status": "deferred",
                "message": "Ти зараз повторюєш картки в Anki — синхронізацію відкладено, щоб не заважати. "
                "Запусти її після повторення.",
            }
        started = time.monotonic()
        caps = await self.client.capabilities()
        if caps.has("collection.sync") and caps.has("operations.get"):
            result = await self._sync_v1(started)
        else:
            result = await self._sync_v6()
        return {"status": "synced", "seconds": round(time.monotonic() - started, 1), **result}

    async def _sync_v1(self, started: float) -> dict[str, Any]:
        try:
            handle = await self.client.rpc("collection.sync", {})
        except AnkiActionError as error:
            raise sync_error(error.kind, error.error) from error
        operation_id = handle["operationId"]
        while True:
            operation = await self.client.rpc("operations.get", {"operationId": operation_id})
            state = operation.get("state")
            if state == "succeeded":
                return {"media": bool((operation.get("result") or {}).get("mediaComplete"))}
            if state in ("failed", "cancelled"):
                error = operation.get("error") or {}
                kind = "cancelled" if state == "cancelled" else (error.get("data") or {}).get("type")
                raise sync_error(kind, error.get("message", ""))
            if state == "waitingForUser":
                raise sync_error("fullSyncRequired", "")
            if time.monotonic() - started > SYNC_TIMEOUT:
                raise SyncError(
                    "timeout",
                    f"Синхронізація не завершилась за {SYNC_TIMEOUT:g} с.",
                    "Подивись у вікно Anki: можливо, вона ще триває або чекає на тебе.",
                )
            await asyncio.sleep(SYNC_POLL_INTERVAL)

    async def _sync_v6(self) -> dict[str, Any]:
        # A bare v6 "sync" can return before the sync finishes; inside "multi" it is awaited
        # and its failure is reported, so this is the only reliable v6 form.
        (outcome,) = await self.client.multi([("sync", {})], timeout=SYNC_TIMEOUT)
        if isinstance(outcome, AnkiActionError):
            raise sync_error(None, outcome.error) from outcome
        if isinstance(outcome, AnkiError):
            raise outcome
        return {}

    async def connection_report(self) -> dict[str, Any]:
        caps = await self.client.capabilities(refresh=True)
        profile, decks, models = await self.client.multi(
            [("getActiveProfile", {}), ("deckNames", {}), ("modelNames", {})]
        )
        for value in (profile, decks, models):
            if isinstance(value, Exception):
                raise value
        review = await self.review_status()

        root = self.config.root_deck
        warnings = list(self.config.warnings)
        root_info: dict[str, Any] = {"name": root, "exists": None, "subdecks": []}
        if root:
            root_info["exists"] = root in decks
            root_info["subdecks"] = sorted(d for d in decks if d.startswith(root + "::"))
        note_types = {name: name in models for name in NOTE_TYPE_NAMES}

        next_steps = []
        if root and not root_info["exists"]:
            next_steps.append(f"Колоди «{root}» ще немає — її створить налаштування колод (setup_decks).")
        if not all(note_types.values()):
            next_steps.append("Типи нотаток AC ще не створені — їх створить ensure_note_types.")

        return {
            "ok": True,
            "anki_connect": {
                "url": self.client.url,
                "api_version": caps.api_version,
                "api_v1": caps.v1,
                "anki_version": caps.anki_version,
            },
            "profile": profile,
            "root_deck": root_info,
            "note_types": note_types,
            "review": review,
            "warnings": warnings,
            "next_steps": next_steps,
        }


def sync_error(kind: str | None, detail: str) -> SyncError:
    """Translate an AnkiConnect sync failure into a message for the user."""
    text = (detail or "").lower()
    if kind == "authenticationRequired" or "auth not configured" in text:
        return SyncError(
            "not_logged_in",
            "Синхронізація неможлива: у desktop-Anki не виконано вхід в AnkiWeb.",
            "В Anki натисни «Синхронізувати» (кнопка вгорі праворуч) і увійди в акаунт AnkiWeb.",
        )
    if kind == "fullSyncRequired" or "sync status" in text or "full sync" in text:
        return SyncError(
            "full_sync_required",
            "Anki потребує повної синхронізації і чекає твого вибору у своєму вікні.",
            "Обери в Anki напрям: Upload — залишити версію з комп'ютера, Download — взяти з AnkiWeb. "
            "Якщо не впевнений — натисни Cancel і спитай мене.",
        )
    if kind == "editorConflict":
        return SyncError(
            "editor_open",
            "Синхронізація неможлива, поки в Anki відкрите вікно редагування або додавання карток.",
            "Закрий його і спробуй ще раз.",
        )
    if kind in ("busy", "invalidState"):
        return SyncError("busy", "Anki зараз зайнята іншою операцією (можливо, вже синхронізується).", "Спробуй трохи згодом.")
    if kind == "cancelled":
        return SyncError("cancelled", "Синхронізацію скасовано в Anki.")
    return SyncError(
        "network" if kind == "ioError" else "unknown",
        f"Синхронізація з AnkiWeb не вдалася: {detail or 'без опису'}.",
        "Перевір інтернет і спробуй ще раз.",
    )
