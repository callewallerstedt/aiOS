import io

from PIL import Image

from app import server


def _jpeg_bytes():
    buffer = io.BytesIO()
    Image.new("RGB", (32, 24), "#55ccff").save(buffer, "JPEG")
    return buffer.getvalue()


def test_photo_drop_session_and_multiple_uploads(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PHOTO_DROP_ROOT", tmp_path)
    monkeypatch.setattr(server, "_lan_ip", lambda: "192.168.1.44")
    server.PHOTO_DROP_SESSIONS.clear()
    client = server.app.test_client()

    created = client.post("/api/photo-drop/session")
    assert created.status_code == 200
    session = created.get_json()
    assert session["url"].startswith("http://192.168.1.44:5000/photo-drop/")

    token = session["token"]
    page = client.get(f"/photo-drop/{token}")
    assert page.status_code == 200
    assert b"Take photo" in page.data

    for number in (1, 2):
        response = client.post(
            f"/api/photo-drop/{token}/upload",
            data={"image": (io.BytesIO(_jpeg_bytes()), f"camera-{number}.jpg")},
            content_type="multipart/form-data",
        )
        assert response.status_code == 200
        assert response.get_json()["count"] == number

    status = client.get(f"/api/photo-drop/{token}/status").get_json()
    assert status["count"] == 2
    files = list(server.Path(session["folder"]).glob("*.jpg"))
    assert len(files) == 2
    assert all(path.read_bytes().startswith(b"\xff\xd8") for path in files)


def test_photo_drop_rejects_remote_session_creation_and_non_image(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PHOTO_DROP_ROOT", tmp_path)
    server.PHOTO_DROP_SESSIONS.clear()
    client = server.app.test_client()

    forbidden = client.post(
        "/api/photo-drop/session",
        environ_overrides={"REMOTE_ADDR": "192.168.1.50"},
    )
    assert forbidden.status_code == 403

    session = client.post("/api/photo-drop/session").get_json()
    response = client.post(
        f"/api/photo-drop/{session['token']}/upload",
        data={"image": (io.BytesIO(b"definitely not an image"), "fake.jpg")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 415
    assert list(tmp_path.rglob("*.jpg")) == []
