import json

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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bot_token", "expected_token"),
    [("bot-token", "bot-token"), ("", "chatwoot-token")],
)
async def test_chatwoot_agent_bot_messages_use_the_bot_token(
    settings,
    bot_token,
    expected_token,
) -> None:
    settings.chatwoot_agent_bot_token = bot_token
    tokens: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        tokens.append(request.headers["api_access_token"])
        return httpx.Response(200, json={"id": 1})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    chatwoot = ChatwootClient(settings, client)
    await chatwoot.create_message(
        conversation_id=1, content="Hi", message_type="outgoing", as_agent_bot=True
    )
    await chatwoot.create_message(conversation_id=1, content="Hi", message_type="incoming")
    await client.aclose()

    assert tokens == [expected_token, "chatwoot-token"]


@pytest.mark.asyncio
async def test_chatwoot_message_with_attachments_is_sent_as_multipart(settings) -> None:
    settings.chatwoot_agent_bot_token = "bot-token"
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": 7})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    chatwoot = ChatwootClient(settings, client)
    response = await chatwoot.create_message(
        conversation_id=5,
        content="Contract",
        message_type="outgoing",
        content_attributes={"blip_message_id": "m-1", "blip_direction": "outbound"},
        as_agent_bot=True,
        attachments=[("Deal.pdf", b"%PDF-1.7", "application/pdf")],
    )
    await client.aclose()

    assert response == {"id": 7}
    request = requests[0]
    assert request.url.path == "/api/v1/accounts/10/conversations/5/messages"
    assert request.headers["api_access_token"] == "bot-token"
    assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
    body = request.content.decode("latin-1")
    for expected in (
        'name="content"\r\n\r\nContract',
        'name="message_type"\r\n\r\noutgoing',
        'name="private"\r\n\r\nfalse',
        'name="content_attributes[blip_message_id]"\r\n\r\nm-1',
        'name="content_attributes[blip_direction]"\r\n\r\noutbound',
        'name="attachments[]"; filename="Deal.pdf"\r\n'
        "Content-Type: application/pdf\r\n\r\n%PDF-1.7",
    ):
        assert expected in body


@pytest.mark.asyncio
async def test_chatwoot_message_without_attachments_stays_json(settings) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": 1})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await ChatwootClient(settings, client).create_message(
        conversation_id=5,
        content="Hi",
        message_type="incoming",
        content_attributes={"blip_message_id": "m-1"},
    )
    await client.aclose()

    assert requests[0].headers["content-type"] == "application/json"
    assert b'"content_attributes":{"blip_message_id":"m-1"}' in requests[0].content


@pytest.mark.asyncio
async def test_chatwoot_contact_is_read_and_updated_with_only_the_given_fields(settings) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"payload": {"id": 9}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    chatwoot = ChatwootClient(settings, client)
    await chatwoot.get_contact(contact_id=9)
    await chatwoot.update_contact(contact_id=9, name="Ana", custom_attributes={"crm_id": 1})
    await client.aclose()

    assert (requests[0].method, requests[0].url.path) == ("GET", "/api/v1/accounts/10/contacts/9")
    assert (requests[1].method, requests[1].url.path) == ("PUT", "/api/v1/accounts/10/contacts/9")
    assert requests[1].content == b'{"name":"Ana","custom_attributes":{"crm_id":1}}'


@pytest.mark.asyncio
async def test_chatwoot_agents_are_listed_created_and_added_to_the_inbox(settings) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": 7, "email": "a@example.com"}, "junk"])
        if request.url.path.endswith("/inbox_members"):
            # The real server wraps the member list as {"payload": [...]}; the docs show an array.
            return httpx.Response(200, json={"payload": [{"id": 7}, {"id": 8}]})
        return httpx.Response(200, json={"id": 8, "email": "b@example.com"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    chatwoot = ChatwootClient(settings, client)
    agents = await chatwoot.list_agents()
    created = await chatwoot.create_agent(name="B", email="b@example.com")
    members = await chatwoot.add_inbox_agents([8])
    await client.aclose()

    assert agents == [{"id": 7, "email": "a@example.com"}]
    assert created["id"] == 8
    assert members == [{"id": 7}, {"id": 8}]
    assert [(r.method, r.url.path) for r in requests] == [
        ("GET", "/api/v1/accounts/10/agents"),
        ("POST", "/api/v1/accounts/10/agents"),
        ("POST", "/api/v1/accounts/10/inbox_members"),
    ]
    assert json.loads(requests[1].content) == {
        "name": "B",
        "email": "b@example.com",
        "role": "agent",
    }
    assert json.loads(requests[2].content) == {"inbox_id": 20, "user_ids": [8]}


@pytest.mark.asyncio
async def test_chatwoot_conversation_is_assigned_to_a_user(settings) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": 7})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    chatwoot = ChatwootClient(settings, client)
    await chatwoot.assign_conversation(conversation_id=5, assignee_id=7)
    await client.aclose()

    assert requests[0].url.path == "/api/v1/accounts/10/conversations/5/assignments"
    assert json.loads(requests[0].content) == {"assignee_id": 7}


@pytest.mark.asyncio
async def test_chatwoot_dict_endpoints_ignore_non_object_bodies(settings) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    chatwoot = ChatwootClient(settings, client)
    response = await chatwoot.assign_conversation(conversation_id=5, assignee_id=7)
    await client.aclose()

    assert response == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("desk_key", "desk_expected", "other_expected"),
    [("desk-key", "Key desk-key", "Key blip-key"), ("", "Key blip-key", "Key blip-key")],
)
async def test_blip_desk_commands_use_the_desk_key_when_configured(
    settings,
    desk_key,
    desk_expected,
    other_expected,
) -> None:
    settings.blip_desk_auth_key = desk_key
    auth_by_target: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        auth_by_target[body.get("to", "")] = request.headers["Authorization"]
        return httpx.Response(200, json={"status": "success", "resource": {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    blip = BlipClient(settings, client)
    await blip.command(method="get", uri="/tags/active")
    await blip.command(method="get", uri="/account", to="postmaster@msging.net")
    await client.aclose()

    assert auth_by_target["postmaster@desk.msging.net"] == desk_expected
    assert auth_by_target["postmaster@msging.net"] == other_expected


@pytest.mark.asyncio
async def test_blip_attendants_are_read_page_by_page(settings) -> None:
    settings.blip_desk_auth_key = "desk-key"
    pages = {
        "/attendants?$skip=0&$take=100": ([{"fullName": f"A{i}"} for i in range(100)], 101),
        "/attendants?$skip=100&$take=100": ([{"fullName": "Last"}], 101),
    }
    uris: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        uris.append(body["uri"])
        assert body["to"] == "postmaster@desk.msging.net"
        assert body["method"] == "get"
        items, total = pages[body["uri"]]
        return httpx.Response(
            200, json={"status": "success", "resource": {"total": total, "items": items}}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    attendants = await BlipClient(settings, client).get_attendants()
    await client.aclose()

    assert len(attendants) == 101
    assert attendants[-1] == {"fullName": "Last"}
    assert uris == list(pages)


@pytest.mark.asyncio
async def test_blip_attendants_stop_on_an_empty_page(settings) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"status": "success", "resource": {"total": 5, "items": []}}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    attendants = await BlipClient(settings, client).get_attendants()
    await client.aclose()

    assert attendants == []
