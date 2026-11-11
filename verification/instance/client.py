"""Home Assistant's REST and WebSocket API, as one person's session sees it.

Knows nothing about MXZ's rules. Every call goes to the one instance URL it is
given; there is no host argument anywhere in this driver.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any, Self

import aiohttp

CLIENT_ID = "http://127.0.0.1/"


class ApiError(Exception):
    pass


async def onboard(url: str, auth_file: Path) -> None:
    """Create the instance's invented owner and keep its login in the instance only."""
    async with aiohttp.ClientSession() as http:
        async with http.get(f"{url}/api/onboarding") as response:
            steps = {step["step"]: step["done"] for step in await response.json()}
        if steps.get("user"):
            return
        login = {"username": "invented", "password": secrets.token_urlsafe(18)}
        async with http.post(f"{url}/api/onboarding/users", json={
            "client_id": CLIENT_ID, "name": "Invented Person", "language": "en", **login,
        }) as response:
            code = (await _ok(response))["auth_code"]
        async with http.post(f"{url}/auth/token", data={
            "grant_type": "authorization_code", "code": code, "client_id": CLIENT_ID,
        }) as response:
            tokens = await _ok(response)
        auth_file.touch(mode=0o600)
        auth_file.write_text(json.dumps({**login, "refresh_token": tokens["refresh_token"]}))
        bearer = {"Authorization": f"Bearer {tokens['access_token']}"}
        for step, body in (
            ("core_config", {}),
            ("analytics", {}),
            ("integration", {"client_id": CLIENT_ID, "redirect_uri": CLIENT_ID}),
        ):
            async with http.post(f"{url}/api/onboarding/{step}", json=body, headers=bearer) as response:
                await _ok(response)


async def _ok(response: aiohttp.ClientResponse) -> Any:
    if response.status >= 400:
        raise ApiError(f"{response.method} {response.url.path} -> {response.status}: {await response.text()}")
    return await response.json() if response.content_type == "application/json" else await response.text()


class Client:
    """One authenticated session: REST calls and one WebSocket."""

    def __init__(self, url: str, auth_file: Path) -> None:
        self.url = url
        self._auth_file = auth_file
        self._http: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._listeners: dict[int, Callable[[dict[str, Any]], None]] = {}
        self._reader: asyncio.Task[None] | None = None
        self.user_id: str | None = None

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def connect(self) -> None:
        refresh = json.loads(self._auth_file.read_text())["refresh_token"]
        self._http = aiohttp.ClientSession()
        async with self._http.post(f"{self.url}/auth/token", data={
            "grant_type": "refresh_token", "refresh_token": refresh, "client_id": CLIENT_ID,
        }) as response:
            token = (await _ok(response))["access_token"]
        self._http.headers["Authorization"] = f"Bearer {token}"
        self._ws = await self._http.ws_connect(f"{self.url}/api/websocket", max_msg_size=0)
        await self._ws.receive_json()
        await self._ws.send_json({"type": "auth", "access_token": token})
        if (reply := await self._ws.receive_json())["type"] != "auth_ok":
            raise ApiError(f"websocket auth: {reply}")
        self._reader = asyncio.create_task(self._read())
        self.user_id = (await self.ws("auth/current_user"))["id"]

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
        if self._ws:
            await self._ws.close()
        if self._http:
            await self._http.close()

    async def _read(self) -> None:
        assert self._ws is not None
        async for message in self._ws:
            if message.type is not aiohttp.WSMsgType.TEXT:
                break
            for frame in _frames(message.json()):
                if frame["type"] == "event" and (listener := self._listeners.get(frame["id"])):
                    listener(frame["event"])
                elif (future := self._pending.pop(frame["id"], None)) and not future.done():
                    if frame.get("success", True):
                        future.set_result(frame.get("result"))
                    else:
                        future.set_exception(ApiError(json.dumps(frame.get("error"))))
        for future in self._pending.values():
            future.set_exception(ApiError("websocket closed"))

    async def ws(self, kind: str, **fields: Any) -> Any:
        assert self._ws is not None
        ident = next(self._ids)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[ident] = future
        await self._ws.send_json({"id": ident, "type": kind, **fields})
        return await asyncio.wait_for(future, 60)

    async def subscribe(self, event_type: str, listener: Callable[[dict[str, Any]], None]) -> None:
        assert self._ws is not None
        ident = next(self._ids)
        self._listeners[ident] = listener
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[ident] = future
        await self._ws.send_json({"id": ident, "type": "subscribe_events", "event_type": event_type})
        await asyncio.wait_for(future, 60)

    async def rest(self, method: str, path: str, body: Any = None, params: dict[str, str] | None = None) -> Any:
        assert self._http is not None
        async with self._http.request(method, f"{self.url}{path}", json=body, params=params) as response:
            return await _ok(response)

    async def call(self, domain: str, service: str, **data: Any) -> None:
        await self.ws("call_service", domain=domain, service=service, service_data=data)

    async def states(self) -> dict[str, dict[str, Any]]:
        return {state["entity_id"]: state for state in await self.ws("get_states")}

    async def entities(self) -> list[dict[str, Any]]:
        return await self.ws("config/entity_registry/list")

    async def entries(self, domain: str) -> list[dict[str, Any]]:
        return await self.ws("config_entries/get", domain=domain)

    async def flow_start(self, handler: str) -> dict[str, Any]:
        return await self.rest("POST", "/api/config/config_entries/flow", {"handler": handler})

    async def flow_configure(self, flow_id: str, data: dict[str, Any]) -> dict[str, Any]:
        return await self.rest("POST", f"/api/config/config_entries/flow/{flow_id}", data)

    async def flow_abort(self, flow_id: str) -> None:
        await self.rest("DELETE", f"/api/config/config_entries/flow/{flow_id}")

    async def history(self, start: str, end: str, entity_ids: list[str]) -> Any:
        return await self.rest(
            "GET", f"/api/history/period/{start}",
            params={"end_time": end, "filter_entity_id": ",".join(entity_ids)},
        )

    async def diagnostics(self, entry_id: str) -> Any:
        return await self.rest("GET", f"/api/diagnostics/config_entry/{entry_id}")


def _frames(payload: Any) -> list[dict[str, Any]]:
    return payload if isinstance(payload, list) else [payload]
