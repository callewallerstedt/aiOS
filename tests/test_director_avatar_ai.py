"""Offline tests for the Director AI avatar generation endpoint.

The phone PWA sends a prompt to POST /api/avatar/generate; the server calls
OpenAI's GPT Image 2 and returns the image as a data URL that becomes the
agent's avatar. These tests pin the contract without touching the network.
"""
import asyncio
import json

import pytest


@pytest.fixture()
def director(tmp_path, monkeypatch):
    """A Director package pointed at a throwaway home directory."""
    monkeypatch.setenv("AIOS_DIRECTOR_HOME", str(tmp_path / "home"))
    import director.config as config
    import director.store as store
    store.close()
    config.load_settings(refresh=True)
    yield store
    store.close()


class _FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, content_type=None):
        return self._payload


class _FakeSession:
    def __init__(self, response):
        self._response = response
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, json=None, headers=None):
        self.calls.append((url, json, headers))
        return self._response


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def test_avatar_generate_requires_a_prompt(director):
    from director import server

    async def run():
        resp = await server.generate_avatar(_FakeRequest({}))
        return resp.status

    assert asyncio.run(run()) == 400


def test_avatar_generate_errors_without_an_openai_key(director, monkeypatch):
    from director import server

    monkeypatch.setattr(server.config, "openai_key", lambda settings=None: "")

    async def run():
        resp = await server.generate_avatar(_FakeRequest({"prompt": "a robot"}))
        return resp.status

    assert asyncio.run(run()) == 503


def test_avatar_generate_calls_openai_and_returns_a_data_url(director, monkeypatch):
    import aiohttp

    from director import server

    monkeypatch.setattr(server.config, "openai_key", lambda settings=None: "sk-test")
    fake = _FakeSession(_FakeResponse(200, {"data": [{"b64_json": "QUJD"}]}))
    monkeypatch.setattr(aiohttp, "ClientSession", lambda timeout=None: fake)

    async def run():
        resp = await server.generate_avatar(_FakeRequest({"prompt": "a robot"}))
        body = json.loads(resp.text)
        return resp.status, body, fake.calls

    status, body, calls = asyncio.run(run())

    assert status == 200
    assert body["ok"] is True
    assert body["avatar"] == "data:image/png;base64,QUJD"

    url, payload, headers = calls[0]
    assert url == "https://api.openai.com/v1/images/generations"
    assert payload["model"] == "gpt-image-2"
    assert payload["prompt"] == "a robot"
    assert headers["Authorization"] == "Bearer sk-test"


def test_avatar_generate_surfaces_openai_errors(director, monkeypatch):
    import aiohttp

    from director import server

    monkeypatch.setattr(server.config, "openai_key", lambda settings=None: "sk-test")
    fake = _FakeSession(_FakeResponse(401, {"error": {"message": "bad key"}}))
    monkeypatch.setattr(aiohttp, "ClientSession", lambda timeout=None: fake)

    async def run():
        resp = await server.generate_avatar(_FakeRequest({"prompt": "a robot"}))
        body = json.loads(resp.text)
        return resp.status, body

    status, body = asyncio.run(run())
    assert status == 502
    assert "bad key" in body["error"]