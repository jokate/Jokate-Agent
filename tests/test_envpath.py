import os

from relay_agent import envpath


def test_refresh_appends_registry_entries_the_process_is_missing(tmp_path, monkeypatch):
    installed = tmp_path / "local-bin"
    installed.mkdir()
    (installed / "claude.exe").write_bytes(b"")
    kept = tmp_path / "venv-scripts"
    kept.mkdir()
    monkeypatch.setenv("PATH", str(kept))  # a process started before the install
    monkeypatch.setattr(envpath, "_registry_path", lambda: [str(kept), str(installed), str(tmp_path / "gone")])

    added = envpath.refresh()

    assert added == [str(installed)]  # already present and missing folders are skipped
    assert os.environ["PATH"].split(os.pathsep) == [str(kept), str(installed)]  # existing order wins
    assert envpath.refresh() == []


def test_claude_install_dirs_finds_the_native_installer_folder(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    monkeypatch.setattr(envpath.Path, "home", lambda: home)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    assert envpath.claude_install_dirs() == []
    (home / ".local" / "bin" / "claude.exe").write_bytes(b"")
    assert envpath.claude_install_dirs() == [str(home / ".local" / "bin")]
