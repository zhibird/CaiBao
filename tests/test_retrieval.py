from sqlalchemy import text

from app.db.session import engine
from tests.auth_helpers import register_workspace_user, register_workspace_user_in_team


def _create_conversation(client, *, team_id: str, user_id: str, title: str) -> dict[str, object]:
    response = client.post(
        "/api/v1/conversations",
        json={
            "team_id": "ignored-team",
            "user_id": "ignored-user",
            "title": title,
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["team_id"] == team_id
    assert body["user_id"] == user_id
    return body


def test_retrieval_routes_require_authenticated_user(client) -> None:
    client.cookies.clear()

    index_response = client.post(
        "/api/v1/retrieval/index",
        json={"document_ids": ["doc-1"]},
    )
    assert index_response.status_code == 401

    search_response = client.post(
        "/api/v1/retrieval/search",
        json={"query": "alerts", "top_k": 3},
    )
    assert search_response.status_code == 401


def test_retrieval_index_and_search(client) -> None:
    team_id, user_id = register_workspace_user(
        client,
        prefix="retrieval_team",
        display_name="Retrieval Team",
    )

    import_response = client.post(
        "/api/v1/documents/import",
        json={
            "source_name": "kb.md",
            "content_type": "md",
            "content": (
                "# Ops Playbook\n\n"
                "Always check alerts first. Escalate production incidents quickly. "
                "Keep incident channels updated every 10 minutes. "
                "Record timeline details for postmortem review."
            ),
        },
    )
    assert import_response.status_code == 201
    document_id = import_response.json()["document_id"]

    chunk_response = client.post(
        f"/api/v1/documents/{document_id}/chunk",
        json={
            "max_chars": 100,
            "overlap": 10,
        },
    )
    assert chunk_response.status_code == 200
    assert chunk_response.json()["total_chunks"] >= 2

    index_response = client.post(
        "/api/v1/retrieval/index",
        json={
            "team_id": "ignored-team",
            "user_id": "ignored-user",
            "document_id": document_id,
        },
    )
    assert index_response.status_code == 200
    assert index_response.json()["team_id"] == team_id
    assert index_response.json()["user_id"] == user_id
    assert index_response.json()["indexed_chunks"] >= 2

    search_response = client.post(
        "/api/v1/retrieval/search",
        json={
            "team_id": "ignored-team",
            "query": "alerts",
            "top_k": 3,
            "document_id": document_id,
        },
    )
    assert search_response.status_code == 200

    body = search_response.json()
    assert body["team_id"] == team_id
    assert body["user_id"] == user_id
    assert len(body["hits"]) >= 1
    assert body["hits"][0]["team_id"] == team_id
    assert body["hits"][0]["source_name"] == "kb.md"


def test_retrieval_search_requires_indexing_first(client) -> None:
    register_workspace_user(
        client,
        prefix="retrieval_empty",
        display_name="No Index Team",
    )

    search_response = client.post(
        "/api/v1/retrieval/search",
        json={
            "query": "alerts",
            "top_k": 5,
        },
    )

    assert search_response.status_code == 404


def test_retrieval_index_supports_rebuild(client) -> None:
    register_workspace_user(
        client,
        prefix="retrieval_rebuild",
        display_name="Rebuild Team",
    )

    import_response = client.post(
        "/api/v1/documents/import",
        json={
            "source_name": "rebuild.md",
            "content_type": "md",
            "content": "hello rebuild world",
        },
    )
    assert import_response.status_code == 201
    document_id = import_response.json()["document_id"]

    chunk_response = client.post(
        f"/api/v1/documents/{document_id}/chunk",
        json={
            "max_chars": 100,
            "overlap": 5,
        },
    )
    assert chunk_response.status_code == 200

    first_index = client.post(
        "/api/v1/retrieval/index",
        json={
            "document_id": document_id,
        },
    )
    assert first_index.status_code == 200

    rebuilt_index = client.post(
        "/api/v1/retrieval/index",
        json={
            "document_id": document_id,
            "rebuild": True,
        },
    )
    assert rebuilt_index.status_code == 200
    assert rebuilt_index.json()["indexed_chunks"] >= 1


def test_retrieval_search_skips_dimension_mismatched_vectors(client) -> None:
    team_id, _ = register_workspace_user(
        client,
        prefix="retrieval_dim",
        display_name="Dim Team",
    )

    import_response = client.post(
        "/api/v1/documents/import",
        json={
            "source_name": "dim.md",
            "content_type": "md",
            "content": (
                "Always check alerts first when investigating incidents.\n\n"
                "Escalate production incidents quickly and keep the channel updated.\n\n"
                "Record timeline details for postmortem review afterwards."
            ),
        },
    )
    assert import_response.status_code == 201
    document_id = import_response.json()["document_id"]

    chunk_response = client.post(
        f"/api/v1/documents/{document_id}/chunk",
        json={
            "max_chars": 100,
            "overlap": 5,
        },
    )
    assert chunk_response.status_code == 200
    chunks = chunk_response.json()["chunks"]
    assert len(chunks) >= 2

    index_response = client.post(
        "/api/v1/retrieval/index",
        json={
            "document_id": document_id,
        },
    )
    assert index_response.status_code == 200

    stale_chunk_id = chunks[0]["chunk_id"]
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE chunk_embeddings
                SET vector_json = :vector_json, vector_dim = :vector_dim
                WHERE team_id = :team_id AND chunk_id = :chunk_id
                """
            ),
            {
                "vector_json": "[0.1,0.2]",
                "vector_dim": 2,
                "team_id": team_id,
                "chunk_id": stale_chunk_id,
            },
        )

    # A single stale vector must not fail the whole search; it is skipped.
    search_response = client.post(
        "/api/v1/retrieval/search",
        json={
            "query": "alerts",
            "top_k": 5,
            "document_id": document_id,
        },
    )
    assert search_response.status_code == 200
    hits = search_response.json()["hits"]
    assert len(hits) >= 1
    assert all(hit["chunk_id"] != stale_chunk_id for hit in hits)

    # When every vector is stale, surface a rebuild hint instead of empty hits.
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE chunk_embeddings
                SET vector_json = :vector_json, vector_dim = :vector_dim
                WHERE team_id = :team_id
                """
            ),
            {
                "vector_json": "[0.1,0.2]",
                "vector_dim": 2,
                "team_id": team_id,
            },
        )

    all_stale_response = client.post(
        "/api/v1/retrieval/search",
        json={
            "query": "alerts",
            "top_k": 5,
            "document_id": document_id,
        },
    )
    assert all_stale_response.status_code == 404
    assert "rebuild retrieval index" in all_stale_response.json()["detail"].lower()


def test_retrieval_search_filters_other_embedding_models(client) -> None:
    team_id, _ = register_workspace_user(
        client,
        prefix="retrieval_model",
        display_name="Model Team",
    )

    import_response = client.post(
        "/api/v1/documents/import",
        json={
            "source_name": "model.md",
            "content_type": "md",
            "content": "embedding model isolation check content",
        },
    )
    assert import_response.status_code == 201
    document_id = import_response.json()["document_id"]

    chunk_response = client.post(
        f"/api/v1/documents/{document_id}/chunk",
        json={
            "max_chars": 100,
            "overlap": 5,
        },
    )
    assert chunk_response.status_code == 200

    index_response = client.post(
        "/api/v1/retrieval/index",
        json={
            "document_id": document_id,
        },
    )
    assert index_response.status_code == 200

    # Vectors written by another embedding model must not be silently mixed in,
    # even when the dimensions happen to match.
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE chunk_embeddings
                SET embedding_model = :embedding_model
                WHERE team_id = :team_id
                """
            ),
            {
                "embedding_model": "legacy_model_v0",
                "team_id": team_id,
            },
        )

    search_response = client.post(
        "/api/v1/retrieval/search",
        json={
            "query": "isolation",
            "top_k": 3,
            "document_id": document_id,
        },
    )
    assert search_response.status_code == 404
    assert search_response.json()["detail"] == "No indexed chunks found. Run retrieval indexing first."


def test_retrieval_routes_hide_foreign_document_scope(client) -> None:
    owner_team_id, _ = register_workspace_user(
        client,
        prefix="retrieval_owner",
        display_name="Owner",
    )

    import_response = client.post(
        "/api/v1/documents/import",
        json={
            "source_name": "owner-kb.md",
            "content_type": "md",
            "content": "Owner-only retrieval content.",
        },
    )
    assert import_response.status_code == 201
    document_id = import_response.json()["document_id"]

    chunk_response = client.post(
        f"/api/v1/documents/{document_id}/chunk",
        json={"max_chars": 100, "overlap": 5},
    )
    assert chunk_response.status_code == 200

    index_response = client.post(
        "/api/v1/retrieval/index",
        json={"document_id": document_id},
    )
    assert index_response.status_code == 200

    logout_response = client.post("/api/v1/auth/logout")
    assert logout_response.status_code == 204

    intruder_team_id, _ = register_workspace_user(
        client,
        prefix="retrieval_intruder",
        display_name="Intruder",
    )
    assert intruder_team_id != owner_team_id

    foreign_index = client.post(
        "/api/v1/retrieval/index",
        json={
            "team_id": owner_team_id,
            "document_id": document_id,
        },
    )
    assert foreign_index.status_code == 404
    assert foreign_index.json()["detail"] == "No documents found for indexing scope."

    foreign_search = client.post(
        "/api/v1/retrieval/search",
        json={
            "team_id": owner_team_id,
            "query": "owner-only",
            "top_k": 3,
            "document_id": document_id,
        },
    )
    assert foreign_search.status_code == 404
    assert foreign_search.json()["detail"] == "No indexed chunks found. Run retrieval indexing first."


def test_retrieval_routes_hide_same_team_foreign_document_scope(client) -> None:
    owner_team_id, owner_user_id = register_workspace_user(
        client,
        prefix="retrieval_owner_same_team",
        display_name="Owner Same Team",
    )
    conversation = _create_conversation(
        client,
        team_id=owner_team_id,
        user_id=owner_user_id,
        title="Owner Retrieval Session",
    )

    import_response = client.post(
        "/api/v1/documents/import",
        json={
            "conversation_id": conversation["conversation_id"],
            "source_name": "owner-kb.md",
            "content_type": "md",
            "content": "Owner-only retrieval content.",
        },
    )
    assert import_response.status_code == 201
    document_id = import_response.json()["document_id"]

    chunk_response = client.post(
        f"/api/v1/documents/{document_id}/chunk",
        json={"max_chars": 100, "overlap": 5},
    )
    assert chunk_response.status_code == 200

    index_response = client.post(
        "/api/v1/retrieval/index",
        json={"document_id": document_id},
    )
    assert index_response.status_code == 200

    peer_team_id, peer_user_id = register_workspace_user_in_team(
        client,
        team_id=owner_team_id,
        prefix="retrieval_peer_same_team",
        display_name="Peer Same Team",
    )
    assert peer_team_id == owner_team_id
    assert peer_user_id != owner_user_id

    foreign_index = client.post(
        "/api/v1/retrieval/index",
        json={"document_id": document_id},
    )
    assert foreign_index.status_code == 404

    foreign_search = client.post(
        "/api/v1/retrieval/search",
        json={
            "query": "owner-only",
            "top_k": 3,
            "document_id": document_id,
        },
    )
    assert foreign_search.status_code == 404
