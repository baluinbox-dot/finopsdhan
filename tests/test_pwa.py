"""The PWA manifest and service worker are unauthenticated, always-available
endpoints (a mobile browser fetches them before/without a login session)."""

from __future__ import annotations


def test_manifest_json_served_with_correct_media_type_and_icons(client):
    resp = client.get("/manifest.json")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/manifest+json")

    body = resp.json()
    assert body["name"] == "FinOps Dhan Algo"
    assert body["start_url"] == "/"
    assert body["scope"] == "/"
    assert body["display"] == "standalone"
    assert len(body["icons"]) == 4
    for icon in body["icons"]:
        assert icon["src"].startswith("/static/icons/icon-")


def test_service_worker_served_from_app_root_not_under_static(client):
    # Must be served from the root (not /static/sw.js) so its default
    # registration scope covers the whole app, not just /static/.
    resp = client.get("/sw.js")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/javascript")
    assert "fetch" in resp.text


def test_login_page_links_the_manifest_and_registers_the_service_worker(client):
    resp = client.get("/auth/login")
    assert resp.status_code == 200
    assert 'rel="manifest"' in resp.text
    assert "/manifest.json" in resp.text
    assert "pwa.js" in resp.text
