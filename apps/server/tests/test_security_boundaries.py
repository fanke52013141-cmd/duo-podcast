"""API authentication, trusted-host and provider network-boundary regressions."""
import asyncio
from dataclasses import replace
import socket
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from duocast import main
from duocast.adapters.toapis_image import ToAPIsImageProvider, _NoRedirect
from duocast.adapters.toapis_text import ToAPIsTextProvider
from duocast.core.config import settings
from duocast.services.runtime_config import (
    effective,
    validate_provider_url,
    validate_public_download_url,
)
from duocast.storage.machine import MachineSettings


def _settings(tmp_path, **overrides):
    values = dict(
        storage_root=tmp_path,
        host="127.0.0.1",
        api_token="",
        text_provider="mock",
        image_provider="mock",
        tts_provider="mock",
        video_provider="mock",
    )
    values.update(overrides)
    return replace(settings, **values)


def _dns(address):
    return lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
    ]


def test_non_loopback_without_token_refuses_startup(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "settings", _settings(tmp_path, host="0.0.0.0"))
    with pytest.raises(RuntimeError, match="DUOCAST_API_TOKEN"):
        with TestClient(main.app):
            pass


def test_non_loopback_with_token_keeps_testclient_usable(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "settings", _settings(tmp_path, host="0.0.0.0", api_token="secret"))
    with TestClient(main.app) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/projects").status_code == 401
        assert client.get("/api/projects", headers={"Authorization": "Bearer secret"}).status_code == 200


def test_global_gate_allows_bearer_and_same_origin_but_not_cross_origin(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "settings", _settings(tmp_path, api_token="secret"))
    with TestClient(main.app) as client:
        assert client.get("/api/projects").status_code == 401
        assert client.get("/api/projects", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.get("/api/projects", headers={"Authorization": "Bearer secret"}).status_code == 200
        assert client.get("/api/projects", headers={"Origin": "http://testserver"}).status_code == 200
        assert client.get("/api/projects", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
        assert client.get("/api/projects", headers={"Origin": "http://evil.example"}).status_code == 401
        created = client.post("/api/projects", json={"title": "same-origin"}, headers={"Origin": "http://testserver"})
        assert created.status_code == 200
        assert client.post("/api/projects", json={}, headers={"Origin": "http://evil.example"}).status_code == 401
        assert client.get("/api/health").status_code == 200


def test_trusted_host_rejects_dns_rebinding_host(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "settings", _settings(tmp_path, api_token="secret"))
    with TestClient(main.app) as client:
        response = client.get("/api/projects", headers={"Host": "evil.example", "Authorization": "Bearer secret"})
        assert response.status_code == 400


def test_ingest_never_uses_same_origin_bypass(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "settings", _settings(tmp_path, api_token="secret"))
    with TestClient(main.app) as client:
        project = client.post("/api/projects", json={}, headers={"Authorization": "Bearer secret"}).json()
        envelope = {
            "schemaVersion": "1.0",
            "kind": "script",
            "projectId": project["id"],
            "payload": {"turns": [{"speaker": "A", "lines": [{"displayText": "x"}]}]},
        }
        assert client.post("/api/ingest/script", json=envelope, headers={"Origin": "http://testserver"}).status_code == 401
        assert client.post("/api/ingest/script", json=envelope, headers={"Authorization": "Bearer secret"}).status_code == 200


def test_configured_trusted_hosts_are_normalized(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "settings", _settings(tmp_path, host="0.0.0.0", trusted_hosts=" Example.COM , [::1] , * "))
    hosts = set(main._trusted_hosts())
    assert {"example.com", "::1", "localhost", "127.0.0.1", "testserver"} <= hosts
    assert "*" not in hosts


@pytest.mark.parametrize("url", [
    "https://evil.example",
    "https://127.0.0.1",
    "https://10.0.0.1",
    "http://toapis.com",
    "https://toapis.com:8443",
    "https://user:secret@toapis.com",
    "https://toapis.com?token=x",
])
def test_remote_provider_urls_require_allowlisted_https_origin(url):
    with pytest.raises(ValueError):
        validate_provider_url(url, "text_base_url")


@pytest.mark.parametrize("url", [
    "http://192.168.1.10:8188",
    "http://10.0.0.1:8191",
    "http://evil.example:8188",
    "file:///C:/Windows/win.ini",
])
def test_local_engine_urls_must_be_loopback(url):
    with pytest.raises(ValueError):
        validate_provider_url(url, "comfyui_url")


def test_provider_dns_results_are_checked_before_network_io(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _dns("10.2.3.4"))
    with pytest.raises(ValueError, match="私网|非回环|私网、回环"):
        validate_provider_url("https://toapis.com", "text_base_url", resolve=True)
    with pytest.raises(ValueError, match="非回环"):
        validate_provider_url("http://localhost:8188", "comfyui_url", resolve=True)
    monkeypatch.setattr(socket, "getaddrinfo", _dns("127.0.0.1"))
    validate_provider_url("http://localhost:8188", "comfyui_url", resolve=True)


def test_result_downloads_allow_signed_public_https_only(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _dns("93.184.216.34"))
    validate_public_download_url("https://cdn.example/image.png?signature=abc")
    for url in ("http://cdn.example/image.png", "file:///C:/secret.png", "https://127.0.0.1/image.png"):
        with pytest.raises(ValueError):
            validate_public_download_url(url)


def test_provider_adapters_reject_unsafe_bases_and_redirects(tmp_path):
    with pytest.raises(ValueError):
        ToAPIsTextProvider("secret", base_url="https://evil.example")
    with pytest.raises(ValueError):
        ToAPIsImageProvider("secret", tmp_path, None, base_url="http://127.0.0.1:8188")
    with pytest.raises(ValueError, match="redirect"):
        _NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil.example")


def test_effective_config_rejects_persisted_private_provider(tmp_path):
    machine = MachineSettings(tmp_path / "machine")
    machine.save({"runtimeConfig": {"text_base_url": "https://10.0.0.1"}})
    with pytest.raises(ValueError):
        effective(_settings(tmp_path), machine)


def test_text_adapter_disables_redirects(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _dns("93.184.216.34"))
    seen = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):
            seen.append((url, kwargs))
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"choices": [{"message": {"content": '{"turns":[]}'}}]},
            )

    client_options = []

    def fake_client(**kwargs):
        client_options.append(kwargs)
        return FakeClient()

    monkeypatch.setattr("duocast.adapters.toapis_text.httpx.AsyncClient", fake_client)
    provider = ToAPIsTextProvider("secret")
    result = asyncio.run(provider._chat("system", "user"))
    assert result == {"turns": []}
    assert seen and seen[0][0] == "https://toapis.com/v1/chat/completions"
    assert seen[0][1]["headers"]["Authorization"] == "Bearer secret"
    assert client_options[0]["follow_redirects"] is False
