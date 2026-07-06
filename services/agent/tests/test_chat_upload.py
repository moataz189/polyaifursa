"""/chat image upload: S3 key creation, filename handling, image_id + original
tracking, and the safe_image_name helper."""

import base64

import app as app_module
from s3 import safe_image_name
from tests.conftest import IMAGE_B64, _fake_agent_result


def test_chat_uploads_original_image_to_s3(client, monkeypatch):
    # An image in the request must be uploaded to S3, and only its key is kept;
    # the raw bytes never flow into the agent loop.
    uploaded = {}

    def fake_upload(key, data, content_type="image/jpeg"):
        uploaded["key"] = key
        uploaded["data"] = data
        return key

    monkeypatch.setattr(app_module, "upload_image", fake_upload)
    monkeypatch.setattr(app_module, "run_agent", lambda *args, **kwargs: _fake_agent_result())

    image_b64 = IMAGE_B64

    response = client.post(
        "/chat",
        json={
            "chat_id": "chat-xyz",
            "messages": [
                {
                    "role": "user",
                    "content": "What is in this image?",
                    "image_base64": image_b64,
                    "image_filename": "my_photo.png",
                }
            ],
        },
    )

    assert response.status_code == 200
    # Key follows <chat_id>/<prediction_id>/original/<image_name> and preserves
    # the original uploaded filename (never a hard-coded "image.jpg").
    assert "key" in uploaded
    parts = uploaded["key"].split("/")
    assert len(parts) == 4
    # The client-supplied chat_id is used, not a server-generated one.
    assert parts[0] == "chat-xyz"
    assert parts[2] == "original"
    assert parts[3] == "my_photo.png"
    # The uploaded bytes match the decoded image.
    assert uploaded["data"] == base64.b64decode(image_b64)


def test_chat_uploads_image_without_filename_uses_fallback(client, monkeypatch):
    # When no filename is supplied, a generated name is used (no hard-coded
    # "image.jpg" and no crash).
    uploaded = {}

    def fake_upload(key, data, content_type="image/jpeg"):
        uploaded["key"] = key
        return key

    monkeypatch.setattr(app_module, "upload_image", fake_upload)
    monkeypatch.setattr(app_module, "run_agent", lambda *args, **kwargs: _fake_agent_result())

    image_b64 = IMAGE_B64

    response = client.post(
        "/chat",
        json={
            "chat_id": "chat-abc",
            "messages": [
                {
                    "role": "user",
                    "content": "What is in this image?",
                    "image_base64": image_b64,
                }
            ],
        },
    )

    assert response.status_code == 200
    parts = uploaded["key"].split("/")
    assert parts[0] == "chat-abc"
    assert parts[2] == "original"
    # No filename -> fallback "<prediction_id>.jpg" (the prediction id is the
    # key's second segment). Never the old hard-coded "image.jpg".
    prediction_id = parts[1]
    assert parts[3] == f"{prediction_id}.jpg"
    assert parts[3] != "image.jpg"


def test_chat_uploads_only_newest_image(client, monkeypatch):
    # The conversation history carries an old image, but the latest user
    # message has a new one. Only the newest image must be uploaded.
    uploads = []

    def fake_upload(key, data, content_type="image/jpeg"):
        uploads.append(key)
        return key

    monkeypatch.setattr(app_module, "upload_image", fake_upload)
    monkeypatch.setattr(app_module, "run_agent", lambda *args, **kwargs: _fake_agent_result())

    image_b64 = IMAGE_B64

    response = client.post(
        "/chat",
        json={
            "chat_id": "chat-abc",
            "messages": [
                {
                    "role": "user",
                    "content": "old image",
                    "image_base64": image_b64,
                    "image_filename": "old.png",
                },
                {"role": "assistant", "content": "I saw the old image."},
                {
                    "role": "user",
                    "content": "new image",
                    "image_base64": image_b64,
                    "image_filename": "new.png",
                },
            ],
        },
    )

    assert response.status_code == 200
    # Exactly one upload, and it is the newest image (old.png is NOT re-uploaded).
    assert len(uploads) == 1
    assert uploads[0].endswith("/original/new.png")
    assert uploads[0].startswith("chat-abc/")


def test_chat_new_image_creates_image_id_and_original_key(client, monkeypatch):
    # Uploading a new image must generate an image_id, store the original under
    # <chat_id>/<image_id>/original/<filename>, seed the tools with that key and
    # image_id, and reset any prior prediction.
    seen = {}

    def fake_upload(key, data, content_type="image/jpeg"):
        seen["key"] = key
        return key

    def fake_run_agent(*args, **kwargs):
        seen["image_id"] = app_module._current_image_id.get()
        seen["image_key"] = app_module._current_image_s3_key.get()
        seen["original_key"] = app_module._original_image_s3_key.get()
        result = _fake_agent_result()
        # The real run_agent echoes the current image_id back.
        result["latest_image_id"] = app_module._current_image_id.get()
        result["latest_image_s3_key"] = app_module._current_image_s3_key.get()
        return result

    monkeypatch.setattr(app_module, "upload_image", fake_upload)
    monkeypatch.setattr(app_module, "run_agent", fake_run_agent)

    image_b64 = IMAGE_B64

    response = client.post(
        "/chat",
        json={
            "chat_id": "chat-xyz",
            "messages": [
                {
                    "role": "user",
                    "content": "here is an image",
                    "image_base64": image_b64,
                    "image_filename": "cat.png",
                }
            ],
        },
    )

    assert response.status_code == 200
    data = response.json()

    # The original image key follows <chat_id>/<image_id>/original/<filename>.
    parts = seen["key"].split("/")
    assert parts[0] == "chat-xyz"
    assert parts[2] == "original"
    assert parts[3] == "cat.png"
    image_id = parts[1]

    # The context is seeded with the same image_id and original key.
    assert seen["image_id"] == image_id
    assert seen["image_key"] == seen["key"]
    assert seen["original_key"] == seen["key"]

    # The image_id and key are remembered on the backend, not sent to the client.
    assert "latest_image_id" not in data
    assert app_module._chat_state["chat-xyz"]["latest_image_id"] == image_id
    assert app_module._chat_state["chat-xyz"]["latest_image_s3_key"] == seen["key"]


def test_safe_image_name_preserves_filename_and_extension():
    assert safe_image_name("photo.png") == "photo.png"
    assert safe_image_name("beatles.JPEG") == "beatles.JPEG"
    # Directory components are stripped so the name stays a single key segment.
    assert safe_image_name("../../etc/passwd") == "passwd"
    assert safe_image_name("/abs/path/cat.jpg") == "cat.jpg"


def test_safe_image_name_falls_back_when_missing():
    # No usable filename -> generated name, never the hard-coded "image.jpg".
    for value in [None, "", "   ", ".", ".."]:
        name = safe_image_name(value)
        assert name
        assert name != "image.jpg"
        assert "/" not in name
