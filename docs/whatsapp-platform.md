# WhatsApp Agent Platform: protocol notes

What `channels/whatsapp/client.py` implements. The source is Meta's *WhatsApp Agent Platform
Developer Manual, Version 1 (2026-08-25)*, as implemented and tested by the open-source
[hermes-whatsapp-agent-platform](https://github.com/ahtishamdilawar/hermes-whatsapp-agent-platform)
plugin. Meta's own pages block automated readers, so check the manual if something disagrees.

## Setup

WhatsApp → Settings → Agents → Create an agent (name, photo) → open the agent's chat → Chat info →
API key. Up to 5 agents per account. Regenerating the key, or reinstalling WhatsApp, kills the old one.

## API

Base URL `https://api.whatsapp.com/agent/v1`, header `Authorization: Bearer <key>`.

| Call | Purpose |
|---|---|
| `GET /updates?offset=N&timeout=20&limit=50` | Long poll (timeout ≤ 25 s, limit ≤ 100). **200** with messages and `next_offset`, or **204** when nothing arrived. Polling doesn't consume messages: you advance by passing `next_offset` back. |
| `POST /messages` | Send. `{"messaging_product":"whatsapp","to":"user:…","type":"text","text":{"body":"…"}}`, optional `context.message_id` to quote a message. Returns `messages[0].id`. Text ≤ 4096 characters. |
| `POST /statuses` | `{"status":"read","message_id":…}`, plus optional `typing_indicator: {type: text}` (shows ~25 s). Accepted only for the creator's own messages, which makes it a reliable "is this the creator?" check. |
| `GET /media/<id>` | Download info for inbound media: `url` (on `lookaside.fbsbx.com`), `mime_type`, `sha256`, `file_size`. Download it with the same bearer token. |
| `POST /media` | Upload outbound media (multipart) → media id, then send it as `type: image/audio/video/document`. |

Inbound message shape (inside `entry[].changes[].value.messages[]`):

```json
{"from": "user:509…", "id": "wamid.…", "timestamp": "1736844652", "type": "text", "text": {"body": "Hello"}}
{"from": "user:509…", "id": "wamid.…", "type": "audio",
 "audio": {"id": "5f63…", "mime_type": "audio/ogg", "sha256": "…", "voice": true}}
```

## Rules and limits

- **Creator only.** The agent receives messages from, and can send only to, its creator.
  Proactive messages to the creator are allowed at any time; there's no 24-hour window like the
  Business API has.
- **Per agent, per rolling minute:** 12 messages, 12 statuses, 15 polls, 12 media uploads,
  12 media lookups.
- **One poller per key.** A newer `GET /updates` replaces the running one, which gets HTTP 409.
- **Can't:** edit or delete messages, send reactions, send voice-note bubbles (audio goes out as a
  file), buttons, lists, groups.
- **Not end-to-end encrypted.** A Meta service runs agent chats on the developer's behalf.
- **Media limits out:** images 5 MiB (JPEG/PNG), everything else 16 MiB. Captions ≤ 1024 characters.

## Error codes

| HTTP / code | Meaning | What the client does |
|---|---|---|
| 401, code 190 | Authorization header missing or malformed | `AuthFailed` (stop) |
| 400, code 100 on `/updates` | API key rejected | `AuthFailed` (stop) |
| 403, code 131005 | Not the agent's creator | `NotCreator` (ignore the sender) |
| 409, code 1752041 | Another poller replaced this one | `PollConflict` (pause 60 s; stop after 3 in 10 min) |
| 429, code 130429 | Rate limited (`Retry-After`) | `RateLimited` (wait, retry) |
| 503, code 131016 | Not accepted, retry later | back off, retry |
| 5xx | Outcome unknown | back off |
