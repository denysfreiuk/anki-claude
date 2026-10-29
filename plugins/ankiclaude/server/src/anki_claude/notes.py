"""Adding notes: validation, duplicates, card routing and delayed production cards."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

from .ankiconnect import AnkiConnect
from .decks import Decks
from .notetypes import NoteTypeSpec, build_specs

NoteKind = Literal["concept", "cloze", "formula", "code", "vocab", "distinction"]

TAG_RE = re.compile(r"^[a-z0-9]+(?:[-.][a-z0-9]+)*(?:::[a-z0-9]+(?:[-.][a-z0-9]+)*)*$")
PENDING_PRODUCTION = "status::pending-production"
PENDING_FORMS = "status::pending-forms"
REGISTERS = {"neutral", "formal", "informal", "slang"}
SHORT_FIELDS = {"WhyThisWord": 300, "Explanation": 300, "Why": 300}
YES = {"yes", "y", "true", "1", "так", "+"}
NO = {"no", "n", "false", "0", "ні", "-", ""}
PRODUCTION_ORD = 1
FORMS_ORD = 2


class NoteIn(BaseModel):
    type: NoteKind = Field(description="Тип нотатки")
    fields: dict[str, str] = Field(description="Поля нотатки за назвами (див. list_note_types)")
    deck: str | None = Field(
        default=None,
        description="Колода відносно кореневої, напр. ML, Uni::Calculus, Code::Python. "
        "Для vocab і distinction не потрібна — вони йдуть у свої колоди English.",
    )
    tags: list[str] = Field(default_factory=list, description="Теги kebab-case латиницею, напр. topic::ml::pytorch")
    known: bool | None = Field(default=None, description="vocab: картка UA→EN одразу активна (без чекання)")


class NoteValidationError(Exception):
    pass


@dataclass
class Prepared:
    index: int
    spec: NoteTypeSpec
    deck: str  # relative to the root
    fields: dict[str, str]
    tags: list[str]
    pending: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return plain_text(self.fields[self.spec.fields[0]])[:80]


def plain_text(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", value)).replace("\xa0", " ").strip()


def normalize_tag(tag: str) -> str:
    value = tag.strip().lower().replace(" ", "-").replace("_", "-")
    value = re.sub(r"-{2,}", "-", value)
    if not TAG_RE.match(value):
        raise NoteValidationError(
            f"тег «{tag}» має бути латиницею в kebab-case, з ієрархією через :: (напр. topic::ml::linear-algebra)"
        )
    return value


def _flag(value: str, name: str) -> str:
    lowered = plain_text(value).lower()
    if lowered in YES:
        return "yes"
    if lowered in NO:
        return ""
    raise NoteValidationError(f"{name} має бути yes або no, а не «{value}»")


def _markdown_bold(value: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", value)


def _text(value: str) -> str:
    return value.strip().replace("\r\n", "\n").replace("\n", "<br>")


def prepare(note: NoteIn, index: int, specs: dict[str, NoteTypeSpec], decks: Decks, known: bool) -> Prepared:
    spec = specs[note.type]
    unknown = sorted(set(note.fields) - set(spec.fields))
    if unknown:
        raise NoteValidationError(f"невідомі поля {unknown}; у {spec.name} є: {list(spec.fields)}")
    fields = {name: note.fields.get(name, "") for name in spec.fields}
    for name in fields:
        if name == "Code":
            code = note.fields.get(name, "").replace("\r\n", "\n").strip("\n")
            fields[name] = html.escape(code, quote=False).replace("\n", "<br>")
        else:
            fields[name] = _text(fields[name])
    missing = [name for name in spec.required if not plain_text(fields[name])]
    if missing:
        raise NoteValidationError(f"порожні обов'язкові поля {missing}")
    for name, limit in SHORT_FIELDS.items():
        if name in fields and len(plain_text(fields[name])) > limit:
            raise NoteValidationError(f"{name} довше {limit} символів — скороти до 1–2 рядків або винеси в окрему картку")

    tags = []
    for tag in [*note.tags, "gen::claude", f"added::{date.today():%Y-%m}"]:
        normalized = normalize_tag(tag)
        if normalized not in tags:
            tags.append(normalized)

    pending: list[str] = []
    warnings: list[str] = []
    if spec.key == "vocab":
        examples = []
        for name in ("Example1", "Example2", "Example3"):
            fields[name] = _markdown_bold(fields[name])
            if plain_text(fields[name]):
                if not re.search(r"<(b|strong)>.+?</\1>", fields[name]):
                    raise NoteValidationError(f"{name}: виділи слово в реченні як <b>…</b>")
                examples.append(name)
        if len(examples) < 2:
            raise NoteValidationError("потрібно щонайменше 2 непорожні приклади (Example1–3)")
        fields["Active"] = _flag(fields["Active"], "Active")
        fields["DrillForms"] = _flag(fields["DrillForms"], "DrillForms")
        register = plain_text(fields["Register"]).lower()
        if register and register not in REGISTERS:
            raise NoteValidationError(f"Register має бути одним із {sorted(REGISTERS)}")
        fields["Register"] = register
        if fields["DrillForms"] and not plain_text(fields["Forms"]):
            raise NoteValidationError("DrillForms=yes, але поле Forms порожнє")
        if fields["Active"] and not (note.known if note.known is not None else known):
            pending.append("production")
            tags.append(PENDING_PRODUCTION)
        if fields["DrillForms"]:
            pending.append("forms")
            tags.append(PENDING_FORMS)
    elif spec.key == "cloze":
        if not re.search(r"\{\{c\d+::", fields["Text"]):
            raise NoteValidationError("у Text немає жодного пропуску {{c1::…}}")
    elif spec.key == "distinction":
        if not re.search(r"_{3,}", fields["Sentence"]):
            raise NoteValidationError("у Sentence має бути пропуск ___ і варіанти, напр. «___ a mistake (make / do)»")
    elif spec.key == "code":
        fields["Language"] = re.sub(r"[^a-z0-9+#-]", "", plain_text(fields["Language"]).lower())

    if spec.deck:
        if note.deck and decks.resolve(note.deck) != spec.deck:
            warnings.append(f"колоду {note.deck} проігноровано: {spec.name} завжди йде в {decks.full(spec.deck)}")
        deck = spec.deck
    elif note.deck:
        deck = decks.resolve(note.deck)
    else:
        raise NoteValidationError("не вказано колоду (deck), напр. ML, Uni або Code::Python")
    return Prepared(index, spec, deck, fields, tags, pending, warnings)


def field_query(name: str, value: str) -> str:
    escaped = re.sub(r'([\\"*_:])', r"\\\1", plain_text(value))
    return f'"{name}:{escaped}"'


class Notes:
    def __init__(self, client: AnkiConnect, decks: Decks, language: str) -> None:
        self.client = client
        self.decks = decks
        self.specs = build_specs(language)

    async def add(self, notes: list[NoteIn], known: bool = False) -> dict[str, Any]:
        report: dict[str, Any] = {"added": [], "duplicates": [], "errors": [], "decks_created": [], "warnings": []}
        prepared: list[Prepared] = []
        for index, note in enumerate(notes):
            try:
                item = prepare(note, index, self.specs, self.decks, known)
            except NoteValidationError as error:
                key = next(iter(note.fields.values()), "") if note.fields else ""
                report["errors"].append({"index": index, "type": note.type, "key": plain_text(key)[:80], "error": str(error)})
                continue
            prepared.append(item)
            report["warnings"] += [f"№{index + 1}: {w}" for w in item.warnings]
        if not prepared:
            return self._summarize(report)

        for deck in sorted({p.deck for p in prepared}):
            _, created = await self.decks.ensure(deck)
            report["decks_created"] += created

        anki_notes = [self._anki_note(p) for p in prepared]
        checks = await self.client.invoke("canAddNotesWithErrorDetail", notes=anki_notes)
        addable: list[tuple[Prepared, dict]] = []
        for item, payload, check in zip(prepared, anki_notes, checks, strict=True):
            if check.get("canAdd"):
                addable.append((item, payload))
            elif "duplicate" in str(check.get("error", "")).lower():
                existing = await self.client.invoke(
                    "findNotes",
                    query=f'note:"{item.spec.name}" {field_query(item.spec.fields[0], item.fields[item.spec.fields[0]])}',
                )
                report["duplicates"].append({"index": item.index, "type": item.spec.key, "key": item.key, "existing_note_ids": existing})
            else:
                report["errors"].append({"index": item.index, "type": item.spec.key, "key": item.key, "error": str(check.get("error"))})
        if not addable:
            return self._summarize(report)

        ids = await self.client.invoke("addNotes", notes=[payload for _, payload in addable])
        added = [(item, note_id) for (item, _), note_id in zip(addable, ids, strict=True) if note_id]
        for (item, _), note_id in zip(addable, ids, strict=True):
            if not note_id:
                report["errors"].append({"index": item.index, "type": item.spec.key, "key": item.key, "error": "Anki не додала нотатку"})

        cards_by_note = await self._route_and_suspend(added)
        for item, note_id in added:
            report["added"].append(
                {
                    "index": item.index,
                    "note_id": note_id,
                    "type": item.spec.key,
                    "key": item.key,
                    "deck": self.decks.full(item.deck),
                    "cards": cards_by_note.get(note_id, 0),
                    "pending": item.pending,
                }
            )
        return self._summarize(report)

    def _anki_note(self, item: Prepared) -> dict[str, Any]:
        return {
            "deckName": self.decks.full(item.deck),
            "modelName": item.spec.name,
            "fields": item.fields,
            "tags": item.tags,
            "options": {"allowDuplicate": False, "duplicateScope": "collection"},
        }

    async def _route_and_suspend(self, added: list[tuple[Prepared, int]]) -> dict[int, int]:
        """Move every card to its template's deck and suspend delayed cards.

        With API v1 the template deck override already put the cards in place; this pass
        is what routes them on older AnkiConnect, and a no-op otherwise.
        """
        if not added:
            return {}
        by_note = {note_id: item for item, note_id in added}
        card_ids = await self.client.invoke("findCards", query="nid:" + ",".join(str(n) for n in by_note))
        cards = await self.client.invoke("cardsInfo", cards=card_ids)
        moves: dict[str, list[int]] = {}
        suspend: list[int] = []
        counts: dict[int, int] = {}
        for card in cards:
            item = by_note[card["note"]]
            counts[card["note"]] = counts.get(card["note"], 0) + 1
            template = item.spec.templates[card["ord"]] if card["ord"] < len(item.spec.templates) else None
            wanted = self.decks.full(template.deck) if template and template.deck else self.decks.full(item.deck)
            if card["deckName"] != wanted:
                moves.setdefault(wanted, []).append(card["cardId"])
            if item.spec.key == "vocab" and (
                (card["ord"] == PRODUCTION_ORD and "production" in item.pending)
                or (card["ord"] == FORMS_ORD and "forms" in item.pending)
            ):
                suspend.append(card["cardId"])
        for deck, ids in moves.items():
            await self.client.invoke("changeDeck", cards=ids, deck=deck)
        if suspend:
            await self.client.invoke("suspend", cards=suspend)
        return counts

    def _summarize(self, report: dict[str, Any]) -> dict[str, Any]:
        report["summary"] = (
            f"додано {len(report['added'])}, дублікатів {len(report['duplicates'])}, помилок {len(report['errors'])}"
        )
        return report
