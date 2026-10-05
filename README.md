# Overlay Sender

Web page that pushes text to a desktop overlay over WebSocket.

- `server.py` — serves `index.html` over HTTP and relays messages over WebSocket, on one port.
- `index.html` — the sender page. Shows connection state, whether an overlay is attached, and the text currently live on it.
- `overlay/` — the Windows PyQt5 overlay client. Runs on the viewer's machine; **not** deployed.

## Protocol

Senders connect with `?role=sender` and receive `state` messages:

```json
{ "action": "state", "text": "...", "overlays": 1, "senders": 1, "changedAgo": 2.4 }
```

Overlays connect with no query string and receive only:

```json
{ "action": "update", "text": "..." }
{ "action": "clear" }
```

A reconnecting overlay is automatically resynced to the current text.

> Overlays treat any non-`clear` message as text to display, so never broadcast a
> new message type to them without updating `overlay/overlay.py`.

## Run locally

```sh
pip install -r requirements.txt
python server.py          # http://localhost:8765
```

## Deploy (Render, free tier)

Binds `$PORT`, so it works as a Render Web Service with no extra config —
`render.yaml` sets it up as a Blueprint.

After deploying, point `SERVER_URI` in `overlay/overlay.py` at the deployed host
(`wss://…`).

Free-tier notes: the instance sleeps after ~15 min idle (~50s cold start), and
750 instance-hours/month are shared across all free services.

## No authentication

Anyone who knows the URL can push text to the overlay. Fine on a LAN; consider a
shared token before relying on this over the public internet.
