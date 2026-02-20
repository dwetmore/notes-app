import importlib
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ENV_KEYS = [
    "DATABASE_URL",
    "DB_HOST",
    "DB_PORT",
    "DB_NAME",
    "DB_USER",
    "DB_PASSWORD",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "DB_PATH",
]

def clear_db_env() -> None:
    for key in ENV_KEYS:
        os.environ.pop(key, None)


def load_main_module():
    import main

    return importlib.reload(main)


def create_client(tmp_path: Path) -> TestClient:
    clear_db_env()
    os.environ["DB_PATH"] = str(tmp_path / "test.db")
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


def test_database_url_wins_over_db_host_and_db_path(tmp_path: Path):
    clear_db_env()
    os.environ["DATABASE_URL"] = "postgresql+psycopg://alice:secret@example.com:5432/notes"
    os.environ["DB_HOST"] = "ignored-host"
    os.environ["DB_PATH"] = str(tmp_path / "should-not-be-used.db")

    main = load_main_module()
    assert main.BACKEND == "postgres"
    assert main.DATABASE_URL == os.environ["DATABASE_URL"]


def test_db_host_selects_postgres(tmp_path: Path):
    clear_db_env()
    os.environ["DB_HOST"] = "postgres.internal"
    os.environ["DB_PATH"] = str(tmp_path / "should_not_be_used.db")

    main = load_main_module()
    assert main.BACKEND == "postgres"
    assert "postgres.internal" in main.DATABASE_URL


def test_postgres_host_selects_postgres(tmp_path: Path):
    clear_db_env()
    os.environ["POSTGRES_HOST"] = "pg.internal"
    os.environ["DB_PATH"] = str(tmp_path / "should_not_be_used.db")

    main = load_main_module()
    assert main.BACKEND == "postgres"
    assert "pg.internal" in main.DATABASE_URL


def test_db_path_selects_sqlite():
    clear_db_env()
    os.environ["DB_PATH"] = "/tmp/x.db"

    main = load_main_module()
    assert main.BACKEND == "sqlite"
    assert main.DATABASE_URL == "sqlite+pysqlite:////tmp/x.db"


def test_empty_db_path_does_not_select_sqlite():
    clear_db_env()
    os.environ["DB_PATH"] = "   "

    with pytest.raises(RuntimeError, match="Database backend is not configured"):
        load_main_module()


def test_empty_db_path_falls_back_to_postgres_when_host_present():
    clear_db_env()
    os.environ["DB_PATH"] = ""
    os.environ["DB_HOST"] = "postgres.internal"

    main = load_main_module()
    assert main.BACKEND == "postgres"


def test_redact_connection_target_hides_password():
    clear_db_env()
    os.environ["DATABASE_URL"] = "postgresql+psycopg://alice:secret@example.com:5432/notes"

    main = load_main_module()
    rendered = main.redact_connection_target(main.DATABASE_URL)
    assert "secret" not in rendered
    assert "***" in rendered
    assert "example.com" in rendered


def test_readyz_returns_real_backend_error(tmp_path: Path):
    client = create_client(tmp_path)
    import main

    class BoomConn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, *args, **kwargs):
            raise RuntimeError("db down")

    original_connect = main.engine.connect
    main.engine.connect = lambda: BoomConn()
    try:
        response = client.get("/readyz")
    finally:
        main.engine.connect = original_connect

    assert response.status_code == 503
    assert response.json() == {"detail": f"{main.BACKEND} readiness failed: db down"}
