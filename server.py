# server.py
import asyncio
import json
import os
import time
from pathlib import Path
import websockets

# Detect API version
WEBSOCKETS_VERSION = tuple(int(x) for x in websockets.__version__.split(".")[:2])
NEW_API = WEBSOCKETS_VERSION >= (14, 0)

if NEW_API:
    from websockets.http11 import Response
    from websockets.datastructures import Headers

connected_clients = set()
sender_clients = set()

# Authoritative view of what the overlay is currently showing.
current_text = ""
last_change_at = None


def _overlay_clients():
    return connected_clients - sender_clients


def _client_path(websocket):
    """Request path, across websockets v13 (legacy) and v14+."""
    request = getattr(websocket, "request", None)
    if request is not None and getattr(request, "path", None):
        return request.path
    return getattr(websocket, "path", "") or ""


def _state_message():
    ago = None if last_change_at is None else max(0.0, time.time() - last_change_at)
    return json.dumps({
        "action": "state",
        "text": current_text,
        "overlays": len(_overlay_clients()),
        "senders": len(sender_clients),
        # Age in seconds rather than a timestamp, so a skewed client clock
        # can't make "changed 3s ago" read as minutes.
        "changedAgo": ago,
    })


async def _send(targets, payload):
    if targets:
        await asyncio.gather(
            *[c.send(payload) for c in targets], return_exceptions=True
        )


async def _push_state():
    """Senders are the only clients that understand 'state'."""
    await _send(set(sender_clients), _state_message())


async def handle_client(websocket):
    global current_text, last_change_at

    is_sender = "role=sender" in _client_path(websocket)
    role = "Sender" if is_sender else "Overlay"

    connected_clients.add(websocket)
    if is_sender:
        sender_clients.add(websocket)

    print(f"✅ {role} connected. overlays={len(_overlay_clients())} senders={len(sender_clients)}")

    try:
        # A freshly connected overlay should immediately show whatever is live,
        # instead of staying blank until the next send.
        if not is_sender and current_text:
            await websocket.send(json.dumps({"action": "update", "text": current_text}))

        await _push_state()

        async for message in websocket:
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                continue

            action = data.get("action")

            if action == "update":
                current_text = data.get("text", "")
                last_change_at = time.time()
                await _send(
                    _overlay_clients(),
                    json.dumps({"action": "update", "text": current_text}),
                )
                await _push_state()

            elif action == "clear":
                current_text = ""
                last_change_at = time.time()
                await _send(_overlay_clients(), json.dumps({"action": "clear"}))
                await _push_state()

            elif action == "ping":
                await websocket.send(json.dumps({"action": "pong"}))

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        connected_clients.discard(websocket)
        sender_clients.discard(websocket)
        print(f"❌ {role} disconnected. overlays={len(_overlay_clients())} senders={len(sender_clients)}")
        await _push_state()


def _read_html():
    try:
        p = Path(__file__).parent / "index.html"
        return p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


async def process_request(*args):
    """Serve index.html for HTTP, let WebSocket upgrades through."""

    if NEW_API:
        # v14+: single Request object
        request = args[0]
        upgrade = request.headers.get("Upgrade", "").lower()
        if upgrade == "websocket":
            return None
        html = _read_html()
        if html is None:
            return Response(404, "Not Found", Headers([("Content-Type", "text/plain")]))
        return Response(
            200, "OK",
            Headers([("Content-Type", "text/html")]),
            html.encode("utf-8"),
        )
    else:
        # v13 and older: (path, headers_dict)
        path, headers = args
        if headers.get("Upgrade", "").lower() == "websocket":
            return None
        html = _read_html()
        if html is None:
            return (404, [("Content-Type", "text/plain")], b"Not Found")
        return (200, [("Content-Type", "text/html")], html.encode("utf-8"))


async def start_server():
    port = int(os.environ.get("PORT", 8765))
    async with websockets.serve(
        handle_client, "0.0.0.0", port, process_request=process_request
    ):
        print(f"🔌 Server running on port {port} (HTTP + WebSocket)")
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(start_server())
