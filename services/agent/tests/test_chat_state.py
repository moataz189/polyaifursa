"""/chat backend image state: latest image key, prediction reset, and
original/current/processed key seeding kept in the per-chat_id store."""

import app as app_module
from tests.conftest import IMAGE_B64, _fake_agent_result


def test_chat_returns_new_image_key(client, monkeypatch):
    # When a new image is uploaded, its S3 key is remembered in the backend
    # chat state (keyed by chat_id) so follow-up requests can reuse it. The key
    # is NEVER sent to the frontend.
    captured = {}

    def fake_upload(key, data, content_type="image/jpeg"):
        captured["key"] = key
        return key

    def fake_run_agent(*args, **kwargs):
        # The real run_agent seeds latest_image_s3_key from the context var.
        result = _fake_agent_result()
        result["latest_image_s3_key"] = app_module._current_image_s3_key.get()
        return result

    monkeypatch.setattr(app_module, "upload_image", fake_upload)
    monkeypatch.setattr(app_module, "run_agent", fake_run_agent)

    image_b64 = IMAGE_B64

    response = client.post(
        "/chat",
        json={
            "chat_id": "chat-abc",
            "messages": [
                {
                    "role": "user",
                    "content": "here is an image",
                    "image_base64": image_b64,
                    "image_filename": "pic.png",
                }
            ],
        },
    )

    assert response.status_code == 200
    # The key is not exposed in the response, but is remembered on the backend.
    assert "latest_image_s3_key" not in response.json()
    assert app_module._chat_state["chat-abc"]["latest_image_s3_key"] == captured["key"]


def test_chat_new_image_takes_precedence_over_previous_key(client, monkeypatch):
    # If both a new upload and a previous key are present, the new upload wins.
    captured = {}

    def fake_upload(key, data, content_type="image/jpeg"):
        captured["key"] = key
        return key

    seen_key = {}

    def fake_run_agent(*args, **kwargs):
        seen_key["value"] = app_module._current_image_s3_key.get()
        result = _fake_agent_result()
        result["latest_image_s3_key"] = app_module._current_image_s3_key.get()
        return result

    monkeypatch.setattr(app_module, "upload_image", fake_upload)
    monkeypatch.setattr(app_module, "run_agent", fake_run_agent)

    # Seed backend state with an older image key that the new upload must win over.
    app_module._chat_state["chat-abc"] = {"latest_image_s3_key": "chat-abc/old/original/old.png"}

    image_b64 = IMAGE_B64

    response = client.post(
        "/chat",
        json={
            "chat_id": "chat-abc",
            "messages": [
                {
                    "role": "user",
                    "content": "here is a new image",
                    "image_base64": image_b64,
                    "image_filename": "new.png",
                }
            ],
        },
    )

    assert response.status_code == 200
    assert seen_key["value"] == captured["key"]
    stored = app_module._chat_state["chat-abc"]["latest_image_s3_key"]
    assert stored == captured["key"]
    assert stored != "chat-abc/old/original/old.png"


def test_chat_no_image_and_no_previous_key(client, monkeypatch):
    # Pure text conversation with no image anywhere: the remembered image key
    # stays null on the backend.
    monkeypatch.setattr(app_module, "run_agent", lambda *args, **kwargs: _fake_agent_result())

    response = client.post(
        "/chat",
        json={
            "chat_id": "chat-abc",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert response.status_code == 200
    assert app_module._chat_state["chat-abc"]["latest_image_s3_key"] is None


def test_chat_new_image_resets_previous_prediction_id(client, monkeypatch):
    # Uploading a new image must drop any carried-over prediction id (it belongs
    # to an OLDER image). The agent should be seeded with prediction_id = None.
    seen = {}

    def fake_run_agent(*args, **kwargs):
        seen["prediction"] = app_module._latest_prediction_uid.get()
        seen["image_key"] = app_module._current_image_s3_key.get()
        return _fake_agent_result()

    monkeypatch.setattr(
        app_module, "upload_image", lambda key, data, content_type="image/jpeg": key
    )
    monkeypatch.setattr(app_module, "run_agent", fake_run_agent)

    image_b64 = IMAGE_B64

    response = client.post(
        "/chat",
        json={
            "chat_id": "chat-abc",
            "messages": [
                {
                    "role": "user",
                    "content": "here is a new image",
                    "image_base64": image_b64,
                    "image_filename": "new.png",
                }
            ],
            # Stale state from an older image that must be discarded.
            "latest_prediction_id": "old-pred",
            "latest_image_s3_key": "chat-abc/old/original/old.png",
        },
    )

    assert response.status_code == 200
    # The old prediction was dropped; the tools see no detection for the new image.
    assert seen["prediction"] is None
    # The new upload is the image in play, not the old key.
    assert seen["image_key"].endswith("/original/new.png")


def test_chat_seeds_original_key_from_request_not_latest(client, monkeypatch):
    # A follow-up request after processing: the remembered latest_image_s3_key
    # points at the processed image, but original_image_s3_key must be seeded
    # from the remembered original key, NOT derived from latest_image_s3_key.
    seen = {}

    def fake_run_agent(*args, **kwargs):
        seen["original"] = app_module._original_image_s3_key.get()
        seen["latest"] = app_module._current_image_s3_key.get()
        result = _fake_agent_result()
        result["original_image_s3_key"] = app_module._original_image_s3_key.get()
        result["latest_image_s3_key"] = app_module._current_image_s3_key.get()
        return result

    monkeypatch.setattr(
        app_module,
        "upload_image",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not upload")),
    )
    monkeypatch.setattr(app_module, "run_agent", fake_run_agent)

    processed_key = "chat-abc/img-7/processed/add_noise_pic.png"
    original_key = "chat-abc/img-7/original/pic.png"
    # Seed backend state as if processing already happened in an earlier request.
    app_module._chat_state["chat-abc"] = {
        "latest_image_s3_key": processed_key,
        "latest_image_id": "img-7",
        "original_image_s3_key": original_key,
    }

    response = client.post(
        "/chat",
        json={
            "chat_id": "chat-abc",
            "messages": [{"role": "user", "content": "detect the original image"}],
        },
    )

    assert response.status_code == 200
    # The original key comes from stored state, not the processed latest key.
    assert seen["original"] == original_key
    assert seen["latest"] == processed_key
    assert app_module._chat_state["chat-abc"]["original_image_s3_key"] == original_key
