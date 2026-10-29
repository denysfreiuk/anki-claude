"""Create and update the AC note types in the user's collection."""

from __future__ import annotations

import base64
from typing import Any

from .ankiconnect import AnkiConnect
from .decks import Decks
from .notetypes import NoteTypeSpec, build_specs, media_files


class NoteTypes:
    def __init__(self, client: AnkiConnect, decks: Decks, language: str, tts: bool = True, tts_lang: str = "en_US") -> None:
        self.client = client
        self.decks = decks
        self.specs = build_specs(language, tts, tts_lang)

    async def overview(self) -> dict[str, Any]:
        report = await self.ensure(dry_run=True)
        names = await self.client.invoke("modelNames")
        ours = {spec.name for spec in self.specs.values()}
        return {
            "ac_note_types": {
                spec.name: (
                    "немає" if spec.name in report["created"]
                    else "застарів" if spec.name in report["updated"]
                    else "актуальний"
                )
                for spec in self.specs.values()
            },
            "fields": {spec.name: list(spec.fields) for spec in self.specs.values()},
            "other_note_types": sorted(n for n in names if n not in ours),
        }

    async def ensure(self, dry_run: bool = False) -> dict[str, Any]:
        """Create missing AC note types and bring outdated ones up to date. Never removes fields."""
        caps = await self.client.capabilities()
        use_v1 = caps.has("noteTypes.create") and caps.has("noteTypes.get") and caps.has("noteTypes.editStructure")
        names = set(await self.client.invoke("modelNames"))
        report: dict[str, Any] = {
            "dry_run": dry_run,
            "created": [],
            "updated": [],
            "unchanged": [],
            "changes": {},
            "full_sync_required": False,
            "deck_overrides": "в шаблонах (API v1)" if use_v1 else "маршрутизація після додавання (API v6)",
            "media_stored": [],
        }
        if not dry_run:
            for deck in sorted({t.deck for s in self.specs.values() for t in s.templates if t.deck}):
                await self.decks.ensure(deck)
        deck_ids = await self.client.invoke("deckNamesAndIds") if use_v1 else {}

        for spec in self.specs.values():
            if spec.name not in names:
                report["created"].append(spec.name)
                if not dry_run:
                    await self._create(spec, use_v1)
                continue
            changes = await self._sync_existing(spec, use_v1, deck_ids, dry_run)
            if changes:
                report["updated"].append(spec.name)
                report["changes"][spec.name] = changes
                if any(c.startswith(("нове поле", "новий шаблон")) for c in changes):
                    report["full_sync_required"] = True
            else:
                report["unchanged"].append(spec.name)

        if not dry_run:
            report["media_stored"] = await self._store_media()
        return report

    async def _create(self, spec: NoteTypeSpec, use_v1: bool) -> None:
        if use_v1:
            await self.client.rpc(
                "noteTypes.create",
                {
                    "name": spec.name,
                    "kind": spec.kind,
                    "fields": [{"name": field} for field in spec.fields],
                    "templates": [
                        {
                            "name": t.name,
                            "questionHtml": t.front,
                            "answerHtml": t.back,
                            "deck": {"name": self.decks.full(t.deck)} if t.deck else None,
                        }
                        for t in spec.templates
                    ],
                    "css": spec.css,
                    "sortField": spec.fields[0],
                },
            )
            return
        await self.client.invoke(
            "createModel",
            modelName=spec.name,
            inOrderFields=list(spec.fields),
            css=spec.css,
            isCloze=spec.kind == "cloze",
            cardTemplates=[{"Name": t.name, "Front": t.front, "Back": t.back} for t in spec.templates],
        )

    async def _sync_existing(self, spec: NoteTypeSpec, use_v1: bool, deck_ids: dict, dry_run: bool) -> list[str]:
        fields, templates, styling = await self.client.multi(
            [
                ("modelFieldNames", {"modelName": spec.name}),
                ("modelTemplates", {"modelName": spec.name}),
                ("modelStyling", {"modelName": spec.name}),
            ]
        )
        for value in (fields, templates, styling):
            if isinstance(value, Exception):
                raise value
        changes: list[str] = []
        missing_fields = [f for f in spec.fields if f not in fields]
        missing_templates = [t for t in spec.templates if t.name not in templates]
        changed_templates = [
            t for t in spec.templates
            if t.name in templates and (templates[t.name]["Front"] != t.front or templates[t.name]["Back"] != t.back)
        ]
        changes += [f"нове поле {f}" for f in missing_fields]
        changes += [f"новий шаблон {t.name}" for t in missing_templates]
        changes += [f"оновлено шаблон {t.name}" for t in changed_templates]
        if styling["css"] != spec.css:
            changes.append("оновлено CSS")

        override_edits = []
        if use_v1:
            current = await self.client.rpc("noteTypes.get", {"noteType": {"name": spec.name}})
            current_decks = {t["name"]: (t.get("deck") or {}).get("id") for t in current["templates"]}
            for t in spec.templates:
                wanted = str(deck_ids[self.decks.full(t.deck)]) if t.deck and self.decks.full(t.deck) in deck_ids else None
                if t.deck and t.name in current_decks and current_decks[t.name] != wanted:
                    override_edits.append(
                        {"op": "updateTemplate", "name": t.name, "patch": {"deck": {"name": self.decks.full(t.deck)}}}
                    )
                    changes.append(f"колода шаблону {t.name} → {self.decks.full(t.deck)}")
        if dry_run or not changes:
            return changes

        for field in missing_fields:
            await self.client.invoke("modelFieldAdd", modelName=spec.name, fieldName=field, index=spec.fields.index(field))
        for t in missing_templates:
            await self.client.invoke("modelTemplateAdd", modelName=spec.name, template={"Name": t.name, "Front": t.front, "Back": t.back})
        if changed_templates:
            await self.client.invoke(
                "updateModelTemplates",
                model={"name": spec.name, "templates": {t.name: {"Front": t.front, "Back": t.back} for t in changed_templates}},
            )
        if styling["css"] != spec.css:
            await self.client.invoke("updateModelStyling", model={"name": spec.name, "css": spec.css})
        if use_v1 and missing_templates:
            # Templates added through v6 have no deck override yet.
            override_edits += [
                {"op": "updateTemplate", "name": t.name, "patch": {"deck": {"name": self.decks.full(t.deck)}}}
                for t in missing_templates if t.deck
            ]
        if override_edits:
            await self.client.rpc("noteTypes.editStructure", {"noteType": {"name": spec.name}, "edits": override_edits})
        return changes

    async def _store_media(self) -> list[str]:
        stored = []
        for name, path in media_files().items():
            present = await self.client.invoke("getMediaFilesNames", pattern=name)
            if name in present:
                continue
            data = base64.b64encode(path.read_bytes()).decode("ascii")
            await self.client.invoke("storeMediaFile", filename=name, data=data)
            stored.append(name)
        return stored
