import base64
import time
from io import BytesIO
from uuid import uuid4

import httpx
from PIL import Image

from tests.auth_helpers import register_workspace_user


def _create_team_user_and_conversation(client, suffix: str) -> tuple[str, str, str]:
    team_id, user_id = register_workspace_user(
        client,
        prefix=f"hist_{suffix}",
        display_name="History User",
    )

    create_conversation = client.post(
        "/api/v1/conversations",
        json={"title": "History Session"},
    )
    assert create_conversation.status_code == 201
    conversation_id = create_conversation.json()["conversation_id"]
    assert create_conversation.json()["team_id"] == team_id
    assert create_conversation.json()["user_id"] == user_id

    return team_id, user_id, conversation_id


def _resolve_conversation_space_id(
    client,
    *,
    team_id: str,
    user_id: str,
    conversation_id: str,
) -> str:
    list_response = client.get(
        "/api/v1/conversations",
        params={"limit": 50},
    )
    assert list_response.status_code == 200
    for item in list_response.json():
        if item["conversation_id"] == conversation_id:
            return item["space_id"]
    raise AssertionError("conversation space_id not found")


def _build_png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (4, 4), color=(240, 240, 240)).save(output, format="PNG")
    return output.getvalue()


def _wait_document_ready(client, *, team_id: str, conversation_id: str, document_id: str, max_attempts: int = 30) -> None:
    for _ in range(max_attempts):
        response = client.get(
            f"/api/v1/documents/{document_id}",
            params={"team_id": team_id, "conversation_id": conversation_id},
        )
        assert response.status_code == 200
        status = response.json()["status"]
        if status == "ready":
            return
        assert status != "failed"
        time.sleep(0.05)
    raise AssertionError("document did not reach ready status in time")


def test_chat_echo_requires_configured_user_team(client) -> None:
    team_id, user_id = register_workspace_user(
        client,
        prefix="echo",
        display_name="Alice",
    )

    chat_response = client.post(
        "/api/v1/chat/echo",
        json={"message": "hello"},
    )

    assert chat_response.status_code == 200
    body = chat_response.json()
    assert body["user_id"] == user_id
    assert body["team_id"] == team_id
    assert body["answer"] == "[Echo] hello"


def test_chat_echo_requires_authenticated_session(client) -> None:
    client.cookies.clear()
    response = client.post(
        "/api/v1/chat/echo",
        json={"message": "hello"},
    )

    assert response.status_code == 401


def test_chat_ask_returns_answer_and_hits(client) -> None:
    team_id, user_id = register_workspace_user(
        client,
        prefix="ask",
        display_name="Operator",
    )

    import_response = client.post(
        "/api/v1/documents/import",
        json={
            "team_id": team_id,
            "source_name": "ops.md",
            "content_type": "md",
            "content": (
                "# Ops Guide\n\n"
                "Always check alerts first. "
                "Escalate incidents quickly. "
                "Keep channels updated every 10 minutes."
            ),
        },
    )
    assert import_response.status_code == 201
    document_id = import_response.json()["document_id"]

    chunk_response = client.post(
        f"/api/v1/documents/{document_id}/chunk",
        json={
            "team_id": team_id,
            "max_chars": 100,
            "overlap": 10,
        },
    )
    assert chunk_response.status_code == 200

    index_response = client.post(
        "/api/v1/retrieval/index",
        json={
            "team_id": team_id,
            "document_id": document_id,
        },
    )
    assert index_response.status_code == 200

    ask_response = client.post(
        "/api/v1/chat/ask",
        json={
            "question": "alerts first?",
            "top_k": 3,
            "document_id": document_id,
        },
    )

    assert ask_response.status_code == 200
    body = ask_response.json()
    assert body["user_id"] == user_id
    assert body["team_id"] == team_id
    assert body["answer"]
    assert body["answer"].startswith("[Mock Answer]")
    assert len(body["hits"]) >= 1
    assert body["mode"] == "rag"
    assert len(body["sources"]) >= 1


def test_chat_ask_falls_back_to_chat_mode_without_index(client) -> None:
    team_id, user_id = register_workspace_user(
        client,
        prefix="ask_no_index",
        display_name="Operator",
    )

    ask_response = client.post(
        "/api/v1/chat/ask",
        json={
            "question": "alerts first?",
            "top_k": 3,
        },
    )

    assert ask_response.status_code == 200
    body = ask_response.json()
    assert body["user_id"] == user_id
    assert body["team_id"] == team_id
    assert body["answer"]
    assert body["answer"].startswith("[Mock Chat]")
    assert body["hits"] == []
    assert body["mode"] == "chat"
    assert body["sources"] == []


def test_chat_ask_supports_selected_document_ids_and_source_snippet(client) -> None:
    team_id, user_id = register_workspace_user(
        client,
        prefix="ask_selected",
        display_name="Operator",
    )

    alpha_doc = client.post(
        "/api/v1/documents/import",
        json={
            "team_id": team_id,
            "source_name": "alpha.md",
            "content_type": "md",
            "content": "# Alpha\n\nAlpha runbook says check alerts first.",
        },
    )
    beta_doc = client.post(
        "/api/v1/documents/import",
        json={
            "team_id": team_id,
            "source_name": "beta.md",
            "content_type": "md",
            "content": "# Beta\n\nBeta runbook says deploy on Friday.",
        },
    )
    assert alpha_doc.status_code == 201
    assert beta_doc.status_code == 201

    alpha_id = alpha_doc.json()["document_id"]
    beta_id = beta_doc.json()["document_id"]

    for document_id in [alpha_id, beta_id]:
        chunk_response = client.post(
            f"/api/v1/documents/{document_id}/chunk",
            json={
                "team_id": team_id,
                "max_chars": 100,
                "overlap": 10,
            },
        )
        assert chunk_response.status_code == 200

    index_response = client.post(
        "/api/v1/retrieval/index",
        json={
            "team_id": team_id,
            "document_ids": [alpha_id, beta_id],
        },
    )
    assert index_response.status_code == 200

    ask_response = client.post(
        "/api/v1/chat/ask",
        json={
            "user_id": user_id,
            "team_id": team_id,
            "question": "哪个文档提到 alerts first?",
            "selected_document_ids": [alpha_id],
            "top_k": 3,
        },
    )

    assert ask_response.status_code == 200
    body = ask_response.json()
    assert body["mode"] == "rag"
    assert body["sources"]
    assert body["sources"][0]["document_id"] == alpha_id
    assert body["sources"][0]["source_name"] == "alpha.md"
    assert body["sources"][0]["snippet"]
    assert all(hit["document_id"] == alpha_id for hit in body["hits"])


def test_chat_ask_supports_none_model_for_forced_mock(client) -> None:
    team_id, user_id = register_workspace_user(
        client,
        prefix="ask_none",
        display_name="Operator",
    )

    ask_response = client.post(
        "/api/v1/chat/ask",
        json={
            "user_id": user_id,
            "team_id": team_id,
            "question": "hello",
            "model": "none",
            "top_k": 3,
        },
    )

    assert ask_response.status_code == 200
    body = ask_response.json()
    assert body["answer"].startswith("[Mock Chat]")
    assert body["hits"] == []
    assert body["mode"] == "chat"
    assert body["sources"] == []


def test_chat_ask_prefers_multimodal_image_input_when_image_is_selected(client, monkeypatch) -> None:
    team_id, user_id = register_workspace_user(
        client,
        prefix="ask_image",
        display_name="Vision Operator",
    )

    create_conversation = client.post(
        "/api/v1/conversations",
        json={
            "team_id": "ignored-team",
            "user_id": "ignored-user",
            "title": "Vision Session",
        },
    )
    assert create_conversation.status_code == 201
    conversation_id = create_conversation.json()["conversation_id"]

    upsert_model = client.post(
        "/api/v1/llm/models",
        json={
            "team_id": team_id,
            "user_id": user_id,
            "model_name": "gpt-4.1-mini",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-test",
        },
    )
    assert upsert_model.status_code == 200

    upload_response = client.post(
        "/api/v1/documents/upload",
        data={
            "team_id": team_id,
            "user_id": user_id,
            "conversation_id": conversation_id,
            "auto_index": "true",
        },
        files={
            "file": ("receipt.png", _build_png_bytes(), "image/png"),
        },
    )
    assert upload_response.status_code == 201
    document_id = upload_response.json()["document_id"]
    _wait_document_ready(
        client,
        team_id=team_id,
        conversation_id=conversation_id,
        document_id=document_id,
    )

    captured_payload: dict[str, object] = {}

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return

        def json(self) -> dict[str, object]:
            return {
                "choices": [
                    {
                        "message": {
                            "content": "vision-answer",
                        }
                    }
                ]
            }

    def _fake_post(url, headers, json, timeout):  # noqa: ANN001
        captured_payload["url"] = url
        captured_payload["headers"] = headers
        captured_payload["json"] = json
        captured_payload["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr("app.services.llm_service.httpx.post", _fake_post)

    ask_response = client.post(
        "/api/v1/chat/ask",
        json={
            "user_id": user_id,
            "team_id": team_id,
            "conversation_id": conversation_id,
            "question": "请直接看图告诉我这是什么内容",
            "selected_document_ids": [document_id],
            "top_k": 3,
            "model": "gpt-4.1-mini",
        },
    )
    assert ask_response.status_code == 200
    assert ask_response.json()["answer"] == "vision-answer"

    user_message = captured_payload["json"]["messages"][1]  # type: ignore[index]
    assert isinstance(user_message["content"], list)
    assert user_message["content"][0]["type"] == "text"
    assert user_message["content"][1]["type"] == "image_url"
    assert str(user_message["content"][1]["image_url"]["url"]).startswith("data:image/png;base64,")


def test_chat_ask_falls_back_when_provider_rejects_multimodal_array_content(client, monkeypatch) -> None:
    team_id, user_id = register_workspace_user(
        client,
        prefix="ask_image_retry",
        display_name="Retry Operator",
    )

    create_conversation = client.post(
        "/api/v1/conversations",
        json={
            "team_id": "ignored-team",
            "user_id": "ignored-user",
            "title": "Retry Session",
        },
    )
    assert create_conversation.status_code == 201
    conversation_id = create_conversation.json()["conversation_id"]

    upsert_model = client.post(
        "/api/v1/llm/models",
        json={
            "team_id": team_id,
            "user_id": user_id,
            "model_name": "gpt-4.1-mini",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-test",
        },
    )
    assert upsert_model.status_code == 200

    upload_response = client.post(
        "/api/v1/documents/upload",
        data={
            "team_id": team_id,
            "user_id": user_id,
            "conversation_id": conversation_id,
            "auto_index": "true",
        },
        files={
            "file": ("receipt.png", _build_png_bytes(), "image/png"),
        },
    )
    assert upload_response.status_code == 201
    document_id = upload_response.json()["document_id"]
    _wait_document_ready(
        client,
        team_id=team_id,
        conversation_id=conversation_id,
        document_id=document_id,
    )

    captured_payloads: list[dict[str, object]] = []
    call_count = {"count": 0}

    class _FakeResponse:
        def __init__(self, body: dict[str, object], status_code: int = 200) -> None:
            self._body = body
            self.status_code = status_code

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
                response = httpx.Response(self.status_code, request=request, json=self._body)
                raise httpx.HTTPStatusError("request failed", request=request, response=response)

        def json(self) -> dict[str, object]:
            return self._body

    def _fake_post(url, headers, json, timeout):  # noqa: ANN001
        captured_payloads.append(json)
        call_count["count"] += 1
        if call_count["count"] == 1:
            return _FakeResponse(
                {
                    "error": {
                        "message": "Invalid type for 'messages[1].content': expected a string, but got an array instead."
                    }
                },
                status_code=400,
            )
        return _FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": "fallback-after-array-error",
                        }
                    }
                ]
            }
        )

    monkeypatch.setattr("app.services.llm_service.httpx.post", _fake_post)
    monkeypatch.setattr(
        "app.services.retrieval_enhancer.get_settings",
        lambda: type("S", (), {"retrieval_enhancement_enabled": False})(),
    )

    ask_response = client.post(
        "/api/v1/chat/ask",
        json={
            "user_id": user_id,
            "team_id": team_id,
            "conversation_id": conversation_id,
            "question": "Read the attached image.",
            "selected_document_ids": [document_id],
            "top_k": 3,
            "model": "gpt-4.1-mini",
        },
    )
    assert ask_response.status_code == 200
    assert ask_response.json()["answer"] == "fallback-after-array-error"
    assert len(captured_payloads) == 2
    assert isinstance(captured_payloads[0]["messages"][1]["content"], list)  # type: ignore[index]
    second_user_content = captured_payloads[1]["messages"][1]["content"]  # type: ignore[index]
    assert isinstance(second_user_content, str)
    assert "Question:" in second_user_content
    assert "Context:" in second_user_content
    assert "Image attachment: receipt.png" in second_user_content


def test_chat_ask_returns_image_content_parts_and_history_payload(client, monkeypatch) -> None:
    team_id, user_id = register_workspace_user(
        client,
        prefix="output_image",
        display_name="Image Operator",
    )
    image_bytes = b"\x89PNG\r\n\x1a\nPNGDATA"
    expected_data_url = f"data:image/png;base64,{base64.b64encode(image_bytes).decode('ascii')}"

    upsert_model = client.post(
        "/api/v1/llm/models",
        json={
            "team_id": team_id,
            "user_id": user_id,
            "model_name": "gpt-4.1-mini",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-test",
        },
    )
    assert upsert_model.status_code == 200

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return

        def json(self) -> dict[str, object]:
            return {
                "choices": [
                    {
                        "message": {
                            "content": [
                                {"type": "text", "text": "Here is the generated diagram."},
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": "https://cdn.example.com/generated/diagram.png?signature=test",
                                    },
                                },
                            ],
                        },
                        "finish_reason": "stop",
                    }
                ]
            }

    def _fake_post(url, headers, json, timeout):  # noqa: ANN001, ARG001
        return _FakeResponse()

    def _fake_get(url, follow_redirects, timeout):  # noqa: ANN001, ARG001
        assert url == "https://cdn.example.com/generated/diagram.png?signature=test"
        assert follow_redirects is True
        request = httpx.Request("GET", url)
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "image/png"},
            content=image_bytes,
        )

    monkeypatch.setattr("app.services.llm_service.httpx.post", _fake_post)
    monkeypatch.setattr("app.services.llm_service.httpx.get", _fake_get)

    ask_response = client.post(
        "/api/v1/chat/ask",
        json={
            "user_id": user_id,
            "team_id": team_id,
            "question": "Generate a diagram image.",
            "top_k": 3,
            "model": "gpt-4.1-mini",
        },
    )
    assert ask_response.status_code == 200
    body = ask_response.json()
    assert body["answer"] == "Here is the generated diagram."
    assert body["content_parts"] == [
        {
            "type": "text",
            "text": "Here is the generated diagram.",
            "url": None,
            "original_url": None,
            "mime_type": None,
            "alt": None,
        },
        {
            "type": "image",
            "text": None,
            "url": expected_data_url,
            "original_url": "https://cdn.example.com/generated/diagram.png?signature=test",
            "mime_type": "image/png",
            "alt": None,
        },
    ]

    history_response = client.get(
        "/api/v1/chat/history",
        params={
            "team_id": team_id,
            "user_id": user_id,
            "limit": 20,
        },
    )
    assert history_response.status_code == 200
    items = history_response.json()["items"]
    assert len(items) == 1
    assert items[0]["response_text"] == "Here is the generated diagram."
    assert items[0]["response_payload"]["content_parts"] == body["content_parts"]


def test_chat_ask_extracts_markdown_image_text_into_content_parts(client, monkeypatch) -> None:
    team_id, user_id = register_workspace_user(
        client,
        prefix="markdown_image",
        display_name="Markdown Operator",
    )

    upsert_model = client.post(
        "/api/v1/llm/models",
        json={
            "team_id": team_id,
            "user_id": user_id,
            "model_name": "gpt-4.1-mini",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-test",
        },
    )
    assert upsert_model.status_code == 200

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return

        def json(self) -> dict[str, object]:
            return {
                "choices": [
                    {
                        "message": {
                            "content": "![Generated Image](data:image/jpeg;base64,/9j/AAAA)",
                        },
                        "finish_reason": "stop",
                    }
                ]
            }

    def _fake_post(url, headers, json, timeout):  # noqa: ANN001, ARG001
        return _FakeResponse()

    monkeypatch.setattr("app.services.llm_service.httpx.post", _fake_post)

    ask_response = client.post(
        "/api/v1/chat/ask",
        json={
            "user_id": user_id,
            "team_id": team_id,
            "question": "Generate a diagram image.",
            "top_k": 3,
            "model": "gpt-4.1-mini",
        },
    )
    assert ask_response.status_code == 200
    body = ask_response.json()
    assert body["answer"] == "Image output"
    assert body["content_parts"] == [
        {
            "type": "image",
            "text": None,
            "url": "data:image/jpeg;base64,/9j/AAAA",
            "original_url": None,
            "mime_type": "image/jpeg",
            "alt": "Generated Image",
        }
    ]


def test_chat_ask_includes_recent_conversation_history_in_llm_payload(client, monkeypatch) -> None:
    suffix = uuid4().hex[:8]
    team_id, user_id, conversation_id = _create_team_user_and_conversation(client, suffix)

    first_ask = client.post(
        "/api/v1/chat/ask",
        json={
            "user_id": user_id,
            "team_id": team_id,
            "conversation_id": conversation_id,
            "question": "Remember that the project codename is Apollo.",
        },
    )
    assert first_ask.status_code == 200

    upsert_model = client.post(
        "/api/v1/llm/models",
        json={
            "team_id": team_id,
            "user_id": user_id,
            "model_name": "gpt-4.1-mini",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-test",
        },
    )
    assert upsert_model.status_code == 200

    captured_payload: dict[str, object] = {}

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return

        def json(self) -> dict[str, object]:
            return {
                "choices": [
                    {
                        "message": {
                            "content": "memory-answer",
                        }
                    }
                ]
            }

    def _fake_post(url, headers, json, timeout):  # noqa: ANN001
        captured_payload["url"] = url
        captured_payload["headers"] = headers
        captured_payload["json"] = json
        captured_payload["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr("app.services.llm_service.httpx.post", _fake_post)

    second_ask = client.post(
        "/api/v1/chat/ask",
        json={
            "user_id": user_id,
            "team_id": team_id,
            "conversation_id": conversation_id,
            "question": "What is the project codename?",
            "model": "gpt-4.1-mini",
        },
    )
    assert second_ask.status_code == 200
    assert second_ask.json()["answer"] == "memory-answer"

    messages = captured_payload["json"]["messages"]  # type: ignore[index]
    assert messages[1] == {"role": "user", "content": "Remember that the project codename is Apollo."}
    assert messages[2] == {
        "role": "assistant",
        "content": "[Mock Chat] Remember that the project codename is Apollo.",
    }
    assert messages[3] == {"role": "user", "content": "What is the project codename?"}


def test_chat_ask_injects_memory_when_include_memory_enabled(client, monkeypatch) -> None:
    suffix = uuid4().hex[:8]
    team_id, user_id, conversation_id = _create_team_user_and_conversation(client, suffix)
    space_id = _resolve_conversation_space_id(
        client,
        team_id=team_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )

    create_memory = client.post(
        "/api/v1/memory/cards",
        json={
            "team_id": team_id,
            "user_id": user_id,
            "space_id": space_id,
            "title": "Codename Memory",
            "content": "The project codename is Helios.",
            "category": "fact",
            "status": "active",
        },
    )
    assert create_memory.status_code == 201

    upsert_model = client.post(
        "/api/v1/llm/models",
        json={
            "team_id": team_id,
            "user_id": user_id,
            "model_name": "gpt-4.1-mini",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-test",
        },
    )
    assert upsert_model.status_code == 200

    captured_payload_with_memory: dict[str, object] = {}
    captured_payload_without_memory: dict[str, object] = {}

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return

        def json(self) -> dict[str, object]:
            return {
                "choices": [
                    {
                        "message": {
                            "content": "memory-aware-answer",
                        }
                    }
                ]
            }

    def _fake_post_with_memory(url, headers, json, timeout):  # noqa: ANN001
        captured_payload_with_memory["json"] = json
        return _FakeResponse()

    monkeypatch.setattr("app.services.llm_service.httpx.post", _fake_post_with_memory)
    ask_with_memory = client.post(
        "/api/v1/chat/ask",
        json={
            "user_id": user_id,
            "team_id": team_id,
            "conversation_id": conversation_id,
            "question": "What is the codename?",
            "model": "gpt-4.1-mini",
            "include_memory": True,
        },
    )
    assert ask_with_memory.status_code == 200

    messages_with_memory = captured_payload_with_memory["json"]["messages"]  # type: ignore[index]
    assert "Helios" in str(messages_with_memory)

    def _fake_post_without_memory(url, headers, json, timeout):  # noqa: ANN001
        captured_payload_without_memory["json"] = json
        return _FakeResponse()

    monkeypatch.setattr("app.services.llm_service.httpx.post", _fake_post_without_memory)
    ask_without_memory = client.post(
        "/api/v1/chat/ask",
        json={
            "user_id": user_id,
            "team_id": team_id,
            "conversation_id": conversation_id,
            "question": "What is the codename?",
            "model": "gpt-4.1-mini",
            "include_memory": False,
        },
    )
    assert ask_without_memory.status_code == 200

    messages_without_memory = captured_payload_without_memory["json"]["messages"]  # type: ignore[index]
    assert "Helios" not in str(messages_without_memory)


def test_chat_ask_skips_disabled_and_expired_memories(client, monkeypatch) -> None:
    suffix = uuid4().hex[:8]
    team_id, user_id, conversation_id = _create_team_user_and_conversation(client, suffix)
    space_id = _resolve_conversation_space_id(
        client,
        team_id=team_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )

    entries = [
        ("Active Memory", "Only this active memory should be used.", "active"),
        ("Disabled Memory", "This disabled memory must not be used.", "disabled"),
        ("Expired Memory", "This expired memory must not be used.", "expired"),
    ]
    for title, content, status in entries:
        response = client.post(
            "/api/v1/memory/cards",
            json={
                "team_id": team_id,
                "user_id": user_id,
                "space_id": space_id,
                "title": title,
                "content": content,
                "category": "fact",
                "status": status,
            },
        )
        assert response.status_code == 201

    upsert_model = client.post(
        "/api/v1/llm/models",
        json={
            "team_id": team_id,
            "user_id": user_id,
            "model_name": "gpt-4.1-mini",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-test",
        },
    )
    assert upsert_model.status_code == 200

    captured_payload: dict[str, object] = {}

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return

        def json(self) -> dict[str, object]:
            return {
                "choices": [
                    {
                        "message": {
                            "content": "filtered-memory-answer",
                        }
                    }
                ]
            }

    def _fake_post(url, headers, json, timeout):  # noqa: ANN001
        captured_payload["json"] = json
        return _FakeResponse()

    monkeypatch.setattr("app.services.llm_service.httpx.post", _fake_post)
    ask_response = client.post(
        "/api/v1/chat/ask",
        json={
            "user_id": user_id,
            "team_id": team_id,
            "conversation_id": conversation_id,
            "question": "Summarize memory state.",
            "model": "gpt-4.1-mini",
            "include_memory": True,
        },
    )
    assert ask_response.status_code == 200

    messages = captured_payload["json"]["messages"]  # type: ignore[index]
    serialized = str(messages)
    assert "Only this active memory should be used." in serialized
    assert "This disabled memory must not be used." not in serialized
    assert "This expired memory must not be used." not in serialized


def test_chat_ask_rejects_cross_user_conversation_access(client) -> None:
    owner_team_id, owner_user_id = register_workspace_user(
        client,
        prefix="owner",
        display_name="Owner",
    )

    create_conversation = client.post(
        "/api/v1/conversations",
        json={"title": "Owner Conversation"},
    )
    assert create_conversation.status_code == 201
    conversation_id = create_conversation.json()["conversation_id"]
    assert create_conversation.json()["team_id"] == owner_team_id
    assert create_conversation.json()["user_id"] == owner_user_id

    logout_response = client.post("/api/v1/auth/logout")
    assert logout_response.status_code == 204

    intruder_team_id, intruder_user_id = register_workspace_user(
        client,
        prefix="intruder",
        display_name="Intruder",
    )
    assert intruder_team_id != owner_team_id
    assert intruder_user_id != owner_user_id

    ask_response = client.post(
        "/api/v1/chat/ask",
        json={
            "conversation_id": conversation_id,
            "question": "Can I read this conversation?",
        },
    )
    assert ask_response.status_code == 404
    assert ask_response.json()["detail"] == f"Conversation '{conversation_id}' not found."


def test_chat_ask_include_conclusions_switch_controls_conclusion_retrieval(client) -> None:
    suffix = uuid4().hex[:8]
    team_id, user_id, conversation_id = _create_team_user_and_conversation(client, suffix)
    space_id = _resolve_conversation_space_id(
        client,
        team_id=team_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )

    create_conclusion = client.post(
        "/api/v1/conclusions",
        json={
            "team_id": team_id,
            "user_id": user_id,
            "space_id": space_id,
            "title": "Rollback Rule",
            "topic": "release",
            "content": "When canary error rate exceeds 5%, rollback immediately to the previous stable build.",
            "status": "draft",
        },
    )
    assert create_conclusion.status_code == 201
    conclusion_id = create_conclusion.json()["conclusion_id"]

    confirm_response = client.post(
        f"/api/v1/conclusions/{conclusion_id}/confirm",
        json={
            "team_id": team_id,
            "user_id": user_id,
            "target_status": "confirmed",
        },
    )
    assert confirm_response.status_code == 200
    assert confirm_response.json()["doc_sync_document_id"]

    ask_without_conclusions = client.post(
        "/api/v1/chat/ask",
        json={
            "user_id": user_id,
            "team_id": team_id,
            "conversation_id": conversation_id,
            "question": "What should we do when canary error rate exceeds 5%?",
            "include_library": False,
            "include_conclusions": False,
            "top_k": 3,
        },
    )
    assert ask_without_conclusions.status_code == 200
    assert ask_without_conclusions.json()["mode"] == "chat"
    assert ask_without_conclusions.json()["hits"] == []

    ask_with_conclusions = client.post(
        "/api/v1/chat/ask",
        json={
            "user_id": user_id,
            "team_id": team_id,
            "conversation_id": conversation_id,
            "question": "What should we do when canary error rate exceeds 5%?",
            "include_library": False,
            "include_conclusions": True,
            "top_k": 3,
        },
    )
    assert ask_with_conclusions.status_code == 200
    body = ask_with_conclusions.json()
    assert body["mode"] == "rag"
    assert len(body["hits"]) >= 1
    assert any(str(source.get("source_name", "")).startswith("conclusion-") for source in body["sources"])


def test_chat_ask_include_conclusions_respects_space_boundary(client) -> None:
    team_id, user_id = register_workspace_user(
        client,
        prefix="conclusion_space",
        display_name="Conclusion User",
    )

    create_space_a = client.post(
        "/api/v1/spaces",
        json={
            "team_id": team_id,
            "user_id": user_id,
            "name": "Space A",
        },
    )
    assert create_space_a.status_code == 201
    space_a = create_space_a.json()["space_id"]

    create_space_b = client.post(
        "/api/v1/spaces",
        json={
            "team_id": team_id,
            "user_id": user_id,
            "name": "Space B",
        },
    )
    assert create_space_b.status_code == 201
    space_b = create_space_b.json()["space_id"]

    create_conversation_a = client.post(
        "/api/v1/conversations",
        json={
            "team_id": "ignored-team",
            "user_id": "ignored-user",
            "space_id": space_a,
            "title": "Conversation A",
        },
    )
    assert create_conversation_a.status_code == 201
    conversation_a = create_conversation_a.json()["conversation_id"]

    create_conclusion = client.post(
        "/api/v1/conclusions",
        json={
            "team_id": team_id,
            "user_id": user_id,
            "space_id": space_b,
            "title": "Space B Rule",
            "topic": "ops",
            "content": "Deploy window for Space B starts at 01:00 UTC.",
        },
    )
    assert create_conclusion.status_code == 201
    conclusion_id = create_conclusion.json()["conclusion_id"]

    confirm_response = client.post(
        f"/api/v1/conclusions/{conclusion_id}/confirm",
        json={
            "team_id": team_id,
            "user_id": user_id,
            "target_status": "confirmed",
        },
    )
    assert confirm_response.status_code == 200

    ask_response = client.post(
        "/api/v1/chat/ask",
        json={
            "user_id": user_id,
            "team_id": team_id,
            "conversation_id": conversation_a,
            "question": "When does the deploy window start?",
            "include_library": False,
            "include_conclusions": True,
            "top_k": 3,
        },
    )
    assert ask_response.status_code == 200
    body = ask_response.json()
    assert body["mode"] == "chat"
    assert body["hits"] == []
    assert body["sources"] == []


def test_chat_action_create_incident(client) -> None:
    team_id, user_id = register_workspace_user(
        client,
        prefix="action",
        display_name="Runner",
    )

    response = client.post(
        "/api/v1/chat/action",
        json={
            "user_id": user_id,
            "team_id": team_id,
            "action": "create_incident",
            "arguments": {
                "title": "Database CPU usage above threshold",
                "severity": "P1",
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "create_incident"
    assert body["result"]["tool_name"] == "create_incident"
    assert body["result"]["incident"]["title"] == "Database CPU usage above threshold"
    assert body["result"]["incident"]["severity"] == "P1"
    assert body["result"]["incident"]["status"] == "open"


def test_chat_action_list_recent_documents(client) -> None:
    team_id, user_id = register_workspace_user(
        client,
        prefix="action_docs",
        display_name="Runner",
    )

    first_doc = client.post(
        "/api/v1/documents/import",
        json={
            "team_id": team_id,
            "source_name": "ops-a.md",
            "content_type": "md",
            "content": "A",
        },
    )
    assert first_doc.status_code == 201

    second_doc = client.post(
        "/api/v1/documents/import",
        json={
            "team_id": team_id,
            "source_name": "ops-b.md",
            "content_type": "md",
            "content": "B",
        },
    )
    assert second_doc.status_code == 201

    response = client.post(
        "/api/v1/chat/action",
        json={
            "user_id": user_id,
            "team_id": team_id,
            "action": "list_recent_documents",
            "arguments": {"limit": 2},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["result"]["tool_name"] == "list_recent_documents"
    assert len(body["result"]["documents"]) == 2
    assert body["result"]["documents"][0]["source_name"] == "ops-b.md"
    assert body["result"]["documents"][1]["source_name"] == "ops-a.md"


def test_chat_action_rejects_unknown_action(client) -> None:
    team_id, user_id = register_workspace_user(
        client,
        prefix="action_unknown",
        display_name="Runner",
    )

    response = client.post(
        "/api/v1/chat/action",
        json={
            "user_id": user_id,
            "team_id": team_id,
            "action": "shutdown_cluster",
            "arguments": {},
        },
    )

    assert response.status_code == 400

def test_chat_action_rejects_invalid_incident_payload(client) -> None:
    team_id, user_id = register_workspace_user(
        client,
        prefix="action_invalid",
        display_name="Runner",
    )

    response = client.post(
        "/api/v1/chat/action",
        json={
            "user_id": user_id,
            "team_id": team_id,
            "action": "create_incident",
            "arguments": {},
        },
    )

    assert response.status_code == 400


def test_chat_action_rejects_dangerous_generic_write_file(client) -> None:
    """Dangerous generic tools must NOT execute via /chat/action."""
    team_id, user_id = register_workspace_user(
        client,
        prefix="action_dgwf",
        display_name="Runner",
    )

    response = client.post(
        "/api/v1/chat/action",
        json={
            "user_id": user_id,
            "team_id": team_id,
            "action": "write_file",
            "arguments": {"path": "evil.txt", "content": "pwned"},
        },
    )

    assert response.status_code == 400
    detail = response.json()["detail"].lower()
    assert "dangerous" in detail or "cannot be executed" in detail


def test_chat_action_rejects_dangerous_generic_edit_file(client) -> None:
    team_id, user_id = register_workspace_user(
        client,
        prefix="action_dgef",
        display_name="Runner",
    )

    response = client.post(
        "/api/v1/chat/action",
        json={
            "user_id": user_id,
            "team_id": team_id,
            "action": "edit_file",
            "arguments": {"path": "cfg.txt", "old_text": "a", "new_text": "b"},
        },
    )

    assert response.status_code == 400


def test_chat_action_rejects_dangerous_generic_shell_exec(client) -> None:
    team_id, user_id = register_workspace_user(
        client,
        prefix="action_dgse",
        display_name="Runner",
    )

    response = client.post(
        "/api/v1/chat/action",
        json={
            "user_id": user_id,
            "team_id": team_id,
            "action": "shell_exec",
            "arguments": {"command": "echo hello"},
        },
    )

    assert response.status_code == 400


def test_chat_action_rejects_dangerous_mcp_tool(client) -> None:
    """Dangerous MCP tools must NOT execute via /chat/action."""
    team_id, user_id = register_workspace_user(
        client,
        prefix="action_dgmt",
        display_name="Runner",
    )

    response = client.post(
        "/api/v1/chat/action",
        json={
            "user_id": user_id,
            "team_id": team_id,
            "action": "mcp__demo__destroy",
            "arguments": {},
        },
    )

    assert response.status_code == 400


def test_chat_ask_rejects_unconfigured_custom_model(client) -> None:
    team_id, user_id = register_workspace_user(
        client,
        prefix="ask_model",
        display_name="Operator",
    )

    ask_response = client.post(
        "/api/v1/chat/ask",
        json={
            "user_id": user_id,
            "team_id": team_id,
            "question": "hello",
            "model": "gpt-4.1-mini",
        },
    )
    assert ask_response.status_code == 404


def test_chat_history_delete_message(client) -> None:
    suffix = uuid4().hex[:8]
    team_id, user_id, conversation_id = _create_team_user_and_conversation(client, suffix)

    ask_response = client.post(
        "/api/v1/chat/ask",
        json={
            "user_id": user_id,
            "team_id": team_id,
            "conversation_id": conversation_id,
            "question": "first message",
        },
    )
    assert ask_response.status_code == 200

    history_before = client.get(
        "/api/v1/chat/history",
        params={
            "team_id": team_id,
            "user_id": user_id,
            "conversation_id": conversation_id,
            "limit": 20,
        },
    )
    assert history_before.status_code == 200
    assert len(history_before.json()["items"]) == 1
    message_id = history_before.json()["items"][0]["message_id"]

    delete_response = client.delete(
        f"/api/v1/chat/history/{message_id}",
        params={
            "team_id": team_id,
            "user_id": user_id,
            "conversation_id": conversation_id,
        },
    )
    assert delete_response.status_code == 204

    history_after = client.get(
        "/api/v1/chat/history",
        params={
            "team_id": team_id,
            "user_id": user_id,
            "conversation_id": conversation_id,
            "limit": 20,
        },
    )
    assert history_after.status_code == 200
    assert history_after.json()["items"] == []


def test_chat_history_edit_user_message(client) -> None:
    suffix = uuid4().hex[:8]
    team_id, user_id, conversation_id = _create_team_user_and_conversation(client, suffix)

    ask_response = client.post(
        "/api/v1/chat/ask",
        json={
            "conversation_id": conversation_id,
            "question": "old text",
        },
    )
    assert ask_response.status_code == 200

    history_before = client.get(
        "/api/v1/chat/history",
        params={
            "team_id": team_id,
            "user_id": user_id,
            "conversation_id": conversation_id,
            "limit": 20,
        },
    )
    assert history_before.status_code == 200
    assert len(history_before.json()["items"]) == 1
    item_before = history_before.json()["items"][0]
    message_id = item_before["message_id"]

    edit_response = client.put(
        f"/api/v1/chat/history/{message_id}",
        json={"request_text": "new text"},
    )
    assert edit_response.status_code == 200
    edited = edit_response.json()
    assert edited["request_text"] == "new text"
    assert edited["response_text"].startswith("[Mock Chat]")

    history_after = client.get(
        "/api/v1/chat/history",
        params={
            "team_id": team_id,
            "user_id": user_id,
            "conversation_id": conversation_id,
            "limit": 20,
        },
    )
    assert history_after.status_code == 200
    assert len(history_after.json()["items"]) == 1
    item_after = history_after.json()["items"][0]
    assert item_after["message_id"] == message_id
    assert item_after["request_text"] == "new text"


def test_chat_history_routes_hide_foreign_resources(client) -> None:
    suffix = uuid4().hex[:8]
    team_id, user_id, conversation_id = _create_team_user_and_conversation(client, suffix)

    ask_response = client.post(
        "/api/v1/chat/ask",
        json={
            "conversation_id": conversation_id,
            "question": "owner only",
        },
    )
    assert ask_response.status_code == 200

    history_response = client.get(
        "/api/v1/chat/history",
        params={"conversation_id": conversation_id, "limit": 20},
    )
    assert history_response.status_code == 200
    message_id = history_response.json()["items"][0]["message_id"]

    logout_response = client.post("/api/v1/auth/logout")
    assert logout_response.status_code == 204

    intruder_team_id, intruder_user_id = register_workspace_user(
        client,
        prefix="history_intruder",
        display_name="Intruder",
    )
    assert intruder_team_id != team_id
    assert intruder_user_id != user_id

    foreign_history = client.get(
        "/api/v1/chat/history",
        params={"conversation_id": conversation_id, "limit": 20},
    )
    assert foreign_history.status_code == 404
    assert foreign_history.json()["detail"] == f"Conversation '{conversation_id}' not found."

    foreign_delete = client.delete(
        f"/api/v1/chat/history/{message_id}",
        params={"conversation_id": conversation_id},
    )
    assert foreign_delete.status_code == 404
    assert foreign_delete.json()["detail"] == f"Message '{message_id}' not found."

    foreign_edit = client.put(
        f"/api/v1/chat/history/{message_id}",
        json={"request_text": "stolen edit"},
    )
    assert foreign_edit.status_code == 404
    assert foreign_edit.json()["detail"] == f"Message '{message_id}' not found."


def test_chat_history_edit_requires_latest_message(client) -> None:
    suffix = uuid4().hex[:8]
    team_id, user_id, conversation_id = _create_team_user_and_conversation(client, suffix)

    first_ask = client.post(
        "/api/v1/chat/ask",
        json={
            "user_id": user_id,
            "team_id": team_id,
            "conversation_id": conversation_id,
            "question": "first question",
        },
    )
    assert first_ask.status_code == 200

    second_ask = client.post(
        "/api/v1/chat/ask",
        json={
            "user_id": user_id,
            "team_id": team_id,
            "conversation_id": conversation_id,
            "question": "second question",
        },
    )
    assert second_ask.status_code == 200

    history_response = client.get(
        "/api/v1/chat/history",
        params={
            "team_id": team_id,
            "user_id": user_id,
            "conversation_id": conversation_id,
            "limit": 20,
        },
    )
    assert history_response.status_code == 200
    items = history_response.json()["items"]
    assert len(items) == 2

    latest_item = items[0]
    older_item = items[1]

    stale_edit = client.put(
        f"/api/v1/chat/history/{older_item['message_id']}",
        json={"request_text": "updated first question"},
    )
    assert stale_edit.status_code == 400
    assert stale_edit.json()["detail"] == "Only the latest message in a conversation can be edited or regenerated."

    latest_edit = client.put(
        f"/api/v1/chat/history/{latest_item['message_id']}",
        json={"request_text": "updated second question"},
    )
    assert latest_edit.status_code == 200
    assert latest_edit.json()["request_text"] == "updated second question"

def test_chat_ask_respects_use_document_scope_false_with_ready_conversation_files(client) -> None:
    suffix = uuid4().hex[:8]
    team_id, user_id, conversation_id = _create_team_user_and_conversation(client, suffix)

    import_response = client.post(
        "/api/v1/documents/import",
        json={
            "team_id": team_id,
            "conversation_id": conversation_id,
            "source_name": "brief.md",
            "content_type": "md",
            "content": "# Launch Brief\n\nRollback approval is required before any production rollback.",
        },
    )
    assert import_response.status_code == 201
    document_id = import_response.json()["document_id"]

    chunk_response = client.post(
        f"/api/v1/documents/{document_id}/chunk",
        json={
            "team_id": team_id,
            "conversation_id": conversation_id,
            "max_chars": 100,
            "overlap": 10,
        },
    )
    assert chunk_response.status_code == 200

    index_response = client.post(
        "/api/v1/retrieval/index",
        json={
            "team_id": team_id,
            "conversation_id": conversation_id,
            "document_id": document_id,
        },
    )
    assert index_response.status_code == 200

    ask_response = client.post(
        "/api/v1/chat/ask",
        json={
            "user_id": user_id,
            "team_id": team_id,
            "conversation_id": conversation_id,
            "question": "What does the brief say about rollback?",
            "selected_document_ids": [document_id],
            "use_document_scope": False,
            "include_memory": False,
            "include_library": False,
            "top_k": 3,
        },
    )
    assert ask_response.status_code == 200
    body = ask_response.json()
    assert body["mode"] == "chat"
    assert body["hits"] == []
    assert body["sources"] == []


def test_chat_history_edit_preserves_use_document_scope_false(client) -> None:
    suffix = uuid4().hex[:8]
    team_id, user_id, conversation_id = _create_team_user_and_conversation(client, suffix)

    import_response = client.post(
        "/api/v1/documents/import",
        json={
            "team_id": team_id,
            "conversation_id": conversation_id,
            "source_name": "brief.md",
            "content_type": "md",
            "content": "# Launch Brief\n\nRollback approval is required before any production rollback.",
        },
    )
    assert import_response.status_code == 201
    document_id = import_response.json()["document_id"]

    chunk_response = client.post(
        f"/api/v1/documents/{document_id}/chunk",
        json={
            "team_id": team_id,
            "conversation_id": conversation_id,
            "max_chars": 100,
            "overlap": 10,
        },
    )
    assert chunk_response.status_code == 200

    index_response = client.post(
        "/api/v1/retrieval/index",
        json={
            "team_id": team_id,
            "conversation_id": conversation_id,
            "document_id": document_id,
        },
    )
    assert index_response.status_code == 200

    ask_response = client.post(
        "/api/v1/chat/ask",
        json={
            "user_id": user_id,
            "team_id": team_id,
            "conversation_id": conversation_id,
            "question": "What does the brief say about rollback?",
            "selected_document_ids": [document_id],
            "use_document_scope": False,
            "include_memory": False,
            "include_library": False,
            "top_k": 3,
        },
    )
    assert ask_response.status_code == 200
    assert ask_response.json()["mode"] == "chat"

    history_response = client.get(
        "/api/v1/chat/history",
        params={
            "team_id": team_id,
            "user_id": user_id,
            "conversation_id": conversation_id,
            "limit": 20,
        },
    )
    assert history_response.status_code == 200
    message_id = history_response.json()["items"][0]["message_id"]

    edit_response = client.put(
        f"/api/v1/chat/history/{message_id}",
        json={
            "team_id": team_id,
            "user_id": user_id,
            "request_text": "Rewrite the rollback answer.",
        },
    )
    assert edit_response.status_code == 200
    edited = edit_response.json()
    assert edited["request_payload"]["use_document_scope"] is False
    assert edited["request_payload"]["include_memory"] is False
    assert edited["request_payload"]["include_library"] is False
    assert edited["response_payload"]["mode"] == "chat"

def test_chat_ask_defaults_to_chat_mode_without_use_document_scope_flag_when_document_enhancement_is_disabled(client) -> None:
    suffix = uuid4().hex[:8]
    team_id, user_id, conversation_id = _create_team_user_and_conversation(client, suffix)

    import_response = client.post(
        "/api/v1/documents/import",
        json={
            "team_id": team_id,
            "conversation_id": conversation_id,
            "source_name": "brief.md",
            "content_type": "md",
            "content": "# Launch Brief\n\nRollback approval is required before any production rollback.",
        },
    )
    assert import_response.status_code == 201
    document_id = import_response.json()["document_id"]

    chunk_response = client.post(
        f"/api/v1/documents/{document_id}/chunk",
        json={
            "team_id": team_id,
            "conversation_id": conversation_id,
            "max_chars": 100,
            "overlap": 10,
        },
    )
    assert chunk_response.status_code == 200

    index_response = client.post(
        "/api/v1/retrieval/index",
        json={
            "team_id": team_id,
            "conversation_id": conversation_id,
            "document_id": document_id,
        },
    )
    assert index_response.status_code == 200

    ask_response = client.post(
        "/api/v1/chat/ask",
        json={
            "user_id": user_id,
            "team_id": team_id,
            "conversation_id": conversation_id,
            "question": "What does the brief say about rollback?",
            "include_memory": False,
            "include_library": False,
            "include_conclusions": False,
            "top_k": 3,
        },
    )
    assert ask_response.status_code == 200
    body = ask_response.json()
    assert body["mode"] == "chat"
    assert body["hits"] == []
    assert body["sources"] == []
