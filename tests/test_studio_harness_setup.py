"""The harness is laid into a project when it is registered (T3; §6.4 · §11.3): `bootstrap.py --auto` runs once for a
project that has no harness, not for one that has, a failure leaves the import done and a system message in HR's
room, and `POST /studio/projects/{id}/harness` runs it by hand. The harness is a fake folder and the run a fake."""
import pytest

from relay_agent.studio import api, employees, harness_setup
from test_studio_importer import fake_project, studio  # noqa: F401 - `studio` is its fixture


class Fake:
    """Stands for `run_process`: counts the calls and, when asked to, lays the harness's two things."""

    def __init__(self, lay=True, code=0, error=None, overwrite=False):
        self.calls, self.lay, self.code, self.error, self.overwrite = [], lay, code, error, overwrite

    def __call__(self, argv, *, cwd, log, timeout=None):
        self.calls.append((list(argv), str(cwd)))
        if self.error is not None:
            raise self.error
        log("bootstrap: 시작")
        log("bootstrap: 끝났다" if self.code == 0 else "bootstrap: 깨졌다")
        from pathlib import Path
        target = Path(argv[-1])
        if self.overwrite:  # a bootstrap that breaks its promise, whatever it exits with
            (target / "CLAUDE.md").write_text("# 하네스\n", encoding="utf-8")
            (target / "Docs").mkdir(exist_ok=True)
        elif self.lay and self.code == 0:  # like the real --auto: a file that is there stays
            if not (target / "CLAUDE.md").exists():
                (target / "CLAUDE.md").write_text("# 하네스\n", encoding="utf-8")
            (target / "Docs").mkdir(exist_ok=True)
        return self.code


def harness(studio, monkeypatch, *, script=True):
    root = studio.root / "harness"
    (root / "skills" / "game-bootstrap" / "scripts").mkdir(parents=True)
    if script:
        (root / "skills" / "game-bootstrap" / "scripts" / "bootstrap.py").write_text("# fake\n", encoding="utf-8")
    monkeypatch.setattr(api._config(), "studio_harness_root", str(root))
    return root


def hr_dm(studio):
    [hr] = studio.store.list_employees(department_id=employees.HR_DEPARTMENT)
    return studio.store.list_rooms(kind="dm", employee_id=hr["id"])[0]


def hr_notes(studio):
    return [m for m in studio.store.list_messages(hr_dm(studio)["id"])
            if (m["meta"] or {}).get("event") == harness_setup.EVENT]


def bare(studio, **kw):
    return fake_project(studio.root, claude=False, docs=False, **kw)


def test_importing_a_project_without_a_harness_runs_the_setup_once(studio, monkeypatch):
    root = harness(studio, monkeypatch)
    fake = Fake()
    monkeypatch.setattr(harness_setup, "run_process", fake)
    employees.ensure_hr(studio.store, api._config().studio_dir)
    folder = bare(studio)
    r = studio.bring(folder)
    assert r.status_code == 201, r.text
    [(argv, cwd)] = fake.calls
    assert argv[1:] == [str(root / harness_setup.SCRIPT), "--auto", str(folder.resolve())] and cwd == str(root)
    [note] = hr_notes(studio)
    assert note["author_kind"] == "system" and "완료" in note["body"]
    assert note["meta"] == {"event": "harness_setup", "project_id": r.json()["id"], "status": "done"}
    # the policy talk comes after the note
    assert [(m["meta"] or {}).get("event") for m in studio.store.list_messages(hr_dm(studio)["id"])] == \
        ["harness_setup", "policy_interview"]
    # it is laid now: a second pass of the automatic kind has nothing to run
    assert harness_setup.auto(folder, str(root))["status"] == "ready" and len(fake.calls) == 1


def test_a_project_that_has_the_harness_is_left_alone(studio, monkeypatch):
    harness(studio, monkeypatch)
    fake = Fake()
    monkeypatch.setattr(harness_setup, "run_process", fake)
    employees.ensure_hr(studio.store, api._config().studio_dir)
    r = studio.bring(fake_project(studio.root))
    assert r.status_code == 201, r.text
    assert fake.calls == [] and hr_notes(studio) == []


@pytest.mark.parametrize("fake, told", [(Fake(code=1), "exit 1: bootstrap: 깨졌다"), (Fake(error=OSError("no python")), "no python"),
                                         (Fake(lay=False), "아직 없다")])
def test_a_failing_setup_still_imports_and_leaves_a_message(studio, monkeypatch, fake, told):
    harness(studio, monkeypatch)
    employees.ensure_hr(studio.store, api._config().studio_dir)
    monkeypatch.setattr(harness_setup, "run_process", fake)
    r = studio.bring(bare(studio))
    assert r.status_code == 201, r.text
    assert len(fake.calls) == 1 and studio.store.get_project(r.json()["id"])
    [note] = hr_notes(studio)
    assert note["meta"]["status"] == "failed" and "실패" in note["body"] and "가져오기는 됐다" in note["body"]
    assert told in note["body"] and note["meta"]["project_id"] == r.json()["id"]


def test_without_a_harness_the_setup_is_skipped_with_a_message(studio, monkeypatch):
    fake = Fake()
    monkeypatch.setattr(harness_setup, "run_process", fake)
    employees.ensure_hr(studio.store, api._config().studio_dir)
    r = studio.bring(bare(studio))
    assert r.status_code == 201, r.text
    assert fake.calls == []
    [note] = hr_notes(studio)
    assert note["meta"]["status"] == "skipped" and "건너뜀" in note["body"]


def test_rest_runs_the_setup_and_answers(studio, monkeypatch):
    harness(studio, monkeypatch)
    fake = Fake()
    monkeypatch.setattr(harness_setup, "run_process", fake)
    employees.ensure_hr(studio.store, api._config().studio_dir)
    folder = fake_project(studio.root)  # the harness is there already: the REST runs all the same
    pid = studio.bring(folder).json()["id"]
    assert fake.calls == []
    r = studio.owner.post(f"/studio/projects/{pid}/harness")
    assert r.status_code == 200, r.text
    assert r.json() == {"project_id": pid, "status": "done", "detail": "bootstrap.py --auto",
                        "harness": {"claude_md": True, "docs": True}}
    assert len(fake.calls) == 1 and [m["meta"]["status"] for m in hr_notes(studio)] == ["done"]
    fake.code = 2  # a setting that fails is told in the answer, not as an error
    r = studio.owner.post(f"/studio/projects/{pid}/harness")
    assert r.status_code == 200 and r.json()["status"] == "failed" and "exit 2" in r.json()["detail"]


def test_rest_refuses_a_project_that_is_not_there_and_a_missing_harness(studio, monkeypatch):
    fake = Fake()
    monkeypatch.setattr(harness_setup, "run_process", fake)
    pid = studio.bring(fake_project(studio.root)).json()["id"]
    r = studio.owner.post("/studio/projects/prj_none/harness")
    assert (r.status_code, r.json()["code"]) == (404, "not_found")
    r = studio.owner.post(f"/studio/projects/{pid}/harness")  # no harness on this computer
    assert (r.status_code, r.json()["code"]) == (409, "precondition_failed")
    harness(studio, monkeypatch, script=False)  # a harness folder without the script is none
    r = studio.owner.post(f"/studio/projects/{pid}/harness")
    assert (r.status_code, r.json()["code"]) == (409, "precondition_failed")
    assert fake.calls == []


def test_the_argument_form_is_fixed(studio, monkeypatch):
    import sys
    root = harness(studio, monkeypatch)
    fake = Fake()
    monkeypatch.setattr(harness_setup, "run_process", fake)
    folder = bare(studio)
    assert harness_setup.auto(folder, str(root))["status"] == "done"
    assert fake.calls == [([sys.executable, str(root / "skills" / "game-bootstrap" / "scripts" / "bootstrap.py"),
                            "--auto", str(folder)], str(root))]


def test_a_rule_file_that_is_there_is_put_back_when_the_setup_overwrites_it(studio, monkeypatch):
    root = harness(studio, monkeypatch)
    folder = bare(studio)
    (folder / "CLAUDE.md").write_text("# 내 규칙\n", encoding="utf-8")
    (folder / "CLAUDE.local.md").write_text("# 내 로컬\n", encoding="utf-8")
    monkeypatch.setattr(harness_setup, "run_process", Fake(overwrite=True))
    r = harness_setup.auto(folder, str(root))
    assert r["status"] == "done" and "되돌림" in r["detail"] and "CLAUDE.md" in r["detail"]
    assert (folder / "CLAUDE.md").read_text(encoding="utf-8") == "# 내 규칙\n"
    assert (folder / "CLAUDE.local.md").read_text(encoding="utf-8") == "# 내 로컬\n"  # untouched: not in the list
    assert "CLAUDE.local.md" not in r["detail"]


def test_a_setup_that_leaves_the_files_alone_says_nothing_of_putting_back(studio, monkeypatch):
    root = harness(studio, monkeypatch)
    folder = bare(studio)
    (folder / "CLAUDE.md").write_text("# 내 규칙\n", encoding="utf-8")
    monkeypatch.setattr(harness_setup, "run_process", Fake())
    assert harness_setup.auto(folder, str(root)) == {"status": "done", "detail": "bootstrap.py --auto"}
    assert (folder / "CLAUDE.md").read_text(encoding="utf-8") == "# 내 규칙\n"


def test_a_file_is_put_back_even_when_the_overwriting_run_fails(studio, monkeypatch):
    root = harness(studio, monkeypatch)
    folder = bare(studio)
    (folder / "CLAUDE.md").write_text("# 내 규칙\n", encoding="utf-8")
    monkeypatch.setattr(harness_setup, "run_process", Fake(overwrite=True, code=1))
    r = harness_setup.auto(folder, str(root))
    assert r["status"] == "failed" and "exit 1" in r["detail"] and "되돌림" in r["detail"]
    assert (folder / "CLAUDE.md").read_text(encoding="utf-8") == "# 내 규칙\n"
