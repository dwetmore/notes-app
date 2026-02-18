import logging
import os
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
