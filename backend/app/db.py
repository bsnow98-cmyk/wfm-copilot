"""
SQLAlchemy 2.0 setup.

We expose:
- `engine`: the connection pool, created once per process.
- `SessionLocal`: a session factory.
- `get_db()`: a FastAPI dependency that yields a session and closes it.
"""
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

settings = get_settings()

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,    # checks connections before handing them out
    pool_size=5,
    max_overflow=10,
    future=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
    future=True,
)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency. Use as: `db: Session = Depends(get_db)`."""
    db = SessionLocal()
    try:
        yield db
    finally:
        # Roll back any transaction an exception left open — Session.close()
        # alone returns the connection to the pool mid-transaction.
        db.rollback()
        db.close()
