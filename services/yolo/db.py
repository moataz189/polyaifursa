import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base

DB_BACKEND = os.environ.get("DB_BACKEND", "sqlite")

if DB_BACKEND == "postgres":
    DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://user:password@localhost/predictions")
else:
    DATABASE_URL = "sqlite:///predictions.db"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(bind=engine)
