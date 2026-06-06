import importlib


def test_default_confidence_threshold_when_env_missing(monkeypatch):
    monkeypatch.delenv("CONFIDENCE_THRESHOLD", raising=False)

    import app

    importlib.reload(app)

    assert app.CONFIDENCE_THRESHOLD == 0.5