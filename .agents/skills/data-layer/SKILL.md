---
name: data-layer
description: |
  Use this skill for ANY task that reads or writes YOLO prediction data in services/yolo/ —
  even if the user doesn't mention SQLAlchemy. Trigger on: "add an endpoint", "add a column",
  "add a table", "modify an endpoint", "delete a prediction", "filter by", "recent sessions",
  "store/persist/query predictions", "write tests for the API", or "the database layer".
  Examples: "Add GET /predictions/recent", "Create an endpoint that filters by label",
  "Delete the /predictions/all endpoint".
  Covers: endpoint implementation, SQLAlchemy models, database access, ORM query patterns,
  test isolation, and verification.
  Do NOT use for: UI/frontend code, agent/chat logic, or anything that doesn't touch the
  prediction database.

---

# YOLO API Data Layer

> **Read this entire file before touching code in `services/yolo/`.** It contains the schema,
> the exact file layout, ORM query recipes, test-isolation rules, and the hard constraints that
> keep the API contract stable. Prefer what is written here over your own judgment.

## Quick Start: Adding a New Endpoint

**Follow these steps in order:**

1. **Decide what data the endpoint needs** — Read from `prediction_sessions`? `detection_objects`? Both?
2. **Add or update SQLAlchemy models** — If needed, add columns/relationships to `models.py`
3. **Implement the endpoint** — Use `Depends(get_db)` to inject a session; write the SQLAlchemy query
4. **Write tests** — Call `setup_db()` at the start of each test; use `TestClient` to verify behavior
5. **Run the test suite** — Ensure coverage stays ≥95% and all tests pass
Coverage is required only for `services/yolo/app.py`.

6. **Stop and verify** — run evals/tests/coverage, update `verification-report.md`, then only return the final response.

**Do not merge** an endpoint until all six steps are complete.

---

## Overview
`services/yolo/app.py` currently uses raw `sqlite3` with `init_db()` and inline SQL. Migrate it to SQLAlchemy ORM with a configurable backend. Two principles govern this skill:

- **Backend is configuration, not logic.** SQLite for local dev, Postgres for production, same code path.
- **Schema and config live in separate files.** `models.py` describes the schema; `db.py` wires up the engine and sessions.

Any prediction-related endpoint change is a full feature task: update the endpoint, affected data access code, corresponding tests, and the verification report before finishing.
## Required Files

| File | Owns (and nothing else) |
|---|---|
| `services/yolo/models.py` | `Base = declarative_base()`, the `PredictionSession` and `DetectionObject` models, columns, table names, relationships/cascade rules. **No** engine, `SessionLocal`, `get_db`, `init_db`, or `DATABASE_URL`. |
| `services/yolo/db.py` | `DATABASE_URL` (from env vars), `create_engine(...)`, `SessionLocal`, `get_db()`, `init_db()`. Imports `Base` from `models.py`. **No** ORM model definitions. |

`app.py` imports models from `models.py` and `get_db` / `init_db` from `db.py`.

|
## When to Use This Skill

**✓ Use this skill when:**
- User asks to add a new endpoint (e.g., "Add GET /predictions/recent")
- User asks to modify an existing endpoint's behavior
- User asks to delete an endpoint
- Any of the above reads or writes prediction data

**✗ Do NOT use this skill for:**
- Frontend/UI code changes
- Agent logic or chat endpoints
- Non-database operations (e.g., image processing, YOLO model loading)
- Configuration or deployment

---

## Endpoint Implementation Rule

**If the task adds or modifies an endpoint that reads/writes prediction data:**

**Do this:**
1. If the code still uses raw `sqlite3.connect()`, migrate to SQLAlchemy first
2. Add the endpoint using `Depends(get_db)` to inject a session
3. Write tests; ensure coverage stays ≥95%,Coverage is required only for `services/yolo/app.py`.


**Never do this:**
- Add another `sqlite3.connect()` call to extend the legacy code
- Change response shapes or status codes
- Skip tests
- Leave coverage below 95%

**Why?** Students need to see how SQLAlchemy works end-to-end. Mixing `sqlite3` and SQLAlchemy teaches bad patterns.

## Backward Compatibility Rule

Response shapes, field names, and status codes are a **contract**. Existing tests and clients depend on them.

**✓ DO:**
- Keep the same JSON structure
- Use the same field names (`box`, not `bbox`)
- Keep the same status codes

**✗ DO NOT:**
- Rename fields
- Add new required fields
- Change HTTP status codes
- Reshape nested objects
- "Improve" the API while migrating

**Why?** Tests verify the old behavior. Changing it breaks those tests and the existing client code. API cleanup is a separate task with its own tests.

### Schema to Preserve

| Table | Columns |
|---|---|
| `prediction_sessions` | `uid` (PK, str), `timestamp`, `original_image`, `predicted_image` |
| `detection_objects` | `id` (PK, autoincrement), `prediction_uid` (FK → `prediction_sessions.uid`), `label`, `score`, `box` (stored as `str(box)`) |


## Core Pattern: How to Write an Endpoint

Every endpoint that reads or writes prediction data follows this pattern:

```python
# ✅ The pattern all endpoints must follow:
@app.get("/prediction/{uid}")
def get_prediction(uid: str, db: Session = Depends(get_db)):
    # 1. Query using SQLAlchemy
    session = db.get(PredictionSession, uid)
    
    # 2. Handle not found
    if session is None:
        raise HTTPException(status_code=404, detail="Prediction not found")
    
    # 3. Build and return the response (keep the same shape as before migration)
    return {
        "uid": session.uid,
        "timestamp": session.timestamp.isoformat(),
        "original_image": session.original_image,
        "predicted_image": session.predicted_image,
        "detection_objects": [
            {
                "id": obj.id,
                "label": obj.label,
                "score": obj.score,
                "box": obj.box,
            }
            for obj in session.detection_objects
        ]
    }
```

**Key points:**
- Always inject a session: `db: Session = Depends(get_db)`
- Query using SQLAlchemy ORM, not raw SQL
- Respond with the **same JSON shape** as the existing API (backward compatibility)
- Return appropriate status codes (200, 404, etc.)
- Handle errors gracefully

## ORM Query Recipes

Use these as building blocks. They match the models in `models.py` (`PredictionSession`,
`DetectionObject`). Note `box` is stored and returned as a **raw string** — never parse it.

**INSERT (session + its detections):**
```python
session = PredictionSession(uid=uid, original_image=orig, predicted_image=pred)
db.add(session)
db.flush()  # satisfies the FK before adding detection rows
for det in detections:
    db.add(DetectionObject(
        prediction_uid=uid, label=det.label, score=det.score, box=str(det.box),
    ))
db.commit()
```

**SELECT one by uid (404 if missing):**
```python
session = db.get(PredictionSession, uid)
if session is None:
    raise HTTPException(status_code=404, detail="Prediction not found")
```

**SELECT N most recent:**
```python
sessions = (
    db.query(PredictionSession)
    .order_by(PredictionSession.timestamp.desc())
    .limit(10)
    .all()
)
```

**JOIN — sessions that contain a given label:**
```python
sessions = (
    db.query(PredictionSession)
    .join(DetectionObject, PredictionSession.uid == DetectionObject.prediction_uid)
    .filter(DetectionObject.label == label)
    .order_by(PredictionSession.timestamp)
    .all()
)
```

**FILTER detections by score:**
```python
objects = (
    db.query(DetectionObject)
    .filter(DetectionObject.score >= min_score)
    .order_by(DetectionObject.id)
    .all()
)
```

> **`box` rule:** store as `str(bbox)`, return as-is. Do **not** call `ast.literal_eval` unless
> the task explicitly asks for it — tests assert string equality (e.g. `box == "[10,20,100,200]"`).

## File Layout

```python
# services/yolo/models.py — ORM models only, no engine or sessions
from sqlalchemy import Column, String, Integer, Float, ForeignKey
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class PredictionSession(Base):
    __tablename__ = "prediction_sessions"

    uid = Column(String, primary_key=True)
    timestamp = Column(DateTime, server_default=func.now(), nullable=False)
    original_image = Column(String)
    predicted_image = Column(String)

    detection_objects = relationship(
        "DetectionObject",
        back_populates="session",
        cascade="all, delete-orphan",
    )


class DetectionObject(Base):
    __tablename__ = "detection_objects"

    id = Column(Integer, primary_key=True, autoincrement=True)
    prediction_uid = Column(String, ForeignKey("prediction_sessions.uid"))
    label = Column(String)
    score = Column(Float)
    box = Column(String)

    session = relationship("PredictionSession", back_populates="detection_objects")
```

```python
# services/yolo/db.py — engine/session config only; imports Base from models.py
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base

DB_BACKEND = os.environ.get("DB_BACKEND", "sqlite")

if DB_BACKEND == "postgres":
    DATABASE_URL = os.environ["DATABASE_URL"]
else:
    DATABASE_URL = "sqlite:///predictions.db"

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(bind=engine)
```

## Changing the Schema

### Adding a column
1. Add the `Column(...)` to the model in `models.py`.
2. Delete the local `predictions.db` so `init_db()` rebuilds it (no migrations in this project).
3. Update the writing endpoint (e.g. `/predict`) to populate the new column.
4. Update the seed data in tests if any test needs the new column.

### Adding a table
1. Add a new class inheriting `Base` in `models.py` (with a `__tablename__`).
2. `init_db()` / `Base.metadata.create_all` picks it up automatically — no extra wiring.
3. Add the new endpoint with `db: Session = Depends(get_db)`.

### Test Update Rule

Every change that affects endpoint behavior, database access, models, response-building logic, or persistence must be validated by automated tests.

For refactoring tasks:

- First determine whether the existing tests fully cover the refactored code.
- If they do not, update or add tests in `services/yolo/tests/`.
- If they already provide sufficient coverage, keep them unchanged but explicitly state which existing test files and test cases verify the refactored behavior.

Do not complete a refactoring task without demonstrating that the modified code is covered by automated tests.

Coverage for modified modules must remain at or above 95%.
## Testing

- All tests live in `services/yolo/tests/`. Update existing test files instead of creating duplicates.
- Use FastAPI `TestClient` for HTTP-level tests.
- Use a temporary SQLite database per test/module; **never** use or modify the real `predictions.db`.
- Override the session with `app.dependency_overrides[get_db]` and create tables via `Base.metadata.create_all(bind=engine)`.
- Mock the YOLO model and other external dependencies; never load the real model.
- Keep coverage **≥ 95%** for modified modules. If coverage drops below 95%, add tests for the uncovered branches until it passes.

**Every test that uses the database must define and call its own `setup_db()` helper inside the test file. Do not import `setup_db` from `conftest.py`, and do not create shared pytest fixtures for database setup.**

### setup_db() Helper Function

Add this function to every test file that needs database access:

```python
import tempfile

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app import app
from db import get_db
from models import Base


def setup_db():
    _, db_path = tempfile.mkstemp(suffix=".db")
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    TestSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = TestSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app), TestSessionLocal
```

### Example: Using setup_db() in a Test

### Example: Self-Contained Database Test

```python
import tempfile

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app import app
from db import get_db
from models import Base, PredictionSession


def setup_db():
    _, db_path = tempfile.mkstemp(suffix=".db")

    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )

    TestSessionLocal = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
    )

    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = TestSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    return TestClient(app), TestSessionLocal


def test_get_prediction_by_uid():
    client, session_local = setup_db()

    with session_local() as db:
        db.add(
            PredictionSession(
                uid="prediction-1",
                original_image="original.jpg",
                predicted_image="predicted.jpg",
            )
        )
        db.commit()

    response = client.get("/prediction/prediction-1")

    assert response.status_code == 200
    assert response.json()["uid"] == "prediction-1"
```




## Common Mistakes (Avoid These)

| Mistake | Correct Way |
|---------|------------|
| Putting models in `db.py` | Import `Base` from `models.py`; put all model classes there |
| Putting `engine`, `SessionLocal`, or `get_db` in `models.py` | Put those in `db.py` only |
| Declaring a second `Base = declarative_base()` in `db.py` | Import it: `from models import Base` |
| Still using `sqlite3.connect()` in the endpoint | Use `Depends(get_db)` to inject a session |
| Opening a session with `SessionLocal()` inside the endpoint | Always use `Depends(get_db)`; let FastAPI manage the lifecycle |
| Hard-coding the SQLite path (e.g., `"predictions.db"`) | Use `DATABASE_URL` from `db.py`; configure via env vars |
| Changing response shape or field names | Keep the JSON structure exactly the same |
| Changing HTTP status codes | Preserve existing codes (200, 404, etc.) |
| Forgetting to call `setup_db()` in tests | Every database test needs a fresh client and session |
| Using pytest fixtures for database setup | Call `setup_db()` directly in each test function |
| Leaving orphaned `DetectionObject` rows | Use `cascade="all, delete-orphan"` in the relationship |
| Mixing query logic with response building | Separate them: query first, then serialize |
| Test coverage below 95% | Run `pytest --cov` and add tests for missing branches |
| Parsing `box` with `ast.literal_eval` | Keep it a raw string; tests assert string equality |
| Importing `setup_db` from `conftest.py` | Define `setup_db()` directly inside each database test file |

## Hard Rules — treat a violation as a build failure

| Rule | Detail |
|------|--------|
| No `import sqlite3` | Anywhere in `app.py`, `db.py`, or `models.py` |
| No raw SQL strings | No `SELECT`/`INSERT`/`CREATE TABLE` text in `app.py` |
| `Depends(get_db)` everywhere | Every endpoint that reads or writes the DB injects the session |
| ORM models live in `models.py` only | No engine/`SessionLocal`/`get_db` there |
| Engine/session config in `db.py` only | No model classes there; import `Base` from `models.py` |
| `box` stays a raw string | Store `str(bbox)`, return as-is |
| API contract unchanged | Same paths, status codes, field names, and response shapes after any change |
| Tests define `setup_db()` locally | Do not import `setup_db` from `conftest.py`; each DB test file must contain its own helper |

## Verification Commands

Run these after every change — the greps must return **empty**, the tests must all pass:

```bash
cd services/yolo

# Must return nothing (no legacy sqlite3 left behind)
grep -rn "import sqlite3" app.py db.py models.py
grep -rn "sqlite3.connect" app.py

# Every DB endpoint injects a session
grep -n "Depends(get_db)" app.py

# Full suite + coverage for changed modules (keep >= 95%)
pytest --tb=short -q
pytest --cov=app  --cov-report=term-missing
```
Coverage is required only for `services/yolo/app.py`.


If any grep returns unexpected output, or any test fails, fix it before marking the task done.
`--cov-report=term-missing` lists the uncovered line numbers — add tests for those branches until each modified module is at or above 95%.

## Checklist Before Submitting

**Before you consider the task done, verify ALL of these:**

- [ ] Endpoint code uses `Depends(get_db)` to inject a session
- [ ] No `sqlite3.connect()` calls in the endpoint
- [ ] Models added/updated in `models.py` (if needed)
- [ ] `db.py` unchanged (only configuration lives there)
- [ ] Response JSON shape matches the existing API (no renamed fields, no new required fields)
- [ ] HTTP status codes unchanged (200, 404, etc.)
- [ ] Test file created or updated with new test cases
- [ ] Each test calls `setup_db()` at the start
- [ ] All tests pass: `pytest services/yolo/tests`
- [ ] Coverage ≥95% for modified modules: `pytest --cov=app --cov-report=term-missing`
- [ ] Verification report updated (see below)

**If any item is unchecked, the task is not done.** Go back and fix it before submitting.



## Mandatory Evals
After every code change, before producing the final response, you must:
1. Run the required evals.
2. Run all relevant pytest tests.
3. Confirm that endpoint behavior has not changed unless explicitly requested.
4. Update `verification-report.md`.
5. Include a summary of the eval and test results in the final response.

Do not consider the task complete until all of the above have been completed successfully.
## Mandatory Verification Report

Every task that changes data-layer code must end by creating/updating `.agents/skills/data-layer/evals/verification-report.md`. The task is not finished until this report exists and reflects the current run.

To build it:

1. **Match the task to an eval** in `evals/evals.json` and record the closest `id` (or `none` if nothing matches).
2. **Build the checklist** from the eval's `expected_output` plus the base items below; add eval-specific items when relevant (e.g. "GET /predictions/recent orders by timestamp desc, limit 10").
3. **Run the verification commands** (at minimum `pytest services/yolo/tests`) and record them and their results.
4. **Mark each item PASS/FAIL from actual evidence.** Only check `[x]` for verified items.
5. **Final result is `PASS` only when every item passes**; a single failure makes the whole report `FAIL`. Never claim PASS without re-running and confirming.

### Report template

```markdown
# Data Layer Skill Verification

## Matched Eval
Eval ID: <id>

## Task Summary
<one or two sentences describing the prompt/task>

## Checklist

- [ ] models.py exists
- [ ] db.py exists
- [ ] endpoints use Depends(get_db)
- [ ] no sqlite3.connect in API code
- [ ] response shapes unchanged
- [ ] tests added or updated
- [ ] pytest passed
<add eval-specific items here>

## Commands Run

- `pytest services/yolo/tests` → <passed / failed, summary>
- <other commands run>

## Failures / Remaining Work

<for each FAIL item: what failed and what still needs fixing; write "None" if everything passed>

## Final Result

PASS / FAIL
```

Include a short summary of the report (matched eval, final result, any failures) in your final response.

---