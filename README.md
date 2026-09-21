# BLiP to Chatwoot bridge

This service connects a BLiP bot HTTP endpoint to a Chatwoot API inbox. Customer
messages become incoming Chatwoot messages, and public agent replies are sent
back to the original BLiP customer identity.

## Scope

- WhatsApp identities are supported first; the full BLiP node is preserved as
  the mapping key.
- Text messages are supported directly.
- Unsupported BLiP content is represented in Chatwoot as an explicit fallback
  message instead of being discarded.
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
5. Do not configure multiple Chatwoot callbacks for the same events unless the
   duplicate deliveries are intentionally routed to this same idempotent
   endpoint.

The API token needs permission to create contacts, conversations, messages,
labels, and message statuses.

## BLiP configuration

Configure the bot's HTTP connection information as follows:

- Message URL:
  `https://<public-host>/webhooks/blip/messages/<BLIP_INBOUND_PATH_TOKEN>`
- Notification URL:
  `https://<public-host>/webhooks/blip/notifications/<BLIP_NOTIFICATION_PATH_TOKEN>`

Set `BLIP_CONTRACT_ID`, `BLIP_AUTH_KEY`, and `BLIP_BOT_IDENTITY`. The bridge
sends messages to:
`https://<contract_id>.http.msging.net/messages` using `Authorization: Key ...`.

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

## Operational behavior

- Webhook handlers persist events and return quickly.
- A PostgreSQL-backed outbox retries transient failures.
- Duplicate BLiP messages and Chatwoot deliveries are ignored.
- Agent replies use deterministic BLiP message IDs derived from the Chatwoot
  message ID to make retries traceable.
- Message payloads can contain personal data; production deployments should
  restrict database access and configure appropriate retention.

## Tests

```bash
pytest
ruff check .
python -m compileall -q app tests
```
