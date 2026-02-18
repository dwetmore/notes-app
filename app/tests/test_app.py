import importlib
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ENV_KEYS = [
    "DB_HOST",
    "DB_PORT",
    "DB_NAME",
    "DB_USER",
    "DB_PASSWORD",
    "DB_PATH",
    "DATABASE_URL",
]


def clear_db_env() -> None:
    for key in ENV_KEYS:
        os.environ.pop(key, None)


def load_main_module():
    import main

    return importlib.reload(main)


def create_client(tmp_path: Path) -> TestClient:
    db_file = tmp_path / "test.db"
    clear_db_env()
    os.environ["DB_PATH"] = str(db_file)
    main = load_main_module()
    main.Base.metadata.create_all(bind=main.engine)
    return TestClient(main.app)


def test_healthz(tmp_path: Path):
    client = create_client(tmp_path)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz(tmp_path: Path):
    client = create_client(tmp_path)
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"ready": True}


def test_notes_crud(tmp_path: Path):
    client = create_client(tmp_path)

    create_response = client.post("/api/notes", json={"title": "First", "body": "Hello"})
    assert create_response.status_code == 200
    created = create_response.json()
    assert created["id"] > 0
    assert created["title"] == "First"

    list_response = client.get("/api/notes")
    assert list_response.status_code == 200
    listed = list_response.json()
    assert len(listed) == 1
    assert listed[0]["id"] == created["id"]

    update_response = client.put(
        f"/api/notes/{created['id']}",
        json={"title": "Updated", "body": "World"},
    )
    assert update_response.status_code == 200
    assert update_response.json()["title"] == "Updated"

    delete_response = client.delete(f"/api/notes/{created['id']}")
    assert delete_response.status_code == 200
    assert delete_response.json() == {"deleted": created["id"]}

    empty_list_response = client.get("/api/notes")
    assert empty_list_response.status_code == 200
    assert empty_list_response.json() == []


def test_db_selection_requires_env():
    clear_db_env()
    with pytest.raises(RuntimeError, match="Database backend is not configured"):
        load_main_module()


def test_db_host_has_priority_over_db_path(tmp_path: Path):
    clear_db_env()
    os.environ["DB_HOST"] = "postgres.internal"
    os.environ["DB_PATH"] = str(tmp_path / "should_not_be_used.db")
    main = load_main_module()
    assert main.BACKEND == "postgres"
    assert "postgres.internal" in main.DATABASE_URL


def test_redact_connection_target_hides_password():
    clear_db_env()
    os.environ["DATABASE_URL"] = "postgresql+psycopg://alice:secret@example.com:5432/notes"
    main = load_main_module()
    rendered = main.redact_connection_target(main.DATABASE_URL)
    assert "secret" not in rendered
    assert "***" in rendered
    assert "example.com" in rendered
