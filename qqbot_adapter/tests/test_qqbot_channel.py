import time
from typing import Any

import pytest

from qqbot_adapter.channels.qqbot_channel import QQBotChannel
from qqbot_adapter.core.bus import MessageBus
from qqbot_adapter.core.events import OutboundMessage


class _FakeResponse:
    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self._data = data or {"id": "sent"}
        self.status_code = 200
        self.text = "{}"

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._data


class _FakeHttp:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.posts.append({"url": url, **kwargs})
        return _FakeResponse()


def _make_channel() -> tuple[QQBotChannel, _FakeHttp]:
    channel = QQBotChannel(bus=MessageBus(), app_id="app", client_secret="secret")
    http = _FakeHttp()
    channel._http = http
    channel._access_token = "token"
    channel._token_expires_at = time.monotonic() + 1000
    return channel, http


@pytest.mark.asyncio
async def test_send_message_uses_inbound_message_id_for_passive_reply() -> None:
    channel, http = _make_channel()

    await channel.send_message(chat_id="openid-a", content="hello", reply_to="incoming-msg")
    await channel.send_message(chat_id="openid-a", content="again", reply_to="incoming-msg")

    assert http.posts[0]["json"]["msg_id"] == "incoming-msg"
    assert http.posts[0]["json"]["msg_seq"] == 1
    assert http.posts[1]["json"]["msg_id"] == "incoming-msg"
    assert http.posts[1]["json"]["msg_seq"] == 2


@pytest.mark.asyncio
async def test_send_message_omits_msg_id_when_not_replying() -> None:
    channel, http = _make_channel()

    await channel.send_message(chat_id="openid-a", content="active")

    assert "msg_id" not in http.posts[0]["json"]
    assert "msg_seq" not in http.posts[0]["json"]


@pytest.mark.asyncio
async def test_outbound_callback_forwards_reply_to() -> None:
    channel, http = _make_channel()

    await channel._outbound_callback(
        OutboundMessage(
            channel_type="qqbot",
            chat_id="openid-a",
            content="reply",
            reply_to="incoming-msg",
        )
    )

    assert http.posts[0]["json"]["msg_id"] == "incoming-msg"


@pytest.mark.asyncio
async def test_group_allow_all_accepts_unconfigured_group() -> None:
    bus = MessageBus()
    channel = QQBotChannel(
        bus=bus,
        app_id="app",
        client_secret="secret",
        allow_all=True,
        groups=[],
    )

    await channel._on_group_message(
        {
            "id": "msg-1",
            "group_openid": "group-a",
            "author": {"member_openid": "member-a", "username": "Tester"},
            "content": "<@!12345> hello",
        }
    )

    inbound = await bus.consume_inbound()

    assert inbound.channel_type == "qqbot"
    assert inbound.chat_type == "group"
    assert inbound.chat_id == "ggroup-a"
    assert inbound.user_id == "member-a"
    assert inbound.message_id == "msg-1"
    assert inbound.content == "hello"
