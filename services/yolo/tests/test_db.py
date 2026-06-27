import tempfile
import os
from sqlalchemy import inspect
from db import init_db, Base
from models import PredictionSession, DetectionObject


def test_init_db_creates_tables():
    """Test that init_db creates all required tables"""
    # This test will use the default SQLite database
    # Since init_db uses the global engine from db.py, we need to verify
    # that the tables are created
    init_db()
    
    # Verify the tables exist by inspecting the engine
    from db import engine
    inspector = inspect(engine)
    table_names = inspector.get_table_names()
    
    assert "prediction_sessions" in table_names
    assert "detection_objects" in table_names


def test_postgres_database_url_configuration(monkeypatch):
    """Test that PostgreSQL DATABASE_URL is set when DB_BACKEND is postgres"""
    # Set environment variables for PostgreSQL
    monkeypatch.setenv("DB_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:password@localhost/predictions")
    
    # Reimport db module to pick up new environment variables
    import importlib
    import db as db_module
    importlib.reload(db_module)
    
    assert db_module.DATABASE_URL == "postgresql://user:password@localhost/predictions"


def test_sqlite_database_url_default(monkeypatch):
    """Test that SQLite DATABASE_URL is used by default"""
    # Clear the DB_BACKEND environment variable
    monkeypatch.delenv("DB_BACKEND", raising=False)
    
    # Reimport db module
    import importlib
    import db as db_module
    importlib.reload(db_module)
    
    assert "sqlite" in db_module.DATABASE_URL
