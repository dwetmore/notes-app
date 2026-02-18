import os
import logging
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Integer, String, create_engine, or_, select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Mapped, Session, declarative_base, mapped_column, sessionmaker

logger = logging.getLogger("notes_app")

DB_HOST = os.environ.get("DB_HOST")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "notes")
DB_USER = os.environ.get("DB_USER", "notes")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "notes")
DB_PATH = os.environ.get("DB_PATH")
DATABASE_URL_ENV = os.environ.get("DATABASE_URL")

if DB_HOST:
    DATABASE_URL = f"postgresql+psycopg://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    BACKEND = "postgres"
elif DATABASE_URL_ENV:
    DATABASE_URL = DATABASE_URL_ENV
    BACKEND = "postgres"
elif DB_PATH:
    DATABASE_URL = f"sqlite+pysqlite:///{DB_PATH}"
    BACKEND = "sqlite"
else:
    raise RuntimeError(
        "Database backend is not configured. Set DB_HOST or DATABASE_URL for Postgres, or set DB_PATH for SQLite."
    )


def redact_connection_target(database_url: str) -> str:
    try:
        parsed_url: URL = make_url(database_url)
        if parsed_url.password is None:
            return parsed_url.render_as_string(hide_password=False)
        return parsed_url.render_as_string(hide_password=True)
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


class NoteIn(BaseModel):
    title: str
    body: str


class NoteOut(NoteIn):
    id: int
    model_config = ConfigDict(from_attributes=True)


app = FastAPI(title="Notes App")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
Instrumentator().instrument(app).expose(app, include_in_schema=False)


@app.on_event("startup")
def on_startup() -> None:
    logger.info("Selected DB backend=%s target=%s", BACKEND, redact_connection_target(DATABASE_URL))
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
def readyz(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        return {"ready": True}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"{type(exc).__name__}: {exc}") from exc


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
    db.delete(existing)
    db.commit()
    return {"deleted": note_id}
