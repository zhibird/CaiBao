import time
import zipfile
from io import BytesIO

from PIL import Image
from pypdf import PdfWriter

from tests.auth_helpers import register_workspace_user, register_workspace_user_in_team


def _wait_document_terminal_status(client, *, document_id: str, max_attempts: int = 30) -> str:
    for _ in range(max_attempts):
        response = client.get(f"/api/v1/documents/{document_id}")
        assert response.status_code == 200
        status = response.json()["status"]
        if status in {"ready", "failed"}:
            return status
        time.sleep(0.05)
    raise AssertionError("document did not reach terminal status in time")


def _build_pdf_bytes() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _build_png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (2, 2), color=(255, 255, 255)).save(output, format="PNG")
    return output.getvalue()


def _build_docx_bytes() -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>""",
        )
        archive.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>""",
        )
        archive.writestr(
            "word/document.xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>Quarterly revenue increased.</w:t></w:r></w:p>
    <w:p><w:r><w:t>Action item: review APAC pipeline.</w:t></w:r></w:p>
  </w:body>
</w:document>""",
        )
    return output.getvalue()


def _build_xlsx_bytes() -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>
</Types>""",
        )
        archive.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
        )
        archive.writestr(
            "xl/workbook.xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="Metrics" sheetId="1" r:id="rId1"/>
  </sheets>
</workbook>""",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>
</Relationships>""",
        )
        archive.writestr(
            "xl/sharedStrings.xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="4" uniqueCount="4">
  <si><t>Region</t></si>
  <si><t>Revenue</t></si>
  <si><t>APAC</t></si>
  <si><t>EMEA</t></si>
</sst>""",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1">
      <c r="A1" t="s"><v>0</v></c>
      <c r="B1" t="s"><v>1</v></c>
    </row>
    <row r="2">
      <c r="A2" t="s"><v>2</v></c>
      <c r="B2"><v>128</v></c>
    </row>
    <row r="3">
      <c r="A3" t="s"><v>3</v></c>
      <c r="B3"><v>96</v></c>
    </row>
  </sheetData>
</worksheet>""",
        )
    return output.getvalue()


def _create_conversation(client, *, title: str, team_id: str | None = None, user_id: str | None = None) -> dict[str, object]:
    response = client.post(
        "/api/v1/conversations",
        json={
            "team_id": team_id or "ignored-team",
            "user_id": user_id or "ignored-user",
            "title": title,
        },
    )
    assert response.status_code == 201
    return response.json()


def _create_space(client, *, name: str) -> dict[str, object]:
    response = client.post(
        "/api/v1/spaces",
        json={
            "name": name,
            "description": None,
        },
    )
    assert response.status_code == 201
    return response.json()


def test_document_routes_require_authenticated_user(client) -> None:
    client.cookies.clear()

    list_response = client.get("/api/v1/documents")
    assert list_response.status_code == 401

    import_response = client.post(
        "/api/v1/documents/import",
        json={
            "source_name": "ops-guide.md",
            "content_type": "md",
            "content": "# Ops Guide\n\nAlways check alerts first.",
        },
    )
    assert import_response.status_code == 401


def test_document_import_and_query_in_team(client) -> None:
    team_id, _ = register_workspace_user(
        client,
        prefix="doc_team",
        display_name="Doc Team",
    )

    import_response = client.post(
        "/api/v1/documents/import",
        json={
            "team_id": "ignored-team",
            "user_id": "ignored-user",
            "source_name": "ops-guide.md",
            "content_type": "md",
            "content": "# Ops Guide\n\nAlways check alerts first.",
        },
    )
    assert import_response.status_code == 201
    assert import_response.json()["team_id"] == team_id
    document_id = import_response.json()["document_id"]

    list_response = client.get("/api/v1/documents")
    assert list_response.status_code == 200
    listed_ids = [item["document_id"] for item in list_response.json()]
    assert document_id in listed_ids

    get_response = client.get(f"/api/v1/documents/{document_id}")
    assert get_response.status_code == 200
    assert get_response.json()["source_name"] == "ops-guide.md"
    assert get_response.json()["team_id"] == team_id


def test_document_upload_and_file_download(client) -> None:
    team_id, user_id = register_workspace_user(
        client,
        prefix="doc_upload",
        display_name="Upload Team",
    )
    conversation = _create_conversation(client, title="Upload Session", team_id=team_id, user_id=user_id)

    upload_response = client.post(
        "/api/v1/documents/upload",
        data={
            "conversation_id": conversation["conversation_id"],
            "auto_index": "false",
        },
        files={
            "file": (
                "notes.md",
                b"# Uploaded Notes\n\nKeep this for later.",
                "text/markdown",
            )
        },
    )
    assert upload_response.status_code == 201
    uploaded = upload_response.json()
    assert uploaded["team_id"] == team_id
    assert uploaded["conversation_id"] == conversation["conversation_id"]
    assert uploaded["source_name"] == "notes.md"

    file_response = client.get(f"/api/v1/documents/{uploaded['document_id']}/file")
    assert file_response.status_code == 200
    assert file_response.content == b"# Uploaded Notes\n\nKeep this for later."


def test_document_chunking_and_list_chunks(client) -> None:
    team_id, _ = register_workspace_user(
        client,
        prefix="doc_chunk",
        display_name="Chunk Team",
    )

    import_response = client.post(
        "/api/v1/documents/import",
        json={
            "source_name": "long.txt",
            "content_type": "txt",
            "content": "A" * 420,
        },
    )
    assert import_response.status_code == 201
    document_id = import_response.json()["document_id"]

    chunk_response = client.post(
        f"/api/v1/documents/{document_id}/chunk",
        json={
            "team_id": "ignored-team",
            "max_chars": 120,
            "overlap": 20,
        },
    )
    assert chunk_response.status_code == 200
    body = chunk_response.json()
    assert body["team_id"] == team_id
    assert body["total_chunks"] >= 3

    list_chunks_response = client.get(f"/api/v1/documents/{document_id}/chunks")
    assert list_chunks_response.status_code == 200
    assert len(list_chunks_response.json()) == body["total_chunks"]


def test_document_chunking_rejects_invalid_overlap(client) -> None:
    register_workspace_user(
        client,
        prefix="doc_chunk_invalid",
        display_name="Chunk Invalid Team",
    )

    import_response = client.post(
        "/api/v1/documents/import",
        json={
            "source_name": "short.txt",
            "content_type": "txt",
            "content": "short content for testing",
        },
    )
    assert import_response.status_code == 201
    document_id = import_response.json()["document_id"]

    chunk_response = client.post(
        f"/api/v1/documents/{document_id}/chunk",
        json={
            "max_chars": 100,
            "overlap": 100,
        },
    )
    assert chunk_response.status_code == 400


def test_document_import_ignores_client_supplied_identity(client) -> None:
    team_id, _ = register_workspace_user(
        client,
        prefix="doc_identity",
        display_name="Identity User",
    )

    response = client.post(
        "/api/v1/documents/import",
        json={
            "team_id": "team_not_exists",
            "user_id": "other-user",
            "source_name": "faq.txt",
            "content_type": "txt",
            "content": "Q: Who handles incidents?",
        },
    )

    assert response.status_code == 201
    assert response.json()["team_id"] == team_id


def test_document_status_and_delete_flow(client) -> None:
    register_workspace_user(
        client,
        prefix="doc_status",
        display_name="Doc Status Team",
    )

    import_response = client.post(
        "/api/v1/documents/import",
        json={
            "source_name": "status.md",
            "content_type": "md",
            "content": "# Status\n\nChunk and index me.",
        },
    )
    assert import_response.status_code == 201
    document_id = import_response.json()["document_id"]
    assert import_response.json()["status"] == "uploaded"

    chunk_response = client.post(
        f"/api/v1/documents/{document_id}/chunk",
        json={
            "max_chars": 100,
            "overlap": 10,
        },
    )
    assert chunk_response.status_code == 200

    after_chunk = client.get(f"/api/v1/documents/{document_id}")
    assert after_chunk.status_code == 200
    assert after_chunk.json()["status"] == "uploaded"

    index_response = client.post(
        "/api/v1/retrieval/index",
        json={
            "document_ids": [document_id],
        },
    )
    assert index_response.status_code == 200

    after_index = client.get(f"/api/v1/documents/{document_id}")
    assert after_index.status_code == 200
    assert after_index.json()["status"] == "ready"

    delete_response = client.delete(f"/api/v1/documents/{document_id}")
    assert delete_response.status_code == 204


def test_same_team_user_cannot_access_foreign_conversation_documents(client) -> None:
    owner_team_id, owner_user_id = register_workspace_user(
        client,
        prefix="doc_owner",
        display_name="Doc Owner",
    )
    conversation = _create_conversation(
        client,
        title="Owner Conversation",
        team_id=owner_team_id,
        user_id=owner_user_id,
    )

    upload_response = client.post(
        "/api/v1/documents/upload",
        data={
            "conversation_id": conversation["conversation_id"],
            "auto_index": "false",
        },
        files={
            "file": (
                "secret.md",
                b"owner-only document",
                "text/markdown",
            )
        },
    )
    assert upload_response.status_code == 201
    document_id = upload_response.json()["document_id"]

    peer_team_id, peer_user_id = register_workspace_user_in_team(
        client,
        team_id=owner_team_id,
        prefix="doc_peer",
        display_name="Doc Peer",
    )
    assert peer_team_id == owner_team_id
    assert peer_user_id != owner_user_id

    list_response = client.get("/api/v1/documents")
    assert list_response.status_code == 200
    assert all(item["document_id"] != document_id for item in list_response.json())

    foreign_list_response = client.get(
        "/api/v1/documents",
        params={"conversation_id": conversation["conversation_id"]},
    )
    assert foreign_list_response.status_code == 404

    get_response = client.get(f"/api/v1/documents/{document_id}")
    assert get_response.status_code == 404

    file_response = client.get(f"/api/v1/documents/{document_id}/file")
    assert file_response.status_code == 404

    delete_response = client.delete(f"/api/v1/documents/{document_id}")
    assert delete_response.status_code == 404


def test_same_team_user_cannot_access_foreign_space_documents(client) -> None:
    owner_team_id, owner_user_id = register_workspace_user(
        client,
        prefix="doc_space_owner",
        display_name="Doc Space Owner",
    )
    space = _create_space(client, name="Owner Space")

    import_response = client.post(
        "/api/v1/documents/import",
        json={
            "space_id": space["space_id"],
            "source_name": "space-secret.md",
            "content_type": "md",
            "content": "# Space Secret\n\nOnly the space owner should see this.",
        },
    )
    assert import_response.status_code == 201
    assert import_response.json()["team_id"] == owner_team_id
    assert import_response.json()["space_id"] == space["space_id"]
    assert import_response.json()["conversation_id"] is None
    document_id = import_response.json()["document_id"]

    peer_team_id, peer_user_id = register_workspace_user_in_team(
        client,
        team_id=owner_team_id,
        prefix="doc_space_peer",
        display_name="Doc Space Peer",
    )
    assert peer_team_id == owner_team_id
    assert peer_user_id != owner_user_id

    list_response = client.get("/api/v1/documents")
    assert list_response.status_code == 200
    assert all(item["document_id"] != document_id for item in list_response.json())

    foreign_space_list = client.get(
        "/api/v1/documents",
        params={"space_id": space["space_id"]},
    )
    assert foreign_space_list.status_code == 404

    get_response = client.get(f"/api/v1/documents/{document_id}")
    assert get_response.status_code == 404

    file_response = client.get(f"/api/v1/documents/{document_id}/file")
    assert file_response.status_code == 404

    delete_response = client.delete(f"/api/v1/documents/{document_id}")
    assert delete_response.status_code == 404
