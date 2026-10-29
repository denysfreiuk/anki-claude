"""In-memory stand-in for AnkiConnect, served through httpx.MockTransport."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class FakeAnkiConnect:
    running: bool = True
    api_version: int = 6
    v1: bool = True
    key: str | None = None
    logged_in: bool = True
    reviewing: bool = False
    has_gui_review_active: bool = True
    # States collection.sync passes through on API v1; the last one is final.
    sync_states: list[str] = field(default_factory=lambda: ["running", "succeeded"])
    sync_error_type: str = "ioError"
    profile: str = "Користувач 1"
    decks: list[str] = field(default_factory=lambda: ["Типовий"])
    models: list[str] = field(default_factory=lambda: ["Базовий"])
    calls: list[str] = field(default_factory=list)
    syncs: int = 0
    _operations: dict[str, list[str]] = field(default_factory=dict)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if not self.running:
            raise httpx.ConnectError("connection refused", request=request)
        body = json.loads(request.content)
        if request.url.path == "/api/v1":
            return self._v1(request, body)
        return httpx.Response(200, json=self._v6(body))

    # API v6

    def _v6(self, body: dict) -> dict:
        action = body["action"]
        self.calls.append(action)
        if action != "requestPermission" and body.get("key") != self.key:
            return {"result": None, "error": "valid api key must be provided"}
        try:
            return {"result": self._v6_action(action, body.get("params", {})), "error": None}
        except Exception as error:  # noqa: BLE001 - mirrors AnkiConnect's catch-all reply
            return {"result": None, "error": str(error)}

    def _v6_action(self, action: str, params: dict) -> Any:
        if action == "version":
            return self.api_version
        if action == "getActiveProfile":
            return self.profile
        if action == "deckNames":
            return list(self.decks)
        if action == "modelNames":
            return list(self.models)
        if action == "guiReviewActive":
            if not self.has_gui_review_active:
                raise Exception("unsupported action")
            return self.reviewing
        if action == "guiCurrentCard":
            if not self.reviewing:
                raise Exception("Gui review is not currently active.")
            return {"cardId": 1}
        if action == "sync":
            if not self.logged_in:
                raise Exception("sync: auth not configured")
            self.syncs += 1
            return None
        if action == "multi":
            return [self._v6(dict(item, key=self.key)) for item in params["actions"]]
        raise Exception("unsupported action")

    # API v1

    def _v1(self, request: httpx.Request, body: dict) -> httpx.Response:
        if not self.v1:
            return httpx.Response(404)
        if self.key and request.headers.get("authorization") != f"Bearer {self.key}":
            return httpx.Response(401)
        method = body["method"]
        self.calls.append(method)

        def ok(result: Any) -> httpx.Response:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

        def fail(kind: str, message: str) -> httpx.Response:
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": 1000, "message": message, "data": {"type": kind}}},
            )

        if method == "api.describe":
            names = ["reviewer.get", "collection.sync", "operations.get"]
            return ok({"apiMajor": 1, "ankiVersion": "26.09.3", "methods": [{"name": n, "available": True, "reason": None} for n in names]})
        if method == "reviewer.get":
            return ok({"active": self.reviewing, "side": "question" if self.reviewing else "none"})
        if method == "collection.sync":
            if not self.logged_in:
                return fail("authenticationRequired", "Sync authentication is not configured.")
            operation_id = f"op{len(self._operations) + 1}"
            self._operations[operation_id] = list(self.sync_states)
            return ok({"operationId": operation_id, "method": "collection.sync", "session": None})
        if method == "operations.get":
            states = self._operations[body["params"]["operationId"]]
            state = states.pop(0) if len(states) > 1 else states[0]
            operation = {"operationId": body["params"]["operationId"], "method": "collection.sync", "state": state}
            if state == "succeeded":
                self.syncs += 1
                operation["result"] = {"collectionComplete": True, "mediaComplete": True}
            elif state in ("failed", "cancelled"):
                operation["error"] = {"code": 1012, "message": "network down", "data": {"type": self.sync_error_type}}
            return ok(operation)
        return fail("methodNotFound", f"unknown method {method}")
