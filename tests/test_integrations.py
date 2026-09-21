import httpx
import pytest

from app.integrations.blip import BlipClient
from app.integrations.chatwoot import ChatwootClient


@pytest.mark.asyncio
async def test_blip_client_sends_key_authorized_message(settings) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "accepted"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    blip = BlipClient(settings, client)
    await blip.send_message(
        to="551199999999@wa.gw.msging.net",
        message_type="text/plain",
        content="Hello",
        message_id="message-1",
    )
    await client.aclose()

    assert requests[0].headers["Authorization"] == "Key blip-key"
    assert requests[0].url.path.endswith("/messages")
    assert requests[0].content == (
        b'{"id":"message-1","to":"551199999999@wa.gw.msging.net",'
        b'"type":"text/plain","content":"Hello"}'
    )


@pytest.mark.asyncio
async def test_chatwoot_client_creates_api_inbox_contact(settings) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": 123,
                "contact_inboxes": [{"source_id": "source-1", "inbox": {"id": 20}}],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    chatwoot = ChatwootClient(settings, client)
    response = await chatwoot.create_contact(
        name="+551199999999",
        identifier="551199999999@wa.gw.msging.net",
        phone_number="+551199999999",
    )
    await client.aclose()

    assert response["id"] == 123
    assert requests[0].headers["api_access_token"] == "chatwoot-token"
    assert requests[0].url.path == "/api/v1/accounts/10/contacts"
    assert requests[0].content.decode().find('"inbox_id":20') >= 0
