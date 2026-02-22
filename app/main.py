import logging
import os
from datetime import datetime, timezone
from html import escape as html_escape
from pathlib import Path
from uuid import uuid4
from typing import List, Optional

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Boolean, ForeignKey, Integer, String, create_engine, delete, inspect, or_, select, text
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
UPLOAD_DIR = env_value("UPLOAD_DIR") or "/data/uploads"
UPLOAD_MAX_SIZE_MB = int(env_value("UPLOAD_MAX_SIZE_MB") or "700")
MAX_UPLOAD_SIZE = UPLOAD_MAX_SIZE_MB * 1024 * 1024

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
    tags_text: Mapped[str] = mapped_column(String(1024), default="")
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    share_token: Mapped[Optional[str]] = mapped_column(String(64), unique=True, nullable=True)


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    note_id: Mapped[int] = mapped_column(ForeignKey("notes.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    storage_name: Mapped[str] = mapped_column(String(255), unique=True)
    content_type: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(Integer)


class NoteHistory(Base):
    __tablename__ = "note_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    note_id: Mapped[int] = mapped_column(Integer, index=True)
    action: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(String)
    tags_text: Mapped[str] = mapped_column(String(1024), default="")
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[str] = mapped_column(String(64))


class NoteIn(BaseModel):
    title: str
    body: str
    tags: List[str] = Field(default_factory=list)
    pinned: bool = False


class NoteOut(NoteIn):
    id: int
    archived: bool = False
    share_url: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)


class AttachmentOut(BaseModel):
    id: int
    note_id: int
    filename: str
    content_type: str
    size_bytes: int
    download_url: str
    model_config = ConfigDict(from_attributes=True)


class NoteHistoryOut(BaseModel):
    id: int
    note_id: int
    action: str
    title: str
    body: str
    tags: List[str]
    pinned: bool
    archived: bool
    created_at: str


class ShareOut(BaseModel):
    note_id: int
    share_url: str


app = FastAPI(title="Notes App")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
Instrumentator().instrument(app).expose(app, include_in_schema=False)


def normalize_tags(tags: Optional[List[str]]) -> List[str]:
    if not tags:
        return []
    seen = set()
    cleaned: List[str] = []
    for raw in tags:
        tag = (raw or "").strip().lower()
        if not tag:
            continue
        if tag not in seen:
            seen.add(tag)
            cleaned.append(tag)
    return cleaned


def serialize_tags(tags: Optional[List[str]]) -> str:
    return ",".join(normalize_tags(tags))


def parse_tags(tags_text: Optional[str]) -> List[str]:
    if not tags_text:
        return []
    return [t for t in tags_text.split(",") if t]


def to_note_out(note: Note) -> NoteOut:
    share_url = f"/share/{note.share_token}" if note.share_token else None
    return NoteOut(
        id=note.id,
        title=note.title,
        body=note.body,
        tags=parse_tags(note.tags_text),
        pinned=bool(note.pinned),
        archived=bool(note.archived),
        share_url=share_url,
    )


def record_note_history(db: Session, note: Note, action: str) -> None:
    db.add(
        NoteHistory(
            note_id=note.id,
            action=action,
            title=note.title,
            body=note.body,
            tags_text=note.tags_text or "",
            pinned=bool(note.pinned),
            archived=bool(note.archived),
            created_at=datetime.now(timezone.utc).isoformat(),
        )
    )


def ensure_notes_schema() -> None:
    column_defs = {
        "tags_text": "VARCHAR(1024) NOT NULL DEFAULT ''",
        "pinned": "BOOLEAN NOT NULL DEFAULT FALSE",
        "archived": "BOOLEAN NOT NULL DEFAULT FALSE",
        "share_token": "VARCHAR(64)",
    }
    with engine.begin() as conn:
        inspector = inspect(conn)
        if "notes" not in inspector.get_table_names():
            return
        existing = {col["name"] for col in inspector.get_columns("notes")}
        for col_name, col_def in column_defs.items():
            if col_name not in existing:
                conn.execute(text(f"ALTER TABLE notes ADD COLUMN {col_name} {col_def}"))


@app.on_event("startup")
def on_startup() -> None:
    logger.info("Selected DB backend=%s target=%s", BACKEND, redact_connection_target(DATABASE_URL))
    Path(UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)
    ensure_notes_schema()


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
def list_notes(
    search: Optional[str] = None,
    tag: Optional[str] = None,
    include_archived: bool = False,
    db: Session = Depends(get_db),
):
    query = select(Note).order_by(Note.pinned.desc(), Note.id.desc())
    if search:
        term = f"%{search.strip()}%"
        query = query.where(or_(Note.title.like(term), Note.body.like(term), Note.tags_text.like(term)))
    if not include_archived:
        query = query.where(Note.archived.is_(False))
    notes = db.execute(query).scalars().all()
    if tag:
        wanted = tag.strip().lower()
        notes = [item for item in notes if wanted in parse_tags(item.tags_text)]
    return [to_note_out(item) for item in notes]


@app.post("/api/notes", response_model=NoteOut)
def create_note(note: NoteIn, db: Session = Depends(get_db)):
    new_note = Note(
        title=note.title,
        body=note.body,
        tags_text=serialize_tags(note.tags),
        pinned=note.pinned,
        archived=False,
    )
    db.add(new_note)
    db.commit()
    db.refresh(new_note)
    return to_note_out(new_note)


@app.post("/api/notes/{note_id}/attachments", response_model=AttachmentOut)
def upload_attachment(note_id: int, file: UploadFile = File(...), db: Session = Depends(get_db)):
    note = db.get(Note, note_id)
    if not note:
        raise HTTPException(status_code=404, detail="note not found")

    original_name = os.path.basename(file.filename or "")
    if not original_name:
        raise HTTPException(status_code=400, detail="filename is required")

    storage_name = f"{uuid4().hex}-{original_name}"
    destination = Path(UPLOAD_DIR) / storage_name
    size_bytes = 0
    try:
        with destination.open("wb") as out:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                size_bytes += len(chunk)
                if size_bytes > MAX_UPLOAD_SIZE:
                    raise HTTPException(status_code=413, detail=f"file too large (max {UPLOAD_MAX_SIZE_MB} MiB)")
                out.write(chunk)
    except HTTPException:
        if destination.exists():
            destination.unlink()
        raise
    finally:
        file.file.close()

    attachment = Attachment(
        note_id=note_id,
        filename=original_name,
        storage_name=storage_name,
        content_type=file.content_type or "application/octet-stream",
        size_bytes=size_bytes,
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
def download_attachment(attachment_id: int, inline: bool = False, db: Session = Depends(get_db)):
    item = db.get(Attachment, attachment_id)
    if not item:
        raise HTTPException(status_code=404, detail="attachment not found")
    path = Path(UPLOAD_DIR) / item.storage_name
    if not path.exists():
        raise HTTPException(status_code=404, detail="attachment file missing")
    if inline:
        return FileResponse(path, media_type=item.content_type or "application/octet-stream")
    return FileResponse(path, media_type=item.content_type or "application/octet-stream", filename=item.filename)


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
    record_note_history(db, existing, "update")
    existing.title = note.title
    existing.body = note.body
    existing.tags_text = serialize_tags(note.tags)
    existing.pinned = note.pinned
    db.commit()
    db.refresh(existing)
    return to_note_out(existing)


@app.post("/api/notes/{note_id}/archive", response_model=NoteOut)
def archive_note(note_id: int, db: Session = Depends(get_db)):
    existing = db.get(Note, note_id)
    if not existing:
        raise HTTPException(status_code=404, detail="not found")
    if not existing.archived:
        record_note_history(db, existing, "archive")
        existing.archived = True
        db.commit()
        db.refresh(existing)
    return to_note_out(existing)


@app.post("/api/notes/{note_id}/unarchive", response_model=NoteOut)
def unarchive_note(note_id: int, db: Session = Depends(get_db)):
    existing = db.get(Note, note_id)
    if not existing:
        raise HTTPException(status_code=404, detail="not found")
    if existing.archived:
        record_note_history(db, existing, "unarchive")
        existing.archived = False
        db.commit()
        db.refresh(existing)
    return to_note_out(existing)


@app.post("/api/notes/{note_id}/share", response_model=ShareOut)
def share_note(note_id: int, db: Session = Depends(get_db)):
    existing = db.get(Note, note_id)
    if not existing:
        raise HTTPException(status_code=404, detail="not found")
    if not existing.share_token:
        existing.share_token = uuid4().hex
        db.commit()
        db.refresh(existing)
    return ShareOut(note_id=existing.id, share_url=f"/share/{existing.share_token}")


@app.get("/api/notes/{note_id}/history", response_model=List[NoteHistoryOut])
def get_note_history(note_id: int, db: Session = Depends(get_db)):
    existing = db.get(Note, note_id)
    if not existing:
        raise HTTPException(status_code=404, detail="not found")
    entries = db.execute(select(NoteHistory).where(NoteHistory.note_id == note_id).order_by(NoteHistory.id.desc())).scalars().all()
    return [
        NoteHistoryOut(
            id=item.id,
            note_id=item.note_id,
            action=item.action,
            title=item.title,
            body=item.body,
            tags=parse_tags(item.tags_text),
            pinned=bool(item.pinned),
            archived=bool(item.archived),
            created_at=item.created_at,
        )
        for item in entries
    ]


@app.get("/api/share/{share_token}", response_model=NoteOut)
def get_shared_note(share_token: str, db: Session = Depends(get_db)):
    note = db.execute(select(Note).where(Note.share_token == share_token)).scalar_one_or_none()
    if not note:
        raise HTTPException(status_code=404, detail="share not found")
    return to_note_out(note)


@app.get("/share/{share_token}", response_class=HTMLResponse)
def share_note_page(share_token: str, db: Session = Depends(get_db)):
    note = db.execute(select(Note).where(Note.share_token == share_token)).scalar_one_or_none()
    if not note:
        raise HTTPException(status_code=404, detail="share not found")
    safe_title = html_escape(note.title)
    safe_body = html_escape(note.body).replace("\n", "<br>")
    safe_tags = ", ".join(parse_tags(note.tags_text)) or "none"
    return HTMLResponse(
        f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Shared Note</title>
<style>body{{font-family:system-ui,sans-serif;margin:0;background:#f5f7fb;color:#0f172a}}main{{max-width:760px;margin:40px auto;padding:0 16px}}article{{background:#fff;border:1px solid #d9e2ec;border-radius:14px;padding:20px;box-shadow:0 8px 20px rgba(15,23,42,.08)}}h1{{margin:0 0 6px}}.meta{{color:#4b5563;font-size:13px;margin-bottom:16px}}.body{{white-space:normal;line-height:1.5}}</style>
</head><body><main><article><h1>{safe_title}</h1><p class="meta">Tags: {html_escape(safe_tags)} | Shared read-only</p><div class="body">{safe_body}</div></article></main></body></html>"""
    )


@app.delete("/api/notes/{note_id}")
def delete_note(note_id: int, db: Session = Depends(get_db)):
    existing = db.get(Note, note_id)
    if not existing:
        raise HTTPException(status_code=404, detail="not found")
    if not existing.archived:
        record_note_history(db, existing, "archive")
        existing.archived = True
        db.commit()
        db.refresh(existing)
    return {"archived": note_id}


@app.delete("/api/notes/{note_id}/purge")
def purge_note(note_id: int, db: Session = Depends(get_db)):
    existing = db.get(Note, note_id)
    if not existing:
        raise HTTPException(status_code=404, detail="not found")
    attachments = db.execute(select(Attachment).where(Attachment.note_id == note_id)).scalars().all()
    for item in attachments:
        path = Path(UPLOAD_DIR) / item.storage_name
        if path.exists():
            path.unlink()
    db.execute(delete(Attachment).where(Attachment.note_id == note_id))
    db.execute(delete(NoteHistory).where(NoteHistory.note_id == note_id))
    db.delete(existing)
    db.commit()
    return {"deleted": note_id}
