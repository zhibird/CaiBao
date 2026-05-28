from __future__ import annotations

from unittest import mock

import pytest

from app.services.retrieval_enhancer import (
    EnhancedRetrievalResult,
    EnhancedRetrievalService,
    HyDEEnhancer,
    QueryRewriter,
    SufficiencyChecker,
)


class TestQueryRewriter:
    def test_returns_rewritten_query(self):
        rw = QueryRewriter()
        mock_llm = mock.MagicMock()
        mock_result = mock.MagicMock()
        mock_result.assistant_text = "expanded query with synonyms"
        mock_llm.complete_chat.return_value = mock_result

        result = rw.rewrite("test query", mock_llm, "fast-model", "url", "key")
        assert result == "expanded query with synonyms"

    def test_fail_open_returns_original(self):
        rw = QueryRewriter()
        mock_llm = mock.MagicMock()
        mock_llm.complete_chat.side_effect = RuntimeError("LLM down")

        result = rw.rewrite("important query", mock_llm, "fast-model", "url", "key")
        assert result == "important query"

    def test_skip_when_disabled(self):
        rw = QueryRewriter()
        with mock.patch("app.services.retrieval_enhancer.get_settings") as m:
            m.return_value.retrieval_query_rewrite_enabled = False
            result = rw.rewrite("x", mock.MagicMock(), "m", "u", "k")
            assert result == "x"


class TestHyDEEnhancer:
    def test_generates_hypothesis(self):
        hyde = HyDEEnhancer()
        mock_llm = mock.MagicMock()
        mock_result = mock.MagicMock()
        mock_result.assistant_text = "A hypothetical answer document."
        mock_llm.complete_chat.return_value = mock_result

        result = hyde.generate("what is RAG?", mock_llm, "fast", "url", "key")
        assert result == "A hypothetical answer document."

    def test_fail_open_returns_none(self):
        hyde = HyDEEnhancer()
        mock_llm = mock.MagicMock()
        mock_llm.complete_chat.side_effect = Exception("crash")

        result = hyde.generate("query", mock_llm, "fast", "url", "key")
        assert result is None

    def test_short_response_filtered_out(self):
        hyde = HyDEEnhancer()
        mock_llm = mock.MagicMock()
        mock_result = mock.MagicMock()
        mock_result.assistant_text = "short"
        mock_llm.complete_chat.return_value = mock_result

        result = hyde.generate("query", mock_llm, "fast", "url", "key")
        assert result is None


class TestSufficiencyChecker:
    def test_checks_hits(self):
        checker = SufficiencyChecker()
        mock_llm = mock.MagicMock()
        mock_result = mock.MagicMock()
        mock_result.assistant_text = '{"sufficient": true, "refined_query": null}'
        mock_llm.complete_chat.return_value = mock_result

        sufficient, refined = checker.check(
            [{"content": "relevant info"}], "query", mock_llm, "m", "u", "k",
        )
        assert sufficient is True
        assert refined is None

    def test_empty_hits_short_circuits(self):
        checker = SufficiencyChecker()
        mock_llm = mock.MagicMock()
        sufficient, refined = checker.check([], "query", mock_llm, "m", "u", "k")
        assert sufficient is False
        assert refined == "query"
        mock_llm.complete_chat.assert_not_called()

    def test_fail_open_returns_sufficient(self):
        checker = SufficiencyChecker()
        mock_llm = mock.MagicMock()
        mock_llm.complete_chat.side_effect = RuntimeError("fail")

        sufficient, _ = checker.check(
            [{"content": "x"}], "query", mock_llm, "m", "u", "k",
        )
        assert sufficient is True


class TestEnhancedRetrievalService:
    def test_llm_failure_falls_back_to_base_search(self):
        mock_retrieval = mock.MagicMock()
        mock_retrieval.search_chunks.return_value = [
            {"chunk_id": "c1", "content": "base result", "score": 0.9},
        ]
        mock_llm = mock.MagicMock()
        mock_llm.complete_chat.side_effect = RuntimeError("down")

        svc = EnhancedRetrievalService(mock_retrieval, mock_llm)
        result = svc.search_chunks_enhanced(
            query="test", team_id="t1", user_id="u1",
            fast_model_name="fast", fast_base_url="url", fast_api_key="key",
        )

        assert len(result.hits) == 1
        assert result.hits[0]["chunk_id"] == "c1"

    def test_hyde_merge_deduplicates(self):
        mock_retrieval = mock.MagicMock()
        mock_retrieval.search_chunks.side_effect = [
            [{"chunk_id": "c1", "content": "raw", "score": 0.8}],
            [{"chunk_id": "c1", "content": "same", "score": 0.7}],
            [{"chunk_id": "c2", "content": "hyde hit", "score": 0.9}],
        ]
        mock_llm = mock.MagicMock()
        mock_result = mock.MagicMock()
        mock_result.assistant_text = "hypothetical document about the query"
        mock_llm.complete_chat.return_value = mock_result

        with mock.patch("app.services.retrieval_enhancer.get_settings") as m:
            m.return_value.retrieval_enhancement_enabled = True
            m.return_value.retrieval_query_rewrite_enabled = True
            m.return_value.retrieval_hyde_enabled = True
            m.return_value.retrieval_sufficiency_enabled = False

            svc = EnhancedRetrievalService(mock_retrieval, mock_llm)
            result = svc.search_chunks_enhanced(
                query="test", team_id="t1", user_id="u1",
                fast_model_name="f", fast_base_url="u", fast_api_key="k",
            )

        chunk_ids = {h["chunk_id"] for h in result.hits}
        assert chunk_ids == {"c1", "c2"}
        # c1 from raw, c2 from hyde — raw score preserved
        raw_hit = next(h for h in result.hits if h["chunk_id"] == "c1")
        assert raw_hit["score"] == 0.8
