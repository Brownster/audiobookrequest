from contextlib import contextmanager
from urllib.parse import quote_plus

from sqlalchemy import create_engine
from sqlmodel import Session, text

from app.internal.env_settings import Settings

db = Settings().db
if db.use_postgres:
    # URL-encode credentials to prevent injection via special characters
    pg_user = quote_plus(db.postgres_user)
    pg_password = quote_plus(db.postgres_password)
    pg_host = db.postgres_host
    pg_port = db.postgres_port
    pg_db = quote_plus(db.postgres_db)
    pg_ssl = db.postgres_ssl_mode
    engine = create_engine(
        f"postgresql://{pg_user}:{pg_password}@{pg_host}:{pg_port}/{pg_db}?sslmode={pg_ssl}"
    )
else:
    sqlite_path = Settings().get_sqlite_path()
    engine = create_engine(f"sqlite+pysqlite:///{sqlite_path}")


def get_session():
    with Session(engine) as session:
        if not Settings().db.use_postgres:
            session.execute(text("PRAGMA foreign_keys=ON"))  # pyright: ignore[reportDeprecated]
        yield session


# TODO: couldn't get a single function to work with FastAPI and allow for session creation wherever
@contextmanager
def open_session():
    with Session(engine) as session:
        if not Settings().db.use_postgres:
            session.execute(text("PRAGMA foreign_keys=ON"))  # pyright: ignore[reportDeprecated]
        yield session
