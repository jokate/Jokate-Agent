"""A-13: `GET /studio/app/latest` and the APK download (§6.4, §12.4 #48). The folder is a tmp folder; the server
only reads it."""
import hashlib
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from relay_agent.studio import api, app_release
from tests.test_studio_api_tasks import AUTH, TOKEN, Fx, World, refused
from tests.test_studio_inbox import phone_of

ARM = b"arm64-apk-bytes" * 1000
V7 = b"v7a-apk-bytes" * 500
INFO = {"version_name": "1.4.0", "build_number": 14, "notes": "버튼 수정\n둘째 줄",
        "apks": {"arm64-v8a": "app-arm64-v8a-release.apk", "armeabi-v7a": "app-armeabi-v7a-release.apk"}}


@pytest.fixture
def world(tmp_path, monkeypatch):
    import relay_agent.server as server

    fx = Fx()
    monkeypatch.setattr(server.cfg, "runs_dir", tmp_path / "runs")
    monkeypatch.setattr(server.cfg, "auth_token", TOKEN)
    monkeypatch.setattr(server.cfg, "remote_networks", [])
    monkeypatch.setattr(server.cfg, "studio_app_dir", str(tmp_path / "app"))
    monkeypatch.setattr(api, "_effects", fx)
    api.close_store()
    yield World(server, fx)
    api.close_store()


@pytest.fixture
def folder(tmp_path):
    d = tmp_path / "app"
    d.mkdir()
    (d / "app-arm64-v8a-release.apk").write_bytes(ARM)
    (d / "app-armeabi-v7a-release.apk").write_bytes(V7)
    (d / "latest.json").write_text(json.dumps(INFO, ensure_ascii=False), encoding="utf-8")
    return d


def test_info_has_the_six_values_and_the_download_matches_its_sha256(world, folder):
    r = world.owner.get("/studio/app/latest")
    assert r.status_code == 200, r.text
    info = r.json()
    assert info["available"] is True
    assert (info["version_name"], info["build_number"], info["notes"]) == ("1.4.0", 14, "버튼 수정")
    assert info["size"] == len(ARM) and info["abi"] == "arm64-v8a"
    assert info["uploaded_at"] == app_release.datetime.fromtimestamp(
        (folder / info["file"]).stat().st_mtime).isoformat(timespec="seconds")
    got = world.owner.get(info["url"])
    assert got.status_code == 200 and got.content == ARM
    assert hashlib.sha256(got.content).hexdigest() == info["sha256"]
    assert got.headers["content-type"].startswith(app_release.APK_MIME)


def test_the_abi_picks_the_apk(world, folder):
    v7 = world.owner.get("/studio/app/latest", params={"abi": "armeabi-v7a"}).json()
    assert (v7["abi"], v7["size"], v7["file"]) == ("armeabi-v7a", len(V7), "app-armeabi-v7a-release.apk")
    assert v7["sha256"] == hashlib.sha256(V7).hexdigest()
    # an abi without an entry and no universal one: no update; with a universal APK it is taken
    assert world.owner.get("/studio/app/latest", params={"abi": "x86_64"}).json() == {"available": False, "reason": "no_apk"}
    (folder / "app-universal.apk").write_bytes(b"fat")
    info = {**INFO, "apks": {**INFO["apks"], "universal": "app-universal.apk"}}
    (folder / "latest.json").write_text(json.dumps(info), encoding="utf-8")
    assert world.owner.get("/studio/app/latest", params={"abi": "x86_64"}).json()["file"] == "app-universal.apk"


def test_sha256_is_not_calculated_for_every_request(world, folder, monkeypatch):
    calls = []
    real = app_release._digest
    monkeypatch.setattr(app_release, "_digest", lambda p: calls.append(p) or real(p))
    app_release._digests.clear()
    first = world.owner.get("/studio/app/latest").json()["sha256"]
    assert world.owner.get("/studio/app/latest").json()["sha256"] == first and len(calls) == 1
    apk = folder / INFO["apks"]["arm64-v8a"]
    apk.write_bytes(ARM + b"more")
    os.utime(apk, ns=(apk.stat().st_atime_ns, apk.stat().st_mtime_ns + 5_000_000_000))
    second = world.owner.get("/studio/app/latest").json()["sha256"]
    assert second == hashlib.sha256(ARM + b"more").hexdigest() != first and len(calls) == 2


def test_nothing_to_offer_is_a_normal_answer(world, folder):
    c = world.owner
    (folder / "latest.json").write_text("{not json", encoding="utf-8")
    assert c.get("/studio/app/latest").json() == {"available": False, "reason": "bad_info"}
    (folder / "latest.json").write_text(json.dumps({**INFO, "apks": {"arm64-v8a": "../x.apk"}}), encoding="utf-8")
    assert c.get("/studio/app/latest").json() == {"available": False, "reason": "no_apk"}
    (folder / "latest.json").write_text(json.dumps({"version_name": "1"}), encoding="utf-8")
    assert c.get("/studio/app/latest").json()["reason"] == "bad_info"
    (folder / "latest.json").unlink()
    r = c.get("/studio/app/latest")
    assert (r.status_code, r.json()) == (200, {"available": False, "reason": "no_info"})
    for f in folder.iterdir():
        f.unlink()
    folder.rmdir()
    r = c.get("/studio/app/latest")
    assert (r.status_code, r.json()) == (200, {"available": False, "reason": "no_folder"})
    assert not folder.exists()  # the server does not make it


def test_the_token_is_needed_and_a_device_token_or_the_owner_passes(world, folder):
    paths = ["/studio/app/latest", "/studio/app/files/app-arm64-v8a-release.apk"]
    phone = phone_of(world)
    for path in paths:
        assert world.remote.get(path).status_code == 401
        assert TestClient(world.owner.app, client=("192.168.0.7", 50000), headers={"Authorization": "Bearer nope"}
                          ).get(path).status_code == 401
        assert world.owner.get(path).status_code == 200
        assert phone.get(path).status_code == 200


def test_names_outside_the_folder_are_refused(world, folder, tmp_path, monkeypatch):
    (tmp_path / "outside.apk").write_bytes(b"secret")
    (folder / "sub").mkdir()
    (folder / "sub" / "in.apk").write_bytes(b"x")
    for name in ["..", "%2e%2e", "%2e%2e%2foutside.apk", "..%2foutside.apk", "latest.json", "missing.apk",
                 "%2Fetc%2Fpasswd.apk", str(tmp_path / "outside.apk").replace("\\", "%5C").replace(":", "%3A"),
                 "sub%2Fin.apk", "sub"]:
        r = world.owner.get("/studio/app/files/" + name)
        assert r.status_code == 404, (name, r.status_code)
    refused(world.owner.get("/studio/app/files/latest.json"), 404, "not_found")
    link = folder / "link.apk"
    try:
        link.symlink_to(tmp_path / "outside.apk")
    except (OSError, NotImplementedError):
        # No symlinks on this PC: a plain file in the folder that Path.is_symlink reports as a link, so the
        # is_symlink branch of safe_file still runs (the resolve() comparison alone would not refuse it).
        link.write_bytes(b"secret")
        real = Path.is_symlink
        monkeypatch.setattr(Path, "is_symlink", lambda self: self.name == "link.apk" or real(self))
    refused(world.owner.get("/studio/app/files/link.apk"), 404, "not_found")
    # a link named by latest.json is not offered either
    (folder / "latest.json").write_text(json.dumps({**INFO, "apks": {"arm64-v8a": "link.apk"}}), encoding="utf-8")
    assert world.owner.get("/studio/app/latest").json()["available"] is False
