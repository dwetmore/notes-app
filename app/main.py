import logging
import os
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Integer, String, create_engine, or_, select, text
from sqlalchemy.orm import Mapped, Session, declarative_base, mapped_column, sessionmaker

logger = logging.getLogger(__name__)


def env_value(*keys: str, default: Optional[str] = None) -> Optional[str]:
    for key in keys:
        value = os.environ.get(key)
        if value:
            return value
    return default


def redact_connection_target(database_url: str) -> str:
    if "@" not in database_url:
        return database_url
    prefix, suffix = database_url.split("@", 1)
    if "://" in prefix:
        scheme, _ = prefix.split("://", 1)
        return f"{scheme}://***@{suffix}"
    return f"***@{suffix}"


POSTGRES_HOST = env_value("POSTGRES_HOST")
POSTGRES_PORT = env_value("POSTGRES_PORT", default="5432")
POSTGRES_DB = env_value("POSTGRES_DB", default="notes")
POSTGRES_USER = env_value("POSTGRES_USER", default="notes")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD") or os.environ.get("DB_PASSWORD", "notes")

if env_value("DATABASE_URL"):
    DATABASE_URL = env_value("DATABASE_URL")
    if DATABASE_URL.startswith("postgresql"):
        BACKEND = "postgres"
    elif DATABASE_URL.startswith("sqlite"):
        BACKEND = "sqlite"
    else:
        BACKEND = "database_url"
elif POSTGRES_HOST:
    BACKEND = "postgres"
    DATABASE_URL = f"postgresql+psycopg://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
else:
    BACKEND = "sqlite"
    DATABASE_URL = "sqlite+pysqlite:///./notes.db"
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
    Base.metadata.create_all(bind=engine)
    logger.info("Starting Notes App with backend=%s target=%s", BACKEND, redact_connection_target(DATABASE_URL))


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
        raise HTTPException(status_code=503, detail=f"{BACKEND} readiness failed: {exc}")


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
