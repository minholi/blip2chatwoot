# BLiP to Chatwoot bridge

This service mirrors a BLiP bot into a Chatwoot API inbox. Customer messages become
incoming Chatwoot messages, and messages sent by the bot or by BLiP Desk agents become
outgoing messages posted by a Chatwoot Agent Bot. Sending public Chatwoot replies back
to the BLiP customer is optional and disabled by default.

## Scope

- WhatsApp identities are supported first; the full BLiP node is preserved as
  the mapping key.
- Text messages and quoted replies are supported directly. WhatsApp templates
  (body with the send parameters filled in, plus buttons), interactive messages
  (body plus the buttons/rows offered) and reactions (emoji plus the quoted text) are
  rendered as readable text.
- Media links are downloaded and attached to the Chatwoot message, because BLiP's
  signed URLs expire about 30 minutes after the message. Only https hosts in
  `BLIP_MEDIA_ALLOWED_HOSTS` are fetched, files over `BLIP_MEDIA_MAX_BYTES` are
  skipped, and `BLIP_MEDIA_ATTACHMENTS=false` turns this off. Whenever a file cannot
  be fetched, the message falls back to a text link (without the signature) with type
  and caption; transient download errors are retried like any other failure.
- Typing indicators, BLiP Desk ticket envelopes and tracking events are
  acknowledged and not mirrored.
- BLiP contact updates are reduced to a fixed set of fields and copied onto the
  Chatwoot contact (see "Contact data"). `BLIP_CONTACT_SYNC=false` drops them
  unstored instead.
- Unsupported BLiP content is represented in Chatwoot as an explicit fallback
  message instead of being discarded.
- By default the bridge never writes to BLiP (read-only mirror). Three independent
  switches opt in: `BLIP_ACK_MESSAGES` (send `consumed`/`failed` notifications),
  `CHATWOOT_REPLIES_TO_BLIP` (forward public agent replies) and
  `BLIP_TICKET_TAG_SYNC_ENABLED`.
- Chatwoot webhook signatures are verified when
  `CHATWOOT_WEBHOOK_SECRET` is configured.
- BLiP notification callbacks update Chatwoot message delivery status.
- Optional shadow BLiP Desk tickets synchronize Chatwoot conversation labels
  with BLiP ticket tags.

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
# For a direct local process, use SQLite instead of the Compose database host.
export DATABASE_URL=sqlite+aiosqlite:///./bridge.db
uvicorn app.main:app --reload
python -m app.worker
```

For PostgreSQL and separate processes:

```bash
docker compose up --build
```

The Compose `migrate` service applies Alembic migrations before the API and
worker start. For a direct PostgreSQL deployment, run `alembic upgrade head`
before starting either process and set `AUTO_CREATE_SCHEMA=false`.

The API is available on `http://localhost:8000`. Health endpoints are
`/healthz` and `/readyz`.

## Chatwoot configuration

1. Create an **API** inbox and add the agents who will answer conversations.
2. Set `CHATWOOT_BASE_URL`, `CHATWOOT_ACCOUNT_ID`, `CHATWOOT_API_TOKEN`, and
   `CHATWOOT_INBOX_ID`.
3. Create an account webhook pointing to `/webhooks/chatwoot` with these
   subscriptions:
   `message_created`, `conversation_updated`, and
   `conversation_status_changed`.
4. Store the generated webhook secret as `CHATWOOT_WEBHOOK_SECRET`.
5. Create an Agent Bot, attach it to the inbox and set its access token as
   `CHATWOOT_AGENT_BOT_TOKEN`. Mirrored bot and BLiP agent messages are posted as
   this bot, so they are not attributed to a human user. Without it the bridge falls
   back to `CHATWOOT_API_TOKEN`.
6. Do not configure multiple Chatwoot callbacks for the same events unless the
   duplicate deliveries are intentionally routed to this same idempotent
   endpoint.

The API token needs permission to create contacts, conversations, messages,
labels, and message statuses.

## BLiP configuration

Point the BLiP webhook at the unified endpoint, which accepts everything BLiP sends
on one URL and dispatches by payload shape (messages in either direction,
notifications, tracking events, which are acknowledged and dropped, and contact
updates, which are synced onto the Chatwoot contact):

- `https://<public-host>/webhooks/blip/<BLIP_INBOUND_PATH_TOKEN>`

`POST /webhooks/blip` is accepted too, but then the token has to be sent as an
`X-Bridge-Token` header, which BLiP cannot do by itself. There is no route at the
root (`/`): a BLiP webhook still pointing at it gets a 404.

The legacy per-type URLs still work:

- Message URL:
  `https://<public-host>/webhooks/blip/messages/<BLIP_INBOUND_PATH_TOKEN>`
- Notification URL:
  `https://<public-host>/webhooks/blip/notifications/<BLIP_NOTIFICATION_PATH_TOKEN>`

Set `BLIP_BOT_IDENTITY` to the exact bot node (for example `mybot@msging.net`):
it separates customer messages from the copies of the bot's own messages, which
arrive with the sender `<bot>/<instance>`.

`BLIP_CONTRACT_ID` and `BLIP_AUTH_KEY` are only needed when a BLiP write switch is
on. The bridge then sends to `https://<contract_id>.http.msging.net/messages`
using `Authorization: Key ...`.

The route tokens are optional during local development, but high-entropy tokens
behind HTTPS are required in production because the BLiP HTTP callback contract
does not provide the same HMAC header used by Chatwoot webhooks.

## Optional ticket tags

Set `BLIP_TICKET_TAG_SYNC_ENABLED=true` only after validating the operational
effect in a test bot. The bridge creates a shadow BLiP Desk ticket for each
mapped conversation and stores its internal ticket ID. Chatwoot labels are sent
to `/tickets/{ticketId}/change-tags`; BLiP tag changes are reconciled by the
worker using `/ticket/{ticketId}`.

Tags must already exist as active BLiP Desk tags. The feature is disabled by
default and does not block message delivery if shadow-ticket creation fails.

## Contact data

BLiP sends a contact update whenever a customer's profile changes. The bridge
keeps only what `app/services/contact_profile.py` allows and drops the rest
before storing anything (bot state, campaign ids, ActiveCampaign links, the
customer's own phone field):

- `name` and `email` become the standard Chatwoot contact fields.
- `taxDocument` (CPF, when filled) becomes the custom attribute `cpf`.
- A fixed list of `extras` becomes custom attributes: lead profile (`crm_id`,
  `area_atuacao`, `formacao`, `ano_conclusao_graduacao`, `motivacao`, `lead_score`,
  `prioridade`, `origin`, `recent_origin`, `web_voucher`, `vendedor_email`) and
  student data (`ra`, `curso`, `modalidade`, `situacao_aluno`, `win_usuario`, `score`).

Only changes are stored. The worker never replaces a name an agent typed in
Chatwoot (it renames the phone-number placeholder and names it set itself), keeps
custom attributes it does not manage, and still updates the rest of the profile
when Chatwoot refuses an e-mail already used by another contact. A profile that
arrives before the customer's first message is applied when the conversation is
created; a failure there never blocks the message.

These fields are personal data: they are stored in `inbound_events`/`outbox_jobs`
and in Chatwoot, so restrict access and set a retention period for both.

## Operational behavior

- Webhook handlers persist events and return quickly.
- A PostgreSQL-backed outbox retries transient failures.
- Duplicate BLiP messages and Chatwoot deliveries are ignored.
- Agent replies use deterministic BLiP message IDs derived from the Chatwoot
  message ID to make retries traceable.
- Echo protection: mirrored messages are recorded as `mirrored` deliveries and
  carry `content_attributes.blip_message_id`, so their Chatwoot webhook is never
  forwarded back to BLiP; the BLiP copy of a message the bridge sent is not
  mirrored again.
- Mirrored messages are timestamped when they are processed, not when BLiP sent
  them, and a bot or agent message to a customer with no conversation creates one.
- Message payloads can contain personal data; production deployments should
  restrict database access and configure appropriate retention.

## Tests

```bash
pytest
ruff check .
python -m compileall -q app tests
```
