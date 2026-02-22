import logging
import os
from pathlib import Path
from uuid import uuid4
from typing import List, Optional

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel, ConfigDict
from sqlalchemy import ForeignKey, Integer, String, create_engine, delete, or_, select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Mapped, Session, declarative_base, mapped_column, sessionmaker

logger = logging.getLogger("notes_app")


def env_value(name: str) -> Optional[str]:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value if value else None


DATABASE_URL_ENV = env_value("DATABASE_URL")
POSTGRES_HOST = env_value("POSTGRES_HOST") or env_value("DB_HOST")
POSTGRES_PORT = env_value("POSTGRES_PORT") or env_value("DB_PORT") or "5432"
POSTGRES_DB = env_value("POSTGRES_DB") or env_value("DB_NAME") or "notes"
POSTGRES_USER = env_value("POSTGRES_USER") or env_value("DB_USER") or "notes"
POSTGRES_PASSWORD = env_value("POSTGRES_PASSWORD") or env_value("DB_PASSWORD") or "notes"
DB_PATH = env_value("DB_PATH")
UPLOAD_DIR = env_value("UPLOAD_DIR") or os.path.join(os.path.dirname(__file__), "uploads")
MAX_UPLOAD_SIZE = 10 * 1024 * 1024

if DATABASE_URL_ENV:
    DATABASE_URL = DATABASE_URL_ENV
    BACKEND = "postgres"
elif POSTGRES_HOST:
    DATABASE_URL = f"postgresql+psycopg://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
    BACKEND = "postgres"
elif DB_PATH:
    DATABASE_URL = f"sqlite+pysqlite:///{DB_PATH}"
    BACKEND = "sqlite"
else:
    raise RuntimeError(
        "Database backend is not configured. Set non-empty DATABASE_URL, DB_HOST/POSTGRES_HOST, or DB_PATH."
    )


def redact_connection_target(database_url: str) -> str:
    try:
        parsed_url: URL = make_url(database_url)
        return parsed_url.render_as_string(hide_password=parsed_url.password is not None)
    except Exception:
        return database_url


STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class Note(Base):
    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(String)


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    note_id: Mapped[int] = mapped_column(ForeignKey("notes.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    storage_name: Mapped[str] = mapped_column(String(255), unique=True)
    content_type: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(Integer)


class NoteIn(BaseModel):
    title: str
    body: str


class NoteOut(NoteIn):
    id: int
    model_config = ConfigDict(from_attributes=True)


class AttachmentOut(BaseModel):
    id: int
    note_id: int
    filename: str
    content_type: str
    size_bytes: int
    download_url: str
    model_config = ConfigDict(from_attributes=True)


app = FastAPI(title="Notes App")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
Instrumentator().instrument(app).expose(app, include_in_schema=False)


@app.on_event("startup")
def on_startup() -> None:
    logger.info("Selected DB backend=%s target=%s", BACKEND, redact_connection_target(DATABASE_URL))
    Path(UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)


def get_db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@app.get("/")
def root():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"ready": True}
    except Exception as exc:
        detail = f"{BACKEND} readiness failed: {exc}"
        raise HTTPException(status_code=503, detail=detail) from exc


@app.get("/api/notes", response_model=List[NoteOut])
def list_notes(search: Optional[str] = None, db: Session = Depends(get_db)):
    query = select(Note).order_by(Note.id.desc())
    if search:
        term = f"%{search.strip()}%"
        query = query.where(or_(Note.title.like(term), Note.body.like(term)))
    return db.execute(query).scalars().all()


@app.post("/api/notes", response_model=NoteOut)
def create_note(note: NoteIn, db: Session = Depends(get_db)):
    new_note = Note(title=note.title, body=note.body)
    db.add(new_note)
    db.commit()
    db.refresh(new_note)
    return new_note


@app.post("/api/notes/{note_id}/attachments", response_model=AttachmentOut)
def upload_attachment(note_id: int, file: UploadFile = File(...), db: Session = Depends(get_db)):
    note = db.get(Note, note_id)
    if not note:
        raise HTTPException(status_code=404, detail="note not found")

    original_name = os.path.basename(file.filename or "")
    if not original_name:
        raise HTTPException(status_code=400, detail="filename is required")

    data = file.file.read(MAX_UPLOAD_SIZE + 1)
    file.file.close()
    if len(data) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="file too large (max 10 MiB)")

    storage_name = f"{uuid4().hex}-{original_name}"
    destination = Path(UPLOAD_DIR) / storage_name
    destination.write_bytes(data)

    attachment = Attachment(
        note_id=note_id,
        filename=original_name,
        storage_name=storage_name,
        content_type=file.content_type or "application/octet-stream",
        size_bytes=len(data),
    )
    try:
        db.add(attachment)
        db.commit()
        db.refresh(attachment)
    except Exception:
        if destination.exists():
            destination.unlink()
        raise
    return AttachmentOut(
        id=attachment.id,
        note_id=attachment.note_id,
        filename=attachment.filename,
        content_type=attachment.content_type,
        size_bytes=attachment.size_bytes,
        download_url=f"/api/attachments/{attachment.id}/download",
    )


@app.get("/api/notes/{note_id}/attachments", response_model=List[AttachmentOut])
def list_attachments(note_id: int, db: Session = Depends(get_db)):
    note = db.get(Note, note_id)
    if not note:
        raise HTTPException(status_code=404, detail="note not found")
    items = db.execute(select(Attachment).where(Attachment.note_id == note_id).order_by(Attachment.id.desc())).scalars().all()
    return [
        AttachmentOut(
            id=item.id,
            note_id=item.note_id,
            filename=item.filename,
            content_type=item.content_type,
            size_bytes=item.size_bytes,
            download_url=f"/api/attachments/{item.id}/download",
        )
        for item in items
    ]


@app.get("/api/attachments/{attachment_id}/download")
def download_attachment(attachment_id: int, db: Session = Depends(get_db)):
    item = db.get(Attachment, attachment_id)
    if not item:
        raise HTTPException(status_code=404, detail="attachment not found")
    path = Path(UPLOAD_DIR) / item.storage_name
    if not path.exists():
        raise HTTPException(status_code=404, detail="attachment file missing")
    return FileResponse(
        path,
        media_type=item.content_type or "application/octet-stream",
        filename=item.filename,
    )


@app.delete("/api/attachments/{attachment_id}")
def delete_attachment(attachment_id: int, db: Session = Depends(get_db)):
    item = db.get(Attachment, attachment_id)
    if not item:
        raise HTTPException(status_code=404, detail="attachment not found")
    path = Path(UPLOAD_DIR) / item.storage_name
    if path.exists():
        path.unlink()
    db.delete(item)
    db.commit()
    return JSONResponse({"deleted": attachment_id})


@app.put("/api/notes/{note_id}", response_model=NoteOut)
def update_note(note_id: int, note: NoteIn, db: Session = Depends(get_db)):
    existing = db.get(Note, note_id)
    if not existing:
        raise HTTPException(status_code=404, detail="not found")
    existing.title = note.title
    existing.body = note.body
    db.commit()
    db.refresh(existing)
    return existing


@app.delete("/api/notes/{note_id}")
def delete_note(note_id: int, db: Session = Depends(get_db)):
    existing = db.get(Note, note_id)
    if not existing:
        raise HTTPException(status_code=404, detail="not found")
    attachments = db.execute(select(Attachment).where(Attachment.note_id == note_id)).scalars().all()
    for item in attachments:
        path = Path(UPLOAD_DIR) / item.storage_name
        if path.exists():
            path.unlink()
    db.execute(delete(Attachment).where(Attachment.note_id == note_id))
    db.delete(existing)
    db.commit()
    return {"deleted": note_id}
