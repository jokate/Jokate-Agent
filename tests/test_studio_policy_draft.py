"""H-6: a `policy_draft` that is filed and approved is the POLICY.md the REST reads (fixtures of test_studio_mcp.py)."""
from test_studio_mcp import hr_turn, studio_dir  # noqa: F401
from test_studio_api_tasks import world  # noqa: F401 - the fixture `desk` stands on
from test_studio_tools import desk  # noqa: F401


def test_a_policy_draft_filed_and_approved_is_the_policy_file_the_rest_reads(desk, studio_dir):  # noqa: F811
    hr, token = hr_turn(desk)
    pid = desk.w.pid
    sent = desk.call("policy_draft", token, project_id=pid, policy_md="# 정책\n기획 문서 변경은 결재\n", reason="면담에서 정했다",
                     submit=True)
    assert not (studio_dir / "projects" / pid / "POLICY.md").exists()
    r = desk.w.owner.post("/studio/inbox/" + sent["inbox_id"] + "/decide", json={"decision": "approve"})
    assert r.status_code == 200, r.text
    assert (studio_dir / "projects" / pid / "POLICY.md").read_bytes() == "# 정책\n기획 문서 변경은 결재\n".encode("utf-8")
    got = desk.w.owner.get("/studio/projects/" + pid + "/policy").json()
    assert got["text"] == "# 정책\n기획 문서 변경은 결재\n" and len(got["sha256"]) == 64 and got["updated_at"]
