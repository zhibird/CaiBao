from __future__ import annotations

import math

import httpx

from app.core.config import Settings
from app.core.exceptions import DomainValidationError
from app.services.embedding_service import EmbeddingService


class _FakeResponse:
    def __init__(self, body: dict[str, object]) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        return

    def json(self) -> dict[str, object]:
        return self._body


def test_embedding_service_mock_vectors() -> None:
    settings = Settings(embedding_provider="mock", embedding_mock_dim=32)
    service = EmbeddingService(settings=settings)

    vectors = service.embed_texts(["hello world", "hello retrieval"])
    assert len(vectors) == 2
    assert len(vectors[0]) == 32
    assert len(vectors[1]) == 32
    assert service.model_name == "hashing_v1"


def test_embedding_service_real_provider_calls_api(monkeypatch) -> None:
    captured_payload: dict[str, object] = {}

    def _fake_post(url, headers, json, timeout):  # noqa: ANN001
        captured_payload["url"] = url
        captured_payload["headers"] = headers
        captured_payload["json"] = json
        captured_payload["timeout"] = timeout
        return _FakeResponse(
            {
                "data": [
                    {"index": 0, "embedding": [0.6, 0.8, 0.0]},
                    {"index": 1, "embedding": [0.0, 0.8, 0.6]},
                ]
            }
        )

    monkeypatch.setattr("app.services.embedding_service.httpx.post", _fake_post)

    settings = Settings(
        embedding_provider="openai",
        embedding_base_url="https://api.openai.com/v1",
        embedding_api_key="sk-test",
        embedding_model="text-embedding-3-small",
        embedding_timeout_seconds=5.0,
    )
    service = EmbeddingService(settings=settings)

    vectors = service.embed_texts(["alpha", "beta"])
    assert vectors == [[0.6, 0.8, 0.0], [0.0, 0.8, 0.6]]
    assert captured_payload["url"] == "https://api.openai.com/v1/embeddings"
    assert captured_payload["json"] == {
        "model": "text-embedding-3-small",
        "input": ["alpha", "beta"],
    }
    assert service.model_name == "text-embedding-3-small"
    assert service.dim == 3


def test_embedding_service_normalizes_real_provider_vectors(monkeypatch) -> None:
    def _fake_post(url, headers, json, timeout):  # noqa: ANN001
        return _FakeResponse(
            {
                "data": [
                    {"index": 0, "embedding": [3.0, 4.0]},
                ]
            }
        )

    monkeypatch.setattr("app.services.embedding_service.httpx.post", _fake_post)

    settings = Settings(
        embedding_provider="openai",
        embedding_base_url="https://api.openai.com/v1",
        embedding_api_key="sk-test",
        embedding_model="text-embedding-3-small",
    )
    service = EmbeddingService(settings=settings)

    vectors = service.embed_texts(["needs normalization"])
    assert vectors == [[0.6, 0.8]]
    # Dot product on unit vectors equals cosine similarity.
    assert service.cosine_similarity(vectors[0], vectors[0]) == 1.0


def test_embedding_service_mock_vectors_are_unit_normalized() -> None:
    settings = Settings(embedding_provider="mock", embedding_mock_dim=32)
    service = EmbeddingService(settings=settings)

    vector = service.embed_text("hello world")
    norm = math.sqrt(sum(value * value for value in vector))
    assert abs(norm - 1.0) < 1e-9
    assert abs(service.cosine_similarity(vector, vector) - 1.0) < 1e-9


def test_embedding_service_real_provider_requires_api_key() -> None:
    settings = Settings(
        embedding_provider="openai",
        embedding_base_url="https://api.openai.com/v1",
        embedding_api_key=None,
    )
    service = EmbeddingService(settings=settings)

    try:
        service.embed_texts(["alpha"])
    except DomainValidationError as exc:
        assert "EMBEDDING_API_KEY" in str(exc)
    else:
        raise AssertionError("Expected DomainValidationError for missing EMBEDDING_API_KEY.")


def test_embedding_service_default_uses_env_runtime_when_credentials_exist(monkeypatch) -> None:
    captured_payload: dict[str, object] = {}

    def _fake_post(url, headers, json, timeout):  # noqa: ANN001
        captured_payload["url"] = url
        captured_payload["headers"] = headers
        captured_payload["json"] = json
        captured_payload["timeout"] = timeout
        return _FakeResponse(
            {
                "data": [
                    {"index": 0, "embedding": [0.6, 0.8]},
                ]
            }
        )

    monkeypatch.setattr("app.services.embedding_service.httpx.post", _fake_post)

    settings = Settings(
        embedding_provider="mock",
        embedding_base_url="https://api.openai.com/v1",
        embedding_api_key="sk-test",
        embedding_model="text-embedding-3-small",
    )
    service = EmbeddingService(settings=settings)

    vectors = service.embed_texts(["compat path"])
    assert vectors == [[0.6, 0.8]]
    assert captured_payload["url"] == "https://api.openai.com/v1/embeddings"
    assert captured_payload["json"] == {
        "model": "text-embedding-3-small",
        "input": ["compat path"],
    }
    assert service.model_name == "text-embedding-3-small"


def test_embedding_service_timeout_splits_batches(monkeypatch) -> None:
    calls: list[int] = []

    def _fake_post(url, headers, json, timeout):  # noqa: ANN001
        size = len(json["input"])
        calls.append(size)
        if size > 1:
            raise httpx.ReadTimeout("timed out", request=httpx.Request("POST", url))
        return _FakeResponse({"data": [{"index": 0, "embedding": [0.8, 0.6]}]})

    monkeypatch.setattr("app.services.embedding_service.httpx.post", _fake_post)

    settings = Settings(
        embedding_provider="openai",
        embedding_base_url="https://api.openai.com/v1",
        embedding_api_key="sk-test",
        embedding_model="text-embedding-3-small",
        embedding_batch_size=4,
    )
    service = EmbeddingService(settings=settings)

    vectors = service.embed_texts(["a", "b", "c", "d"])
    assert vectors == [[0.8, 0.6], [0.8, 0.6], [0.8, 0.6], [0.8, 0.6]]
    assert calls[0] == 4
    assert any(size == 1 for size in calls)
