"""/chat follow-up requests: reusing the remembered image without re-uploading."""

import app as app_module
from tests.conftest import IMAGE_B64, _fake_agent_result


def test_chat_text_only_follow_up_uploads_nothing(client, monkeypatch):
    # A text-only latest message must not trigger any upload, even if an earlier
    # message had an image.
    uploads = []
    monkeypatch.setattr(
        app_module, "upload_image", lambda key, data, content_type="image/jpeg": uploads.append(key)
    )
    monkeypatch.setattr(app_module, "run_agent", lambda *args, **kwargs: _fake_agent_result())

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
                },
                {"role": "assistant", "content": "Got it."},
                {"role": "user", "content": "tell me more"},
            ],
        },
    )

    assert response.status_code == 200
    assert uploads == []


def test_chat_reuses_previous_image_key(client, monkeypatch):
    # A follow-up request without a new image reuses the image key remembered
    # for this chat_id on the backend, and sets it as the current image.
    seen_key = {}

    def fake_run_agent(*args, **kwargs):
        seen_key["value"] = app_module._current_image_s3_key.get()
        result = _fake_agent_result()
        result["latest_image_s3_key"] = app_module._current_image_s3_key.get()
        return result

    # No new image means no upload should ever happen.
    monkeypatch.setattr(
        app_module,
        "upload_image",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not upload")),
    )
    monkeypatch.setattr(app_module, "run_agent", fake_run_agent)

    prior_key = "chat-abc/pred-1/original/pic.png"
    # Seed the backend state as if an earlier request had remembered this key.
    app_module._chat_state["chat-abc"] = {"latest_image_s3_key": prior_key}

    response = client.post(
        "/chat",
        json={
            "chat_id": "chat-abc",
            "messages": [{"role": "user", "content": "rotate the previous image 90 degrees"}],
        },
    )

    assert response.status_code == 200
    # The tools saw the remembered key, and it stays remembered on the backend.
    assert seen_key["value"] == prior_key
    assert app_module._chat_state["chat-abc"]["latest_image_s3_key"] == prior_key


def test_chat_reuses_previous_image_id(client, monkeypatch):
    # A follow-up request without a new image carries over the image_id
    # remembered on the backend and seeds the tools with it (same image flow).
    seen = {}

    def fake_run_agent(*args, **kwargs):
        seen["image_id"] = app_module._current_image_id.get()
        result = _fake_agent_result()
        result["latest_image_id"] = app_module._current_image_id.get()
        return result

    monkeypatch.setattr(
        app_module,
        "upload_image",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not upload")),
    )
    monkeypatch.setattr(app_module, "run_agent", fake_run_agent)

    # Seed backend state as if an earlier request remembered this image flow.
    app_module._chat_state["chat-abc"] = {
        "latest_image_s3_key": "chat-abc/img-7/original/pic.png",
        "latest_image_id": "img-7",
    }

    response = client.post(
        "/chat",
        json={
            "chat_id": "chat-abc",
            "messages": [{"role": "user", "content": "rotate the previous image"}],
        },
    )

    assert response.status_code == 200
    assert seen["image_id"] == "img-7"
    assert app_module._chat_state["chat-abc"]["latest_image_id"] == "img-7"
