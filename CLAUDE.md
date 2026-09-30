# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
pip install -e '.[dev]'    # Python >=3.11 (Docker image uses 3.12); dev extra = pytest, aiosqlite, ruff
pytest                     # asyncio_mode=auto, testpaths=tests
pytest tests/test_worker.py::test_terminal_blip_failure_is_acknowledged_as_failed   # single test
ruff check .               # line-length 100, rules E,F,I,UP,B
python -m compileall -q app tests

# Run locally — the API and the worker are separate processes and both are needed end to end
export DATABASE_URL=sqlite+aiosqlite:///./bridge.db   # .env.example points at the Compose `db` host, which doesn't resolve outside Docker
uvicorn app.main:app --reload
python -m app.worker

docker compose up --build  # Postgres + one-shot `migrate` + bridge (:8000) + worker
alembic upgrade head       # needed before starting against Postgres when AUTO_CREATE_SCHEMA=false
```

## Architecture

A **read-only mirror by default**: everything BLiP sends is copied into a Chatwoot API inbox — customer
messages as `incoming`, bot and BLiP Desk agent messages as `outgoing` posted with the Agent Bot token
(`CHATWOOT_AGENT_BOT_TOKEN`). Writing to BLiP is opt-in via three independent switches (see
`Settings.blip_writes_enabled`): `BLIP_ACK_MESSAGES` (`consumed`/`failed` notifications),
`CHATWOOT_REPLIES_TO_BLIP` (public agent replies → BLiP) and `BLIP_TICKET_TAG_SYNC_ENABLED` (labels ⇄
Desk ticket tags). Any new code that calls `BlipClient` must sit behind one of them. Two processes share
one database and communicate only through it.

**Webhook → outbox → worker pipeline**
- `app/api.py` handlers only authenticate, validate the envelope, and call `enqueue_event`
  (`app/services/queue.py`), which writes an `InboundEvent` + `OutboxJob` in one commit and dedupes on
  `UNIQUE(provider, external_id)`. They must stay fast and never call BLiP/Chatwoot.
- `app/worker.py::run_once` reclaims `processing` jobs stale >5 min, then claims one `pending` job with
  `FOR UPDATE SKIP LOCKED` (a no-op on SQLite) and dispatches on `job.kind`
  (`blip_message` / `blip_notification` / `blip_contact` / `chatwoot_event`) to a `BridgeService.process_*` method
  (`app/services/bridge.py`, the core logic).
- Failures: `IntegrationError.retryable` decides retry (backoff `min(300, 2**attempts)`s) vs. terminal;
  any other exception is treated as retryable. Terminal failures are reported back — Chatwoot gets the
  outgoing message marked `failed`, and BLiP gets a `failed` notification for a customer message only
  when `BLIP_ACK_MESSAGES` is on.
- `run_worker` also runs `reconcile_blip_tags` every `BLIP_LABEL_POLL_SECONDS` when tag sync is enabled.

**Idempotency / resumability** (every step must be safe to re-run after a crash or retry)
- `BridgeService` commits after each external side effect. `InboundEvent.result_id` stores the Chatwoot
  message id after step 1 so a retry skips re-creating it (and only re-sends the `consumed` ack when
  enabled); otherwise `_find_chatwoot_message` looks it up by `content_attributes.blip_message_id`.
- Agent replies use a deterministic BLiP message id (`uuid5` of Chatwoot account + message id);
  `MessageDelivery` has unique constraints on both ids. BLiP notification → Chatwoot status updates
  only move forward (`_is_monotonic_delivery_status`).
- `ConversationMapping` is keyed by the *full* BLiP customer node (`blip_bot_identity` +
  `blip_customer_identity`) and by Chatwoot account + conversation. `_MAPPING_LOCKS` are in-process
  asyncio locks only; cross-process safety comes from the unique constraints (`_create_mapping` handles
  `IntegrityError`).
- Chatwoot dedupe key is the `X-Chatwoot-Delivery` header, falling back to a body hash. When replies
  are enabled, only public outgoing messages from a `user` sender are forwarded, and only as
  `text/plain` (attachments become text placeholders).
- **Echo protection** — a mirrored `outgoing` message triggers a Chatwoot `message_created` webhook that
  must never reach BLiP. Layers: the replies switch; `content_attributes.blip_message_id` on the payload;
  a `MessageDelivery(status="mirrored")` row written in the same commit as `result_id`. The reverse echo
  (BLiP's copy of a message the bridge sent) is dropped because its `blip_message_id` already has a
  `MessageDelivery`. Chatwoot's webhook `sender.type` for agent bots is unverified, so don't rely on it.

**Webhook auth**: Chatwoot uses an HMAC signature + timestamp (`app/security.py`), enforced only if
`CHATWOOT_WEBHOOK_SECRET` is set. BLiP has no signature, so it uses a route path token (or
`X-Bridge-Token` header), enforced only if the token setting is non-empty. BLiP posts everything to one
URL, so `POST /`, `/webhooks/blip` and `/webhooks/blip/{path_token}` are a unified endpoint that
dispatches by payload shape (`_receive_blip_webhook`); the per-type `/webhooks/blip/messages|notifications`
routes (with and without `{path_token}`) remain. `POST /` can only authenticate via the header.

**BLiP payload families** (seen on real webhook traffic; rendering lives in `_blip_content_as_text`)
- Customer → bot (`from` = `<n>@wa.gw.msging.net`): mirrored as `incoming`. `text/plain` and
  `application/vnd.lime.reply+json` (quoted text via `_blip_reply_as_text`) render as text;
  `application/vnd.lime.reaction+json` as `[Reaction: <emoji>]` + quote (`emoji.values` are Unicode code
  points; empty = removed); other types fall back to `[BLiP message type: …]` + serialized JSON. Customer
  media (image/audio/document/location) has never been observed — no renderer beyond the generic `uri` path.
- Bot → customer (`from` = `<bot>@msging.net/<instance>`, ~80% of message envelopes): mirrored as `outgoing`
  with a `[BLiP agent: <email>]` / `[BLiP bot]` prefix (`#messageEmitter: Human` +
  `#message.agentIdentity`, URL-encoded `user%40domain@blip.ai`, mark a Desk agent). Routing lives in
  `_route_message`; `BLIP_BOT_IDENTITY` must match the bot node exactly or the bot's own messages would be
  treated as customers. They don't reopen a resolved conversation; customer messages do.
  - `application/json` with `templateContent` = WhatsApp template (campaign sends, `<bot>@msging.net` without
    instance, `#activecampaign.*` metadata): body with `{{n}}` filled from `template.components[body]`,
    plus `Options: [...]` for buttons. Text carries literal `\n`; params often arrive as the unresolved
    `${contact.extras.N}` and are shown as received.
  - `application/json` with `interactive` (`button`, `flow`; `list` is handled but unobserved): body +
    `Options: [...]`.
  - `application/vnd.lime.media-link+json` (agent files): `uri` is a SAS URL valid ~30 min with `sig=` in the
    query → downloaded and attached (see `MediaDownloader`), text link without the query as fallback.
- Campaign templates go to `activecampaign:<uuid>@broadcast.msging.net`, a different node than the customer's
  `<n>@wa.gw.msging.net`, so each one gets its own Chatwoot contact/conversation (named with that raw identity)
  and the customer's reply lands in another one. Nothing in the payload links the two.
- Never mirrored: typing signals (`application/json` with `typing_indicator`, `chatstate`) and Desk ticket
  envelopes (`application/vnd.iris.*`, `from` = the customer). Only ticket `status: Waiting` (with `team`,
  `ownerIdentity`, sometimes `agentIdentity`) has been seen; no close event, so nothing resolves a conversation.
- Tracking events (`identity` + `category`/`action`, no `from`) are acknowledged with 200 and dropped in
  `app/api.py` without persisting.
- Contact updates (`identity` + `lastMessageDate`, no `from`; `name`, `email`, `phoneNumber`, `taxDocument`,
  ~60 `extras` keys, all student PII) are reduced by `extract_contact_profile` to an **allow-list**
  (`app/services/contact_profile.py`) *before* anything is stored, then synced onto the Chatwoot contact
  (`BLIP_CONTACT_SYNC`). Add a field by extending that allow-list only, never by storing the raw payload.
  Stored as `event_type="contact"` with `external_id = contact:<identity>:<digest>:<previous event id>`
  (only changes are stored; the previous id keeps A → B → A from being swallowed by dedupe). Job kind
  `blip_contact` → `process_blip_contact`: no mapping yet ⇒ status `deferred`, applied by `_create_mapping`
  (best effort, never blocks the message); otherwise GET + merge custom attributes + PUT. It never renames a
  contact whose name isn't a phone placeholder or the last name it set, and a 422 on the e-mail (unique per
  account) retries without it.

**Config & schema**
- `app/config.py::Settings` (pydantic-settings, `.env`). `validate_for_environment()` is a no-op unless
  `APP_ENV=production`, where it requires the credentials/tokens and `postgresql+asyncpg://`
  (`BLIP_CONTRACT_ID`/`BLIP_AUTH_KEY` only when a BLiP write switch is on). It runs in
  `create_runtime` and in `migrations/env.py`.
- `app/runtime.py::Runtime` bundles settings, engine, session factory, shared `httpx.AsyncClient`, the
  two API clients and the `MediaDownloader`; it lives on `app.state.runtime` (API) or is built in
  `app.worker.main`.
- `app/integrations/media.py::MediaDownloader` fetches BLiP media for `BridgeService._fetch_blip_media`
  (`BLIP_MEDIA_ATTACHMENTS`, `BLIP_MEDIA_MAX_BYTES`, `BLIP_MEDIA_ALLOWED_HOSTS`). It isn't `BlipClient` (no
  write switch applies) but it fetches URLs taken from webhook payloads that may be unauthenticated, so the
  https + host allow-list check must stay. Permanent problems (host, expired link, too large) return `None`
  and the message falls back to a text link; timeouts/429/5xx raise a retryable `IntegrationError`.
- Schema: `AUTO_CREATE_SCHEMA=true` runs `Base.metadata.create_all` at startup (dev/SQLite). Production
  uses Alembic — changing `app/models.py` requires a hand-checked new revision under
  `migrations/versions/` (only `0001_initial` exists). `migrations/env.py` takes the DB URL from
  `Settings`, not `alembic.ini`.
- `BLIP_TICKET_TAG_SYNC_ENABLED` (default off) gates shadow BLiP Desk tickets and label sync; failures
  creating a shadow ticket must not block message delivery.

## Local traffic archive (`.local/`, untracked)

`.local/` is gitignored and holds **real PII** (phones, customer text, student `extras`, agent e-mails):
`db/` (consistent SQLite backups), `captures/` (ngrok inspector exports), `logs/` and `reports/` (masked
reviews). Read its `README.md` first; mask anything printed, and never turn its payloads into test
fixtures (tests use synthetic payloads). The ngrok inspector (`http://127.0.0.1:4040/api/requests/http`) is
in-memory — export it before restarting ngrok. When starting the processes locally behind the tunnel, use
`--port 8080` and append the output to `.local/logs/api.log` / `worker.log`.

## Testing conventions

- Tests use in-memory SQLite via the `session_factory` and `settings` fixtures in `tests/conftest.py`.
- API tests mount `app.api.router` on a bare `FastAPI()` with
  `app.state.runtime = SimpleNamespace(settings=..., session_factory=...)` and call it through
  `httpx.ASGITransport` — the lifespan is not run.
- `BridgeService` tests use `AsyncMock(spec=BlipClient)` / `AsyncMock(spec=ChatwootClient)`; worker tests
  pass a `SimpleNamespace` runtime to `run_once`. The HTTP clients are tested with `httpx.MockTransport`
  (`respx` is a declared dev dependency but currently unused).
