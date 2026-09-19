"""Terminal entry point (no server needed).

    relay session new "HON 캐릭터 작업" --workdir C:/.../MNYS
    relay sessions                          # 세션 목록
    relay history <session_id>              # 그 세션에서 한 질문과 결과
    relay search "쿨다운"                    # 전체 질문 이력 검색
    relay run default "목표" --session <id>  # 세션에 이어서 질문(세션 없으면 --workdir 로 새 세션)
    relay log <run_id>                      # 실제로 한 작업(단계, 도구 호출) 타임라인
    relay approve <run_id> | resume <run_id> | status <run_id> | usage [run_id]
    relay cancel <run_id> | patch <run_id> | apply <run_id> | discard <run_id> | rollback <run_id>
    relay providers [--probe]               # AI 별 사용 가능 여부·남은 사용량(Claude 5h/7d %)
    relay import-cc --cwd C:/.../MNYS       # 그 폴더의 최근 Claude Code 대화를 세션으로 가져오기
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Config, build_engine
from .pipeline import RelayEngine, RunState

EVENT_LABELS = {
    "run_created": "릴레이 시작",
    "stage_started": "단계 시작",
    "tool_use": "도구",
    "tool_error": "도구 오류",
    "note": "메모",
    "stage_finished": "단계 완료",
    "sent_back": "되돌림",
    "awaiting_approval": "승인 대기",
    "approved": "승인됨",
    "run_done": "릴레이 완료",
    "run_failed": "실패",
}


def format_event(e: dict) -> str:
    d = e["detail"]
    kind = e["kind"]
    if kind == "tool_use":
        body = f"{d.get('tool')}  {d.get('target', '')}"
    elif kind == "stage_started":
        body = f"{d.get('model')} ({d.get('runner')}) reads={d.get('reads')}"
    elif kind == "stage_finished":
        cost = f" ${d['cost_usd']:.4f}" if d.get("cost_usd") is not None else ""
        body = f"{d.get('verdict')} · in={d.get('input_tokens')} out={d.get('output_tokens')}{cost} · {d.get('summary')}"
    elif kind == "sent_back":
        body = f"→ {d.get('to')} (#{d.get('attempt')}) {d.get('issues')}"
    else:
        body = " ".join(f"{k}={v}" for k, v in d.items())
    return f"{e['at'][11:19]} [{e['stage']:<7}] {EVENT_LABELS.get(kind, kind):<6} {body}"


def print_run(engine: RelayEngine, run: RunState) -> None:
    """Compact result: what happened, what changed, what to check. Details: relay log / HANDOFF.md."""
    s = engine.summary(run.id)
    status = {"done": "완료", "failed": "실패", "cancelled": "취소", "awaiting_approval": "승인 대기"}.get(run.status, run.status)
    print(f"\n■ {status} — {s['headline']}")
    for h in s["highlights"]:
        print(f"  · {h}")
    ch = s["changes"]
    if ch["files"]:
        print(f"  변경 {len(ch['files'])}개 파일 +{ch['insertions']} -{ch['deletions']} ({ch['mode']}, {ch['status']}): "
              + ", ".join(f["path"] for f in ch["files"][:6]))
    if s["verification"]:
        print("  검증: " + " / ".join(s["verification"]))
    for c in s["user_checks"]:
        print(f"  ☐ 확인: {c}")
    if s["stop"]:
        st = s["stop"]
        print(f"  ⏸ {st['stage']} 에서 멈춤 · 남은 단계: {' → '.join(st['remaining_stages']) or '-'}")
        for a in st["partial_actions"][:5]:
            print(f"     이미 한 일: {a}")
        print(f"     {st['resume_hint']}")
    if s["mcp"]:
        print("  MCP: " + ", ".join(f"{k}×{v['calls']} ({v['result_chars']}자)" for k, v in s["mcp"].items()))
    print(f"  비용 ${s['cost_usd']:.4f} · {s['tokens']:,} 토큰 · " + " → ".join(f"{x['stage']}({x['model']})" for x in s["stages"]))
    handoff = engine.runs_dir / run.id / "HANDOFF.md"
    print(f"  run={run.id} · 전체 내역: relay log {run.id}" + (f" · 인계서 {handoff}" if handoff.exists() else ""))


def print_log(events: list[dict], full: bool = False) -> None:
    """Per-stage timeline. By default consecutive tool calls collapse into counts."""
    bucket: dict[str, int] = {}

    def flush():
        if bucket:
            print("           도구 " + " · ".join(f"{k} {v}" for k, v in bucket.items()))
            bucket.clear()

    for e in events:
        kind, d = e["kind"], e["detail"]
        if not full and kind == "tool_use":
            bucket[d.get("tool") or "?"] = bucket.get(d.get("tool") or "?", 0) + 1
            continue
        if not full and kind in ("note", "mcp_result"):
            continue
        flush()
        if kind == "mcp_call":
            print(f"{e['at'][11:19]} [{e['stage']:<7}] MCP    {d.get('server')}.{d.get('tool')}  {d.get('target', '')}")
        else:
            print(format_event(e))
    flush()


def repo_command(args, cfg) -> str:
    """Write repo entries into relay.config.local.yaml (this machine only)."""
    import subprocess

    import yaml

    from .config import ROOT

    local = ROOT / "relay.config.local.yaml"
    data = (yaml.safe_load(local.read_text(encoding="utf-8")) if local.exists() else None) or {}
    repos = data.setdefault("repos", {})
    if args.action == "remove":
        repos.pop(args.name, None)
    else:
        entry = repos.setdefault(args.name, {})
        shared = cfg.repos.get(args.name, {})
        if args.action == "clone":
            url = args.url or entry.get("url") or shared.get("url")
            if not url:
                sys.exit("clone 에는 --url 이 필요합니다 (또는 relay.config.yaml 의 repos.<이름>.url)")
            base = Path(cfg.repos_root).expanduser() if cfg.repos_root else ROOT.parent  # beside the agent, not a fixed drive
            target = Path(args.path or base / args.name).resolve()
            if not target.exists():
                subprocess.run(["git", "clone", url, str(target)], check=True)
            entry["path"], entry["url"] = target.as_posix(), url
        else:
            if not args.path or not Path(args.path).is_dir():
                sys.exit(f"경로가 폴더가 아닙니다: {args.path}")
            entry["path"] = Path(args.path).resolve().as_posix()
        for key in ("url", "workspace", "relay", "docs", "notes"):
            if getattr(args, key, None):
                entry[key] = getattr(args, key)
        if args.verify:
            entry["verify"] = args.verify
    local.write_text("# This machine only (git-ignored).\n" + yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                     encoding="utf-8")
    return f"{args.action}: {args.name} -> {local}"


def doctor(cfg) -> int:
    """Environment check for a fresh machine. Makes no AI calls. Returns a process exit code."""
    import shutil
    import subprocess

    from .providers import ProviderRegistry

    problems = 0

    def line(ok: bool, what: str, hint: str = "") -> None:
        nonlocal problems
        problems += 0 if ok else 1
        print(f"{'✅' if ok else '❌'} {what}" + (f"\n   → {hint}" if hint and not ok else ""))

    line(sys.version_info >= (3, 11), f"Python {sys.version.split()[0]}", "Python 3.11 이상 필요")
    line(shutil.which("git") is not None, "git", "git 설치 필요 (작업 공간 스냅샷에 사용)")
    claude = shutil.which("claude")
    line(claude is not None, f"Claude Code CLI ({claude or '없음'})", "npm i -g @anthropic-ai/claude-code 후 `claude` 로 로그인")
    if claude:
        out = subprocess.run([claude, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace")
        line(out.returncode == 0, f"claude --version: {out.stdout.strip()}", "claude 실행 실패")
        creds = [Path.home() / ".claude" / ".credentials.json", Path.home() / ".claude.json"]
        line(any(p.exists() for p in creds), "Claude 로그인 흔적", "`claude` 를 한 번 실행해 로그인하세요 (확실한 확인: relay providers --probe)")
    for name, p in (("runs_dir", cfg.runs_dir), ("relays_dir", cfg.relays_dir)):
        line(p.exists() or name == "runs_dir", f"{name}: {p}", "경로가 없습니다")
    if cfg.docs_root.exists():
        line(True, f"docs_root: {cfg.docs_root}")
    else:  # optional (docs-qa only): a warning, not a problem
        print(f"➖ docs_root 없음 (선택): {cfg.docs_root} — 문서 질의는 저장소 등록 시 docs 폴더를 지정하면 됩니다")
    line(True, f"auth_token: {'설정됨' if cfg.auth_token else '없음 (이 PC 에서만 접속 가능)'}")
    from .repos import RepoRegistry

    for repo in RepoRegistry(cfg.repos).repos.values():
        hint = f"relay repo clone {repo.name}" if repo.url else f"relay repo add {repo.name} <이 머신의 경로>"
        line(repo.exists, f"repo {repo.name}: {repo.path or '(경로 미지정)'}", hint)
    print("\nAI providers:")
    reg = ProviderRegistry(cfg.providers, cfg.fallback_chain)
    for name, spec in reg.specs.items():
        if name == "mock":
            continue
        ok, reason = spec.availability()
        print(f"  {'●' if ok else '○'} {name:<14} {'' if ok else reason}")
    print(f"\n{'문제 없음' if not problems else f'확인 필요 {problems}건'}")
    return 1 if problems else 0


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="relay")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_session = sub.add_parser("session")
    p_session.add_argument("action", choices=["new", "complete"])
    p_session.add_argument("title", help="new: 제목 · complete: 세션 id")
    p_session.add_argument("--workdir", default=".")
    p_session.add_argument("--repo")
    sub.add_parser("sessions")
    sub.add_parser("history").add_argument("session_id")
    sub.add_parser("search").add_argument("query")

    p_run = sub.add_parser("run")
    p_run.add_argument("relay")
    p_run.add_argument("goal")
    p_run.add_argument("--session")
    p_run.add_argument("--workdir")
    p_run.add_argument("--workspace", choices=["none", "copy", "inplace"])
    p_run.add_argument("--repo", help="registered repository name (relay repos)")
    p_run.add_argument("--mcp", action="append", default=[],
                       help="extra MCP server for this run by name, e.g. --mcp unreal (repeatable)")
    p_run.add_argument("--approval", choices=["auto", "ai", "always", "never"], default="auto",
                       help="design gates: ai = pause only when the AI asks for a decision (default), always, never")
    p_run.add_argument("--attach", action="append", default=[], metavar="FILE",
                       help="attach a file (screenshot, log, spec...) the AI may read; repeatable")
    p_run.add_argument("--stage-model", action="append", default=[], metavar="STAGE=PROVIDER:MODEL",
                       help="pick a model (and effort) per stage, e.g. plan=claude:opus@high, build=@low (repeatable)")
    sub.add_parser("repos", help="registered repositories on this machine")
    p_repo = sub.add_parser("repo", help="register or clone a repository")
    p_repo.add_argument("action", choices=["add", "clone", "remove"])
    p_repo.add_argument("name")
    p_repo.add_argument("path", nargs="?", help="add: local path / clone: target folder (default repos_root/name)")
    p_repo.add_argument("--url")
    p_repo.add_argument("--workspace", choices=["none", "copy", "inplace"])
    p_repo.add_argument("--relay")
    p_repo.add_argument("--verify", action="append", default=None, help="verification command (repeatable)")
    p_repo.add_argument("--docs")
    p_repo.add_argument("--notes")
    for name in ("approve", "resume", "status", "cancel", "patch", "apply", "discard", "rollback"):
        sub.add_parser(name).add_argument("run_id")
    p_log = sub.add_parser("log")
    p_log.add_argument("run_id")
    p_log.add_argument("--full", action="store_true", help="every tool call instead of per-stage counts")
    sub.add_parser("usage").add_argument("run_id", nargs="?")
    p_prov = sub.add_parser("providers")
    p_prov.add_argument("--probe", action="store_true", help="refresh Claude usage windows (one tiny Haiku call)")
    p_cc = sub.add_parser("import-cc", help="import a Claude Code conversation (default: latest in --cwd)")
    p_cc.add_argument("--cwd", default=".")
    p_cc.add_argument("--project")
    p_cc.add_argument("--session-id")
    p_serve = sub.add_parser("serve", help="start the server (dashboard + API)")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8020)
    sub.add_parser("doctor", help="check this machine: git, claude login, providers, config paths")
    sub.add_parser("disk", help="where the disk space goes (snapshots, working copies, patches)")
    p_clean = sub.add_parser("cleanup", help="delete decided/expired workspaces (result.patch is kept)")
    p_clean.add_argument("--days", type=float, default=None)
    args = parser.parse_args()

    cfg = Config.load()
    if args.cmd == "serve":
        if args.host not in ("127.0.0.1", "localhost", "::1") and not cfg.auth_token:
            sys.exit("다른 기기에서 접속하게 하려면 먼저 토큰을 설정하세요: 환경 변수 KATAE_TOKEN 또는 relay.config.local.yaml 의 auth_token")
        import uvicorn

        uvicorn.run("relay_agent.server:app", host=args.host, port=args.port)
        return
    if args.cmd == "doctor":
        sys.exit(doctor(cfg))
    if args.cmd == "disk":
        d = build_engine(cfg).disk_usage()
        print(f"runs 전체 {d['total_mb']} MB · 실행 {d['runs']}개")
        print(f"  작업 복사본 {d['working_copies_mb']} MB · 패치 {d['patches_mb']} MB · DB {d['databases_mb']} MB")
        for s in d["snapshot_stores"]:
            print(f"  스냅샷 저장소 {s['repo'] or s['store']}: {s['mb']} MB (실행 간 공유)")
        for r in d["largest_runs"]:
            print(f"  큰 실행 {r['run']}: {r['mb']} MB")
        print("정리: relay cleanup  (결정된 실행은 즉시, 미결정은 보관 기간 후. result.patch 는 남음)")
        return
    if args.cmd == "cleanup":
        r = build_engine(cfg).cleanup_workspaces(cfg.workspace_retention_days if args.days is None else args.days)
        print(f"정리 {len(r['cleaned'])}개 실행 · {r['freed_mb']} MB 확보")
        return
    engine = build_engine(cfg)
    h = engine.history

    if args.cmd == "repos":
        for r in engine.repos.status():
            state = (f"{r.get('branch')} · 미커밋 {r.get('dirty')}" if r.get("git") else "git 아님") if r["exists"] else "경로 없음"
            print(f"{'●' if r['exists'] else '○'} {r['name']:<14} {r['path'] or '-':<45} {state}")
            if r["verify"]:
                print(f"    검증: {', '.join(r['verify'])}")
        if not engine.repos.repos:
            print('등록된 저장소가 없습니다: relay repo add <이름> <경로> --verify "<테스트 명령>"')
        return
    if args.cmd == "repo":
        print(repo_command(args, cfg))
        return
    if args.cmd == "session" and args.action == "complete":
        r = engine.complete_session(args.title)
        print(f"작업 완료: {r['session_id']} · 인계서 {r['handoffs_removed']}개 삭제")
    elif args.cmd == "session":
        repo = engine.repos.get(args.repo) if args.repo else None
        workdir = str(repo.resolved) if repo else str(Path(args.workdir).resolve())
        s = h.create_session(args.title, workdir, args.repo)
        print(f"session {s['id']}  {s['title']}  ({s['workdir']})")
    elif args.cmd == "sessions":
        for s in h.list_sessions():
            print(f"{s['id']}  {s['updated_at']}  turns={s['turns']:<3} {s['title']}  — 최근: {s['last_question'] or '-'}")
    elif args.cmd == "history":
        session = h.get_session(args.session_id)
        if not session:
            sys.exit(f"session {args.session_id} not found")
        print(f"# {session['title']}  ({session['workdir']})")
        for t in h.turns(args.session_id):
            print(f"\n[{t['at']}] ({t['relay']}, {t['status']}) run={t['run_id']}\n  Q: {t['question']}")
            if t["result"]:
                print("  A: " + t["result"].strip().replace("\n", "\n     "))
    elif args.cmd == "search":
        for t in h.search_turns(args.query):
            print(f"[{t['at']}] {t['session_title']} ({t['session_id']}) run={t['run_id']}\n  Q: {t['question']}")
    elif args.cmd == "run":
        session = h.get_session(args.session) if args.session else None
        if args.session and not session:
            sys.exit(f"session {args.session} not found")
        repo = args.repo or (session or {}).get("repo")
        workdir = Path(args.workdir) if args.workdir else (None if repo else Path(session["workdir"] if session else "."))
        stage_models = {}
        for item in args.stage_model:
            stage_name, _, choice = item.partition("=")
            choice, _, effort = choice.partition("@")  # plan=claude:opus@high
            provider, _, model = choice.partition(":") if ":" in choice else ("", "", choice)
            stage_models[stage_name] = {"provider": provider or None, "model": model or None, "effort": effort or None}
        run = engine.create(cfg.relays_dir / f"{args.relay}.yaml", args.goal, workdir, session_id=args.session,
                            workspace=args.workspace, repo=repo, stage_models=stage_models,
                            attachments=[Path(a) for a in args.attach], approval=args.approval,
                            mcp=args.mcp or None)
        print(f"session {run.session_id} / run {run.id}")
        print_run(engine, engine.advance(run.id))
    elif args.cmd == "providers":
        if args.probe:
            from .runners import probe_claude_limits

            found = probe_claude_limits()
            h.record_limits("claude", found.get("status"), found.get("windows", []))
        for name in engine.providers.specs:
            if name == "mock":
                continue
            st = engine.providers.status(name)
            wins = "  ".join(f"{w['window']} {round((w['utilization'] or 0) * 100)}%" for w in st.get("limits") or [])
            mark = "●" if st["available"] else "○"
            print(f"{mark} {name:<14} {wins or '-':<24} {st['reason']}")
    elif args.cmd == "import-cc":
        from . import cc_import

        project, sid = args.project, args.session_id
        if not (project and sid):
            found = cc_import.latest_session_for_cwd(Path(args.cwd).resolve())
            if not found:
                sys.exit(f"no Claude Code conversation for {Path(args.cwd).resolve()}")
            project, sid = found
        r = cc_import.import_session(h, project, sid, workdir=str(Path(args.cwd).resolve()))
        print(f"session {r['session_id']}: imported {r['imported']} of {r['total_prompts']} prompts")
    elif args.cmd == "cancel":
        print_run(engine, engine.cancel(args.run_id))
    elif args.cmd == "patch":
        print(engine.patch_text(args.run_id) or "(no changes)")
    elif args.cmd in ("apply", "discard", "rollback"):
        action = {"apply": engine.apply_changes, "discard": engine.discard_changes, "rollback": engine.rollback_changes}
        run = action[args.cmd](args.run_id)
        print(f"changes {run.changes_status}: {(run.changes or {}).get('stat', '')}")
    elif args.cmd == "log":
        print_log(h.events(args.run_id), full=args.full)
    elif args.cmd == "approve":
        engine.approve(args.run_id)
        print_run(engine, engine.advance(args.run_id))
    elif args.cmd == "resume":
        print_run(engine, engine.advance(args.run_id, resume=True))
    elif args.cmd == "status":
        print_run(engine, engine.load(args.run_id))
    else:
        print(json.dumps(engine.usage.summary(args.run_id), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
