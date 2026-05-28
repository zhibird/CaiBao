from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from app.core.config import get_settings

_logger = logging.getLogger(__name__)


@dataclass
class EnhancedRetrievalResult:
    hits: list[dict[str, Any]] = field(default_factory=list)
    trace: dict[str, Any] = field(default_factory=dict)


class QueryRewriter:
    """Rewrite the user query for better retrieval recall. Fail-open."""

    def rewrite(self, query: str, llm_service, model_name: str, base_url: str, api_key: str) -> str:
        settings = get_settings()
        if not settings.retrieval_query_rewrite_enabled:
            return query

        try:
            fast_timeout = settings.retrieval_fast_timeout_ms / 1000.0
            result = llm_service.complete_chat(
                messages=[
                    {"role": "system", "content": (
                        "Rewrite the following search query to improve retrieval accuracy. "
                        "Expand abbreviations, add synonyms, and make the query more specific. "
                        "Return ONLY the rewritten query, no explanation."
                    )},
                    {"role": "user", "content": query},
                ],
                model=model_name,
                base_url=base_url,
                api_key=api_key,
                tools=None,
                timeout_seconds=fast_timeout,
            )
            rewritten = (result.assistant_text or "").strip()
            if rewritten and len(rewritten) >= 2:
                return rewritten
        except Exception:
            _logger.exception("QueryRewriter failed, returning original query")

        return query


class HyDEEnhancer:
    """Generate a hypothetical answer document for embedding-based retrieval. Fail-open."""

    def generate(
        self, query: str, llm_service, model_name: str, base_url: str, api_key: str,
    ) -> str | None:
        settings = get_settings()
        if not settings.retrieval_hyde_enabled:
            return None

        try:
            fast_timeout = settings.retrieval_fast_timeout_ms / 1000.0
            result = llm_service.complete_chat(
                messages=[
                    {"role": "system", "content": (
                        "Generate a short hypothetical paragraph that would answer the "
                        "following query. Write it as if it were a real document. "
                        "Return ONLY the paragraph, no preamble."
                    )},
                    {"role": "user", "content": query},
                ],
                model=model_name,
                base_url=base_url,
                api_key=api_key,
                tools=None,
                timeout_seconds=fast_timeout,
            )
            hyde = (result.assistant_text or "").strip()
            if hyde and len(hyde) >= 10:
                return hyde
        except Exception:
            _logger.exception("HyDE generation failed")

        return None


class SufficiencyChecker:
    """Check if retrieved hits are sufficient. If not, suggest a refined query."""

    def check(
        self, hits: list[dict], query: str, llm_service, model_name: str, base_url: str, api_key: str,
    ) -> tuple[bool, str | None]:
        settings = get_settings()
        if not settings.retrieval_sufficiency_enabled:
            return True, None

        if not hits:
            return False, query  # no hits → short-circuit

        try:
            snippets = "\n".join(
                f"- {h.get('content', '')[:120]}" for h in hits[:5]
            )
            fast_timeout = settings.retrieval_fast_timeout_ms / 1000.0
            result = llm_service.complete_chat(
                messages=[
                    {"role": "system", "content": (
                        "You are a retrieval quality checker. Given a user query and retrieved "
                        "document snippets, determine if the results are sufficient to answer "
                        "the query.\n\nReply with JSON only: "
                        '{"sufficient": true/false, "refined_query": "suggested improved query or null"}'
                    )},
                    {"role": "user", "content": (
                        f"Query: {query}\n\nRetrieved snippets:\n{snippets}"
                    )},
                ],
                model=model_name,
                base_url=base_url,
                api_key=api_key,
                tools=None,
                timeout_seconds=fast_timeout,
            )
            text = (result.assistant_text or "").strip()
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                parsed = json.loads(text[start:end])
                sufficient = bool(parsed.get("sufficient", True))
                refined = parsed.get("refined_query")
                return sufficient, refined if not sufficient else None
        except Exception:
            _logger.exception("Sufficiency check failed")

        return True, None


class EnhancedRetrievalService:
    """Orchestrates query rewriting, HyDE, and sufficiency checking."""

    def __init__(
        self,
        retrieval_service,
        llm_service,
    ) -> None:
        self._retrieval = retrieval_service
        self._llm = llm_service
        self._rewriter = QueryRewriter()
        self._hyde = HyDEEnhancer()
        self._sufficiency = SufficiencyChecker()

    def search_chunks_enhanced(
        self,
        *,
        query: str,
        team_id: str,
        user_id: str,
        fast_model_name: str,
        fast_base_url: str,
        fast_api_key: str,
        top_k: int = 20,
        **kwargs,
    ) -> EnhancedRetrievalResult:
        settings = get_settings()
        trace: dict[str, Any] = {"original_query": query}

        if not settings.retrieval_enhancement_enabled:
            hits = self._retrieval.search_chunks(
                query=query, team_id=team_id, user_id=user_id, top_k=top_k, **kwargs,
            )
            return EnhancedRetrievalResult(hits=hits, trace=trace)

        # 1. Rewrite query
        rewritten = self._rewriter.rewrite(
            query, self._llm, fast_model_name, fast_base_url, fast_api_key,
        )
        trace["rewritten_query"] = rewritten

        # 2. Raw query retrieval
        raw_hits = self._retrieval.search_chunks(
            query=query, team_id=team_id, user_id=user_id, top_k=top_k, **kwargs,
        )
        seen = {h["chunk_id"] for h in raw_hits}
        merged: list[dict] = list(raw_hits)

        # 3. Rewritten query retrieval
        if rewritten != query:
            rewritten_hits = self._retrieval.search_chunks(
                query=rewritten, team_id=team_id, user_id=user_id, top_k=top_k, **kwargs,
            )
            for h in rewritten_hits:
                if h["chunk_id"] not in seen:
                    seen.add(h["chunk_id"])
                    merged.append(h)

        # 4. HyDE
        hyde_text = self._hyde.generate(
            query, self._llm, fast_model_name, fast_base_url, fast_api_key,
        )
        trace["hyde_hypothesis"] = hyde_text
        if hyde_text:
            hyde_hits = self._retrieval.search_chunks(
                query=hyde_text, team_id=team_id, user_id=user_id, top_k=top_k, **kwargs,
            )
            for h in hyde_hits:
                if h["chunk_id"] not in seen:
                    seen.add(h["chunk_id"])
                    merged.append(h)
        trace["used_hyde"] = hyde_text is not None and bool(hyde_text)

        # 5. Sufficiency
        sufficient, refined = self._sufficiency.check(
            merged, query, self._llm, fast_model_name, fast_base_url, fast_api_key,
        )
        trace["sufficiency"] = {"sufficient": sufficient, "refined_query": refined}

        if not sufficient and refined:
            second_hits = self._retrieval.search_chunks(
                query=refined, team_id=team_id, user_id=user_id, top_k=top_k, **kwargs,
            )
            for h in second_hits:
                if h["chunk_id"] not in seen:
                    seen.add(h["chunk_id"])
                    merged.append(h)
            trace["second_pass_query"] = refined

        # Sort by score desc and truncate to top_k
        merged.sort(key=lambda h: h.get("score", 0), reverse=True)
        merged = merged[:top_k]
        return EnhancedRetrievalResult(hits=merged, trace=trace)
