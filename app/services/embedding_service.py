from __future__ import annotations

from collections import deque
import hashlib
import math
import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from app.core.config import Settings, get_settings
from app.core.exceptions import DomainValidationError


@dataclass(slots=True)
class EmbeddingRuntimeConfig:
    provider: str
    model_name: str
    base_url: str | None = None
    api_key: str | None = None

    @classmethod
    def mock_default(cls, model_name: str = "hashing_v1") -> "EmbeddingRuntimeConfig":
        return cls(
            provider="mock",
            model_name=model_name,
            base_url=None,
            api_key=None,
        )


class EmbeddingService:
    """Embedding wrapper with mock + real provider modes."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.provider = self.settings.embedding_provider.strip().lower()
        self.batch_size = max(1, self.settings.embedding_batch_size)

        if self.provider == "mock":
            self.dim = max(8, self.settings.embedding_mock_dim)
            self.model_name = "hashing_v1"
        else:
            self.dim = 0
            self.model_name = self.settings.embedding_model

    def embed_text(self, text: str, runtime: EmbeddingRuntimeConfig | None = None) -> list[float]:
        vectors = self.embed_texts([text], runtime=runtime)
        return vectors[0]

    def embed_texts(
        self,
        texts: list[str],
        runtime: EmbeddingRuntimeConfig | None = None,
    ) -> list[list[float]]:
        normalized_texts = [str(text) for text in texts]
        if not normalized_texts:
            return []

        runtime_provider, runtime_model_name, runtime_base_url, runtime_api_key = self._resolve_runtime(runtime)
        self.provider = runtime_provider

        if runtime_provider == "mock":
            self.dim = max(8, self.settings.embedding_mock_dim)
            self.model_name = runtime_model_name or "hashing_v1"
            vectors = [self._mock_embed_text(text) for text in normalized_texts]
            self.dim = len(vectors[0]) if vectors else self.dim
            return vectors

        base_url = self._normalize_base_url(runtime_base_url or "")
        api_key = (runtime_api_key or "").strip()
        if not api_key:
            raise DomainValidationError(
                "EMBEDDING_API_KEY is required when embedding_provider is not 'mock'."
            )
        url = f"{base_url}/embeddings"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        request_batch_size = self._resolve_request_batch_size(base_url=base_url)
        vectors = self._embed_with_adaptive_retry(
            texts=normalized_texts,
            model_name=runtime_model_name,
            url=url,
            headers=headers,
            request_batch_size=request_batch_size,
        )
        # Normalize once at the source so both index and query sides store/compare
        # unit vectors, making dot product equivalent to cosine similarity.
        vectors = [self._l2_normalize(vector) for vector in vectors]

        if vectors:
            self.dim = len(vectors[0])
        self.model_name = runtime_model_name
        return vectors

    def cosine_similarity(self, a: list[float], b: list[float]) -> float:
        # Vectors produced by this service are L2-normalized, so the plain dot
        # product below is equivalent to cosine similarity.
        if not a or not b or len(a) != len(b):
            return 0.0

        return float(sum(x * y for x, y in zip(a, b)))

    @staticmethod
    def _l2_normalize(vector: list[float]) -> list[float]:
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            return vector
        return [value / norm for value in vector]

    def _mock_embed_text(self, text: str) -> list[float]:
        tokens = self._tokenize(text)
        if not tokens:
            return [0.0] * self.dim

        vector = [0.0] * self.dim
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "little") % self.dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign

        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            return vector

        return [value / norm for value in vector]

    def _parse_embeddings_response(
        self,
        body: dict[str, object],
        *,
        expected_size: int,
    ) -> list[list[float]]:
        data = body.get("data")
        if not isinstance(data, list):
            raise DomainValidationError("Embedding response format is invalid: missing data.")

        parsed_rows: list[tuple[int, list[float]]] = []
        for row in data:
            if not isinstance(row, dict):
                raise DomainValidationError("Embedding response format is invalid: row is not object.")

            index = row.get("index")
            embedding = row.get("embedding")
            if not isinstance(index, int) or not isinstance(embedding, list):
                raise DomainValidationError("Embedding response format is invalid: missing index/embedding.")

            vector: list[float] = []
            for value in embedding:
                if not isinstance(value, (int, float)):
                    raise DomainValidationError("Embedding response format is invalid: non-numeric vector value.")
                vector.append(float(value))

            parsed_rows.append((index, vector))

        if len(parsed_rows) != expected_size:
            raise DomainValidationError(
                f"Embedding response count mismatch: expected {expected_size}, got {len(parsed_rows)}."
            )

        parsed_rows.sort(key=lambda item: item[0])
        ordered = [item[1] for item in parsed_rows]

        first_dim = len(ordered[0]) if ordered else 0
        if first_dim <= 0:
            raise DomainValidationError("Embedding response contains empty vectors.")
        if any(len(vector) != first_dim for vector in ordered):
            raise DomainValidationError("Embedding response vectors have inconsistent dimensions.")

        return ordered

    def _tokenize(self, text: str) -> list[str]:
        return re.findall(r"\w+", text.lower())

    def _resolve_runtime(
        self,
        runtime: EmbeddingRuntimeConfig | None,
    ) -> tuple[str, str, str | None, str | None]:
        if runtime is None:
            provider = self.settings.embedding_provider.strip().lower()
            if provider == "mock":
                settings_base_url = self.settings.embedding_base_url.strip()
                settings_key = (self.settings.embedding_api_key or "").strip()
                # Compat: if user filled .env base_url + api_key but left provider as mock,
                # use real OpenAI-compatible embedding runtime for default mode.
                if settings_base_url and settings_key:
                    return (
                        "openai",
                        self.settings.embedding_model,
                        settings_base_url,
                        settings_key,
                    )
                return "mock", "hashing_v1", None, None
            return (
                provider,
                self.settings.embedding_model,
                self.settings.embedding_base_url,
                self.settings.embedding_api_key,
            )

        provider = runtime.provider.strip().lower()
        model_name = runtime.model_name.strip()
        if not provider:
            raise DomainValidationError("embedding runtime provider cannot be empty.")
        if provider == "mock":
            return "mock", model_name or "hashing_v1", None, None
        if not model_name:
            raise DomainValidationError("embedding runtime model_name cannot be empty.")
        return provider, model_name, runtime.base_url, runtime.api_key

    def _resolve_request_batch_size(self, *, base_url: str) -> int:
        configured = max(1, self.batch_size)
        normalized_base_url = base_url.lower()
        # DashScope-compatible embeddings currently limit input batch size to 10.
        if "dashscope.aliyuncs.com" in normalized_base_url:
            return min(configured, 10)
        return configured

    def _embed_with_adaptive_retry(
        self,
        *,
        texts: list[str],
        model_name: str,
        url: str,
        headers: dict[str, str],
        request_batch_size: int,
    ) -> list[list[float]]:
        slots: list[list[float] | None] = [None] * len(texts)
        work: deque[tuple[int, int, int]] = deque()
        for start in range(0, len(texts), request_batch_size):
            end = min(start + request_batch_size, len(texts))
            work.append((start, end, 0))

        while work:
            start, end, single_retry = work.popleft()
            batch_texts = texts[start:end]
            payload = {
                "model": model_name,
                "input": batch_texts,
            }

            try:
                response = httpx.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=self.settings.embedding_timeout_seconds,
                )
                response.raise_for_status()
            except httpx.TimeoutException as exc:
                batch_len = end - start
                if batch_len > 1:
                    mid = start + (batch_len // 2)
                    # Keep original order: left batch first, then right batch.
                    work.appendleft((mid, end, 0))
                    work.appendleft((start, mid, 0))
                    continue

                max_single_retries = 2
                if single_retry < max_single_retries:
                    time.sleep(min(0.6 * (2**single_retry), 2.0))
                    work.appendleft((start, end, single_retry + 1))
                    continue

                raise DomainValidationError(
                    "Embedding request timed out after retries for a single input. "
                    "Increase EMBEDDING_TIMEOUT_SECONDS or switch to mock embedding."
                ) from exc
            except httpx.HTTPError as exc:
                details = ""
                if isinstance(exc, httpx.HTTPStatusError):
                    body = exc.response.text.strip()
                    if body:
                        details = f" body={body[:500]}"
                raise DomainValidationError(f"Embedding request failed: {exc}.{details}") from exc

            batch_vectors = self._parse_embeddings_response(
                response.json(),
                expected_size=len(batch_texts),
            )
            for offset, vector in enumerate(batch_vectors):
                slots[start + offset] = vector

        if any(vector is None for vector in slots):
            raise DomainValidationError("Embedding response assembly failed due to missing vectors.")

        return [vector for vector in slots if vector is not None]

    @staticmethod
    def _normalize_base_url(raw: str) -> str:
        value = raw.strip().rstrip("/")
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise DomainValidationError("embedding_base_url must be a valid http(s) URL.")
        return value
