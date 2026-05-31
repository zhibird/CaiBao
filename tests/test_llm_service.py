from __future__ import annotations

import base64
import httpx

from app.core.config import Settings
from app.core.exceptions import DomainValidationError
from app.services.llm_service import LLMService, VisionAttachment
from app.services.persona_prompt import DEFAULT_AGENT_SYSTEM_PROMPT


class _FakeResponse:
    def __init__(self, body: dict[str, object], status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code
        self.text = str(body)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
            response = httpx.Response(self.status_code, request=request, json=self._body)
            raise httpx.HTTPStatusError("request failed", request=request, response=response)
        return

    def json(self) -> dict[str, object]:
        return self._body


def test_llm_service_default_uses_env_runtime_when_credentials_exist(monkeypatch) -> None:
    captured_payload: dict[str, object] = {}

    def _fake_post(url, headers, json, timeout):  # noqa: ANN001
        captured_payload["url"] = url
        captured_payload["headers"] = headers
        captured_payload["json"] = json
        captured_payload["timeout"] = timeout
        return _FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": "real-chat-answer",
                        }
                    }
                ]
            }
        )

    monkeypatch.setattr("app.services.llm_service.httpx.post", _fake_post)

    settings = Settings(
        llm_provider="mock",
        llm_base_url="https://api.openai.com/v1",
        llm_api_key="sk-test",
        llm_model="gpt-4.1-mini",
        llm_enable_thinking=False,
    )
    service = LLMService(settings=settings)

    answer = service.answer_chat("hello")
    assert answer.answer == "real-chat-answer"
    assert len(answer.content_parts) == 1
    assert answer.content_parts[0].type == "text"
    assert answer.content_parts[0].text == "real-chat-answer"
    assert captured_payload["url"] == "https://api.openai.com/v1/chat/completions"
    assert captured_payload["json"] == {
        "model": "gpt-4.1-mini",
        "messages": [
            {"role": "system", "content": DEFAULT_AGENT_SYSTEM_PROMPT},
            {"role": "user", "content": "hello"},
        ],
        "temperature": settings.llm_temperature,
        "max_tokens": settings.llm_max_tokens,
    }


def test_llm_service_includes_conversation_history_before_current_user(monkeypatch) -> None:
    captured_payload: dict[str, object] = {}

    def _fake_post(url, headers, json, timeout):  # noqa: ANN001
        captured_payload["url"] = url
        captured_payload["headers"] = headers
        captured_payload["json"] = json
        captured_payload["timeout"] = timeout
        return _FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": "memory-aware-answer",
                        }
                    }
                ]
            }
        )

    monkeypatch.setattr("app.services.llm_service.httpx.post", _fake_post)

    settings = Settings(
        llm_provider="mock",
        llm_base_url="https://api.openai.com/v1",
        llm_api_key="sk-test",
        llm_model="gpt-4.1-mini",
    )
    service = LLMService(settings=settings)

    answer = service.answer_chat(
        "What project are we discussing?",
        conversation_messages=[
            {"role": "user", "content": "Remember that the project codename is Apollo."},
            {"role": "assistant", "content": "Noted. The project codename is Apollo."},
        ],
    )

    assert answer.answer == "memory-aware-answer"
    assert captured_payload["json"]["messages"] == [  # type: ignore[index]
        {"role": "system", "content": DEFAULT_AGENT_SYSTEM_PROMPT},
        {"role": "user", "content": "Remember that the project codename is Apollo."},
        {"role": "assistant", "content": "Noted. The project codename is Apollo."},
        {"role": "user", "content": "What project are we discussing?"},
    ]


def test_llm_service_auto_falls_back_to_compat_history_mode(monkeypatch) -> None:
    captured_payloads: list[dict[str, object]] = []
    call_count = {"count": 0}

    def _fake_post(url, headers, json, timeout):  # noqa: ANN001
        captured_payloads.append(json)
        call_count["count"] += 1
        if call_count["count"] == 1:
            return _FakeResponse(
                {"error": {"message": "openai_error"}},
                status_code=429,
            )
        return _FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": "compat-answer",
                        }
                    }
                ]
            }
        )

    monkeypatch.setattr("app.services.llm_service.httpx.post", _fake_post)

    settings = Settings(
        llm_provider="mock",
        llm_base_url="https://api.openai.com/v1",
        llm_api_key="sk-test",
        llm_model="gpt-4.1-mini",
        llm_history_mode="auto",
    )
    service = LLMService(settings=settings)

    answer = service.answer_chat(
        "What project are we discussing?",
        conversation_messages=[
            {"role": "user", "content": "Remember that the project codename is Apollo."},
            {"role": "assistant", "content": "Noted. The project codename is Apollo."},
        ],
    )

    assert answer.answer == "compat-answer"
    assert len(captured_payloads) == 2
    assert captured_payloads[0]["messages"] == [  # type: ignore[index]
        {"role": "system", "content": DEFAULT_AGENT_SYSTEM_PROMPT},
        {"role": "user", "content": "Remember that the project codename is Apollo."},
        {"role": "assistant", "content": "Noted. The project codename is Apollo."},
        {"role": "user", "content": "What project are we discussing?"},
    ]
    assert captured_payloads[1]["messages"] == [  # type: ignore[index]
        {"role": "system", "content": DEFAULT_AGENT_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Conversation history:\n"
                "User: Remember that the project codename is Apollo.\n"
                "Assistant: Noted. The project codename is Apollo.\n\n"
                "Current user request:\n"
                "What project are we discussing?"
            ),
        },
    ]


def test_llm_service_uses_longer_timeout_for_image_generation_prompt(monkeypatch) -> None:
    captured_payload: dict[str, object] = {}

    def _fake_post(url, headers, json, timeout):  # noqa: ANN001
        captured_payload["url"] = url
        captured_payload["timeout"] = timeout
        return _FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": "image-answer",
                        }
                    }
                ]
            }
        )

    monkeypatch.setattr("app.services.llm_service.httpx.post", _fake_post)

    settings = Settings(
        llm_provider="mock",
        llm_base_url="https://api.openai.com/v1",
        llm_api_key="sk-test",
        llm_model="gpt-4.1-mini",
        llm_timeout_seconds=15,
    )
    service = LLMService(settings=settings)

    answer = service.answer_chat("请生成图片：一只在办公室里的猫")

    assert answer.answer == "image-answer"
    assert captured_payload["url"] == "https://api.openai.com/v1/chat/completions"
    assert captured_payload["timeout"] == 90.0


def test_llm_service_sends_multimodal_payload_when_images_are_provided(monkeypatch) -> None:
    captured_payload: dict[str, object] = {}

    def _fake_post(url, headers, json, timeout):  # noqa: ANN001
        captured_payload["url"] = url
        captured_payload["headers"] = headers
        captured_payload["json"] = json
        captured_payload["timeout"] = timeout
        return _FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": "vision-answer",
                        }
                    }
                ]
            }
        )

    monkeypatch.setattr("app.services.llm_service.httpx.post", _fake_post)

    settings = Settings(
        llm_provider="mock",
        llm_base_url="https://api.openai.com/v1",
        llm_api_key="sk-test",
        llm_model="gpt-4.1-mini",
    )
    service = LLMService(settings=settings)

    answer = service.answer_chat(
        "Describe the attached image.",
        image_attachments=[
            VisionAttachment(
                document_id="doc-1",
                source_name="receipt.png",
                mime_type="image/png",
                data_url="data:image/png;base64,AAAA",
            )
        ],
    )
    assert answer.answer == "vision-answer"

    user_message = captured_payload["json"]["messages"][1]  # type: ignore[index]
    assert user_message["role"] == "user"
    assert isinstance(user_message["content"], list)
    assert user_message["content"][0] == {"type": "text", "text": "Describe the attached image."}
    assert user_message["content"][1] == {
        "type": "image_url",
        "image_url": {
            "url": "data:image/png;base64,AAAA",
            "detail": "auto",
        },
    }


def test_llm_service_routes_images_to_vl_profile_when_main_is_text_only(monkeypatch) -> None:
    captured_payload: dict[str, object] = {}

    def _fake_post(url, headers, json, timeout):  # noqa: ANN001
        captured_payload["url"] = url
        captured_payload["headers"] = headers
        captured_payload["json"] = json
        return _FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": "vl-answer",
                        }
                    }
                ]
            }
        )

    monkeypatch.setattr("app.services.llm_service.httpx.post", _fake_post)

    settings = Settings(
        llm_provider="deepseek",
        llm_base_url="https://api.deepseek.com/v1",
        llm_api_key="sk-main",
        llm_model="deepseek-v4-flash",
        llm_multimodal=False,
        llm_vl_model="qwen-vl-plus",
        llm_vl_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        llm_vl_api_key="sk-vl",
    )
    service = LLMService(settings=settings)

    answer = service.answer_chat(
        "Describe the attached image.",
        image_attachments=[
            VisionAttachment(
                document_id="doc-1",
                source_name="receipt.png",
                mime_type="image/png",
                data_url="data:image/png;base64,AAAA",
            )
        ],
    )

    assert answer.answer == "vl-answer"
    assert captured_payload["url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    assert captured_payload["headers"]["Authorization"] == "Bearer sk-vl"
    assert captured_payload["json"]["model"] == "qwen-vl-plus"


def test_llm_service_falls_back_to_text_when_vision_is_rejected(monkeypatch) -> None:
    captured_payloads: list[dict[str, object]] = []
    call_count = {"count": 0}

    def _fake_post(url, headers, json, timeout):  # noqa: ANN001
        captured_payloads.append(json)
        call_count["count"] += 1
        if call_count["count"] == 1:
            return _FakeResponse(
                {"error": {"message": "This model does not support image inputs."}},
                status_code=400,
            )
        return _FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": "fallback-answer",
                        }
                    }
                ]
            }
        )

    monkeypatch.setattr("app.services.llm_service.httpx.post", _fake_post)

    settings = Settings(
        llm_provider="mock",
        llm_base_url="https://api.openai.com/v1",
        llm_api_key="sk-test",
        llm_model="gpt-4.1-mini",
    )
    service = LLMService(settings=settings)

    answer = service.answer_chat(
        "Read the attached image.",
        image_attachments=[
            VisionAttachment(
                document_id="doc-2",
                source_name="invoice.png",
                mime_type="image/png",
                data_url="data:image/png;base64,BBBB",
            )
        ],
        fallback_text_context="[invoice.png]\nOCR result",
    )
    assert answer.answer == "fallback-answer"
    assert len(captured_payloads) == 2
    assert isinstance(captured_payloads[0]["messages"][1]["content"], list)  # type: ignore[index]
    assert captured_payloads[1]["messages"][1]["content"] == (  # type: ignore[index]
        "Read the attached image.\n\nAttachment text fallback:\n[invoice.png]\nOCR result"
    )


def test_llm_service_sends_enable_thinking_for_main_model(monkeypatch) -> None:
    captured_payload: dict[str, object] = {}

    def _fake_post(url, headers, json, timeout):  # noqa: ANN001
        captured_payload["json"] = json
        return _FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": "thinking-answer",
                        }
                    }
                ]
            }
        )

    monkeypatch.setattr("app.services.llm_service.httpx.post", _fake_post)

    settings = Settings(
        llm_provider="deepseek",
        llm_base_url="https://api.deepseek.com/v1",
        llm_api_key="sk-main",
        llm_model="deepseek-v4-flash",
        llm_enable_thinking=True,
    )
    service = LLMService(settings=settings)

    result = service.complete_chat(messages=[{"role": "user", "content": "hello"}])

    assert result.assistant_text == "thinking-answer"
    assert captured_payload["json"]["enable_thinking"] is True


def test_llm_service_continues_when_model_hits_token_limit(monkeypatch) -> None:
    captured_payloads: list[dict[str, object]] = []
    responses = iter(
        [
            _FakeResponse(
                {
                    "choices": [
                        {
                            "message": {"content": "Part one. "},
                            "finish_reason": "length",
                        }
                    ]
                }
            ),
            _FakeResponse(
                {
                    "choices": [
                        {
                            "message": {"content": "Part two."},
                            "finish_reason": "stop",
                        }
                    ]
                }
            ),
        ]
    )

    def _fake_post(url, headers, json, timeout):  # noqa: ANN001
        captured_payloads.append(json)
        return next(responses)

    monkeypatch.setattr("app.services.llm_service.httpx.post", _fake_post)

    settings = Settings(
        llm_provider="mock",
        llm_base_url="https://api.openai.com/v1",
        llm_api_key="sk-test",
        llm_model="gpt-4.1-mini",
        llm_max_tokens=512,
    )
    service = LLMService(settings=settings)

    answer = service.answer_chat("Give me a long answer.")

    assert answer.answer == "Part one. Part two."
    assert len(captured_payloads) == 2
    assert captured_payloads[1]["messages"][-2:] == [  # type: ignore[index]
        {"role": "assistant", "content": "Part one. "},
        {
            "role": "user",
            "content": (
                "Continue exactly from where you stopped. "
                "Do not repeat prior text. Finish the current answer."
            ),
        },
    ]


def test_llm_service_stops_image_only_continuation_without_placeholder_pollution(monkeypatch) -> None:
    captured_payloads: list[dict[str, object]] = []

    def _fake_post(url, headers, json, timeout):  # noqa: ANN001
        captured_payloads.append(json)
        return _FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": [
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": "data:image/png;base64,AAAA",
                                    },
                                }
                            ],
                        },
                        "finish_reason": "length",
                    }
                ]
            }
        )

    monkeypatch.setattr("app.services.llm_service.httpx.post", _fake_post)

    settings = Settings(
        llm_provider="mock",
        llm_base_url="https://api.openai.com/v1",
        llm_api_key="sk-test",
        llm_model="gpt-4.1-mini",
        llm_max_tokens=512,
    )
    service = LLMService(settings=settings)

    answer = service.answer_chat("Generate an image.")

    assert answer.answer == "Image output"
    assert len(answer.content_parts) == 1
    assert answer.content_parts[0].type == "image"
    assert len(captured_payloads) == 1


def test_llm_service_preserves_image_output_parts(monkeypatch) -> None:
    def _fake_post(url, headers, json, timeout):  # noqa: ANN001, ARG001
        return _FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": [
                                {"type": "text", "text": "Here is the generated chart."},
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": "data:image/png;base64,AAAA",
                                    },
                                },
                            ],
                        },
                        "finish_reason": "stop",
                    }
                ]
            }
        )

    monkeypatch.setattr("app.services.llm_service.httpx.post", _fake_post)

    settings = Settings(
        llm_provider="mock",
        llm_base_url="https://api.openai.com/v1",
        llm_api_key="sk-test",
        llm_model="gpt-4.1-mini",
    )
    service = LLMService(settings=settings)

    answer = service.answer_chat("Generate a chart image.")

    assert answer.answer == "Here is the generated chart."
    assert len(answer.content_parts) == 2
    assert answer.content_parts[0].type == "text"
    assert answer.content_parts[0].text == "Here is the generated chart."
    assert answer.content_parts[1].type == "image"
    assert answer.content_parts[1].url == "data:image/png;base64,AAAA"
    assert answer.content_parts[1].original_url is None


def test_llm_service_extracts_markdown_data_image_from_text_response(monkeypatch) -> None:
    def _fake_post(url, headers, json, timeout):  # noqa: ANN001, ARG001
        return _FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": "![Generated Image](data:image/jpeg;base64,/9j/AAAA)",
                        },
                        "finish_reason": "stop",
                    }
                ]
            }
        )

    monkeypatch.setattr("app.services.llm_service.httpx.post", _fake_post)

    settings = Settings(
        llm_provider="mock",
        llm_base_url="https://api.openai.com/v1",
        llm_api_key="sk-test",
        llm_model="gpt-4.1-mini",
    )
    service = LLMService(settings=settings)

    answer = service.answer_chat("Generate a chart image.")

    assert answer.answer == "Image output"
    assert len(answer.content_parts) == 1
    assert answer.content_parts[0].type == "image"
    assert answer.content_parts[0].mime_type == "image/jpeg"
    assert answer.content_parts[0].url == "data:image/jpeg;base64,/9j/AAAA"
    assert answer.content_parts[0].original_url is None
    assert answer.content_parts[0].alt == "Generated Image"


def test_llm_service_inlines_remote_image_output_urls(monkeypatch) -> None:
    image_bytes = b"\x89PNG\r\n\x1a\nPNGDATA"
    expected_data_url = f"data:image/png;base64,{base64.b64encode(image_bytes).decode('ascii')}"

    def _fake_post(url, headers, json, timeout):  # noqa: ANN001, ARG001
        return _FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": [
                                {"type": "text", "text": "Here is the generated chart."},
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": "https://cdn.example.com/generated/chart.png?signature=test",
                                    },
                                },
                            ],
                        },
                        "finish_reason": "stop",
                    }
                ]
            }
        )

    def _fake_get(url, follow_redirects, timeout):  # noqa: ANN001, ARG001
        assert url == "https://cdn.example.com/generated/chart.png?signature=test"
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

    settings = Settings(
        llm_provider="mock",
        llm_base_url="https://api.openai.com/v1",
        llm_api_key="sk-test",
        llm_model="gpt-4.1-mini",
    )
    service = LLMService(settings=settings)

    answer = service.answer_chat("Generate a chart image.")

    assert answer.answer == "Here is the generated chart."
    assert len(answer.content_parts) == 2
    assert answer.content_parts[1].type == "image"
    assert answer.content_parts[1].url == expected_data_url
    assert answer.content_parts[1].original_url == "https://cdn.example.com/generated/chart.png?signature=test"
    assert answer.content_parts[1].mime_type == "image/png"


def test_llm_service_summarizes_html_error_pages(monkeypatch) -> None:
    class _HtmlErrorResponse:
        status_code = 502
        text = "<!DOCTYPE html><html><head><title>502 Bad Gateway</title></head><body>gateway</body></html>"

        def raise_for_status(self) -> None:
            request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
            response = httpx.Response(
                502,
                request=request,
                text=self.text,
                headers={"content-type": "text/html"},
            )
            raise httpx.HTTPStatusError("request failed", request=request, response=response)

        def json(self) -> dict[str, object]:
            raise ValueError("not json")

    def _fake_post(url, headers, json, timeout):  # noqa: ANN001, ARG001
        return _HtmlErrorResponse()

    monkeypatch.setattr("app.services.llm_service.httpx.post", _fake_post)

    settings = Settings(
        llm_provider="mock",
        llm_base_url="https://api.openai.com/v1",
        llm_api_key="sk-test",
        llm_model="gpt-4.1-mini",
    )
    service = LLMService(settings=settings)

    try:
        service.answer_chat("hello")
        raise AssertionError("expected DomainValidationError")
    except DomainValidationError as exc:
        message = str(exc)
        assert "Upstream provider returned an HTML error page" in message
        assert "502 Bad Gateway" in message
        assert "<html" not in message.lower()
