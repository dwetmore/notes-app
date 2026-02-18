import importlib
import os
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def create_client(tmp_path: Path) -> TestClient:
    db_file = tmp_path / "test.db"
    os.environ.pop("DB_HOST", None)
    os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{db_file}"
    import main

    importlib.reload(main)
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
