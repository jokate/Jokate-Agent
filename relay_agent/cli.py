"""Terminal entry point (no server needed).

    relay session new "HON 캐릭터 작업" --workdir C:/.../MNYS
    relay sessions                          # 세션 목록
    relay history <session_id>              # 그 세션에서 한 질문과 결과
    relay search "쿨다운"                    # 전체 질문 이력 검색
    relay run default "목표" --session <id>  # 세션에 이어서 질문(세션 없으면 --workdir 로 새 세션)
    relay run auto "목표" --workdir <폴더>    # 요청과 대상 폴더에 맞는 릴레이를 자동으로 골라 실행
    relay log <run_id>                      # 실제로 한 작업(단계, 도구 호출) 타임라인
    relay approve <run_id> | resume <run_id> | status <run_id> | usage [run_id]
    relay cancel <run_id> | patch <run_id> | apply <run_id> | discard <run_id> | rollback <run_id>
    relay providers [--probe]               # AI 별 사용 가능 여부·남은 사용량(Claude 5h/7d %)
    relay import-cc --cwd C:/.../MNYS       # 그 폴더의 최근 Claude Code 대화를 세션으로 가져오기
    relay remote on | off | status          # 다른 기기에서 접속 허용(토큰 자동 생성) / 이 PC 전용 / 주소·토큰 확인
    relay remote on --tailscale             # Tailscale 로 들어온 기기만 허용 (+ 토큰)
    relay autostart on | off | status       # Windows 로그온 때 서버 시작, 죽거나 재시작하면 다시 띄움
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
    "relay_routed": "자동 선택",
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


LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
REMOTE_HOST = "0.0.0.0"
TAILNET = ["100.64.0.0/10", "fd7a:115c:a1e0::/48"]  # the address ranges Tailscale assigns to devices
TAILSCALE_EXE = Path(r"C:\Program Files\Tailscale\tailscale.exe")


def lan_urls(port: int) -> list[str]:
    """Addresses other devices can try: this machine's IPv4s except loopback."""
    import socket

    try:
        ips = {a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    except OSError:
        ips = set()
    return [f"http://{ip}:{port}" for ip in sorted(ips) if not ip.startswith("127.")]


def tailscale_info() -> dict:
    """{"installed", "running", "ips", "dns"} from the Tailscale CLI, when it is installed."""
    import shutil
    import subprocess

    exe = shutil.which("tailscale") or (str(TAILSCALE_EXE) if TAILSCALE_EXE.exists() else None)
    if not exe:
        return {"installed": False, "running": False, "ips": [], "dns": ""}
    try:
        out = subprocess.run([exe, "status", "--json"], capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=10)
        data = json.loads(out.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"installed": True, "running": False, "ips": [], "dns": ""}
    me = data.get("Self") or {}
    return {"installed": True, "running": data.get("BackendState") == "Running",
            "ips": [ip for ip in me.get("TailscaleIPs") or [] if ":" not in ip],
            "dns": (me.get("DNSName") or "").rstrip(".")}


def remote_command(action: str, cfg, local: Path | None = None, new_token: bool = False,
                   tailscale: bool | None = None, ts: dict | None = None) -> list[str]:
    """Turn access from other devices on or off in relay.config.local.yaml; `on` makes a token if there is none.
    tailscale=True also limits it to the tailnet, False lifts that limit, None leaves it as it is.
    The mode sticks: start.bat, autostart and the dashboard restart all start `relay serve`, which reads it."""
    import os
    import secrets
    from urllib.parse import urlparse

    import yaml

    from .config import ROOT

    local = local or ROOT / "relay.config.local.yaml"
    data = (yaml.safe_load(local.read_text(encoding="utf-8")) if local.exists() else None) or {}
    out: list[str] = []
    if action in ("on", "off"):
        data["serve_host"] = REMOTE_HOST if action == "on" else "127.0.0.1"
        if action == "on" and (new_token or not (data.get("auth_token") or os.environ.get("KATAE_TOKEN"))):
            data["auth_token"] = secrets.token_urlsafe(24)
            out.append("새 토큰을 만들었습니다" + (" (이전 토큰은 더 이상 통하지 않습니다)" if new_token else ""))
        if action == "on" and tailscale is True:
            data["remote_networks"] = list(TAILNET)
        elif action == "on" and tailscale is False:
            data.pop("remote_networks", None)
        local.write_text("# This machine only (git-ignored).\n" + yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                         encoding="utf-8")
    host = os.environ.get("KATAE_HOST") or data.get("serve_host") or cfg.serve_host
    token = os.environ.get("KATAE_TOKEN") or data.get("auth_token") or ""
    networks = data.get("remote_networks", cfg.remote_networks)
    only_tailnet = bool(networks) and set(networks) <= set(TAILNET)
    port = urlparse(cfg.server_url).port or 8020
    if host in LOOPBACK_HOSTS:
        out.append(f"원격 접속: 꺼짐 (이 PC 에서만 http://127.0.0.1:{port})")
    else:
        scope = "Tailscale 전용" if only_tailnet else (f"허용 네트워크 {', '.join(networks)}" if networks else "토큰이 있으면 어느 네트워크든")
        out.append(f"원격 접속: 켜짐 ({scope})")
        ts = tailscale_info() if ts is None else ts
        if ts["running"]:
            urls = ([f"http://{ts['dns']}:{port}"] if ts["dns"] else []) + [f"http://{ip}:{port}" for ip in ts["ips"]]
            out += [f"  Tailscale 주소: {u}" for u in urls]
        elif ts["installed"]:
            out.append("  Tailscale: 설치됐지만 연결 안 됨 — 트레이의 Tailscale 에서 로그인/Connect")
        else:
            out.append("  Tailscale: 설치 안 됨 — https://tailscale.com/download 에서 이 PC 와 접속할 기기 모두에 설치·같은 계정 로그인")
        if not only_tailnet:
            out += [f"  같은 공유기 주소: {u}" for u in lan_urls(port) if not u.startswith("http://100.")]
        out.append(f"  토큰: {token or '(없음 — relay remote on 으로 만드세요)'}   ← 대시보드가 처음 한 번 묻습니다")
        out.append("  다른 PC 의 katae MCP: KATAE_URL=<위 주소>  KATAE_TOKEN=<토큰>")
        out.append("  첫 실행 때 Windows 방화벽 창이 뜨면 허용을 누르세요 (포트를 인터넷에 직접 열지 마세요).")
    if action in ("on", "off"):
        out.append("실행 중인 서버에는 재시작해야 적용됩니다: 대시보드의 '서버 재시작' 또는 start.bat 을 다시 실행")
    return out


AUTOSTART_NAME = "Agent Katae Server.cmd"


def startup_folder() -> Path:
    import os

    return Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def autostart_script(uv: str, port: int) -> str:
    from .config import ROOT

    return "\r\n".join([
        "@echo off",
        "chcp 65001 >nul",
        "rem Agent 카태: 로그온 때 서버를 띄우고, 죽으면 다시 띄운다. 만든 명령: relay autostart on  / 끄기: relay autostart off",
        f'cd /d "{ROOT}"',
        f'start "Agent 카태 - 서버 (자동)" /min "{uv}" run python -m relay_agent.supervise {port}',
        "",
    ])


def autostart_command(action: str, port: int = 8020, folder: Path | None = None, start_now: bool = True) -> list[str]:
    """Windows: a script in the user's Startup folder runs the supervisor at logon (no admin, no system change)."""
    import os
    import shutil
    import subprocess

    from .supervise import port_busy

    if folder is None and os.name != "nt":
        return ["자동 시작 등록은 Windows 용입니다. 다른 OS 는 systemd/launchd 에 "
                f"`uv run python -m relay_agent.supervise {port}` 를 등록하세요."]
    script = (folder or startup_folder()) / AUTOSTART_NAME
    if action == "on":
        uv = shutil.which("uv")
        if not uv:
            return ["uv 를 찾을 수 없습니다. setup.bat 을 먼저 실행하세요."]
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_bytes(autostart_script(uv, port).encode("utf-8"))
        out = [f"자동 시작 켜짐: 로그온하면 서버(포트 {port})가 뜨고, 죽거나 재시작하면 다시 뜹니다", f"  등록 파일: {script}"]
        if start_now and not port_busy(port):
            subprocess.Popen(["cmd", "/c", str(script)])
            out.append("  지금 바로 시작했습니다 (작업 표시줄의 'Agent 카태 - 서버 (자동)' 창)")
        elif port_busy(port):
            out.append("  서버가 이미 떠 있어 지금은 시작하지 않았습니다 — 그 창을 닫으면 다음 로그온부터 자동으로 뜹니다")
        return out
    if action == "off":
        existed = script.exists()
        script.unlink(missing_ok=True)
        return ["자동 시작 꺼짐" if existed else "자동 시작이 등록돼 있지 않습니다",
                "  지금 떠 있는 서버는 'Agent 카태 - 서버 (자동)' 창을 닫으면 멈춥니다"]
    return [f"자동 시작: {'켜짐' if script.exists() else '꺼짐'} ({script})",
            f"서버(포트 {port}): {'실행 중' if port_busy(port) else '꺼짐'}"]


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
    remote = cfg.serve_host not in LOOPBACK_HOSTS
    line(not remote or bool(cfg.auth_token), f"serve_host: {cfg.serve_host} ({'원격 접속 켜짐' if remote else '이 PC 전용'})",
         "원격 접속에는 토큰이 필요합니다: relay remote on")
    if remote:
        print(f"➖ 원격 허용 네트워크: {', '.join(cfg.remote_networks) or '제한 없음 (토큰만)'}"
              + ("  — Tailscale 전용으로: relay remote on --tailscale" if not cfg.remote_networks else ""))
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
    p_serve.add_argument("--host", default=None, help="default: serve_host from config (relay remote on|off)")
    p_serve.add_argument("--port", type=int, default=8020)
    p_remote = sub.add_parser("remote", help="access from other devices: on (makes a token) | off | status")
    p_remote.add_argument("action", nargs="?", choices=["on", "off", "status"], default="status")
    p_remote.add_argument("--new-token", action="store_true", help="on: replace the token (old one stops working)")
    scope = p_remote.add_mutually_exclusive_group()
    scope.add_argument("--tailscale", dest="tailscale", action="store_const", const=True, default=None,
                       help="on: accept other devices only over Tailscale (plus the token)")
    scope.add_argument("--anywhere", dest="tailscale", action="store_const", const=False,
                       help="on: lift the Tailscale-only limit (any network with the token)")
    p_auto = sub.add_parser("autostart", help="Windows: start the server at logon and restart it when it exits")
    p_auto.add_argument("action", nargs="?", choices=["on", "off", "status"], default="status")
    p_auto.add_argument("--port", type=int, default=8020)
    p_auto.add_argument("--no-start", action="store_true", help="on: only register, don't start it now")
    sub.add_parser("doctor", help="check this machine: git, claude login, providers, config paths")
    sub.add_parser("disk", help="where the disk space goes (snapshots, working copies, patches)")
    p_clean = sub.add_parser("cleanup", help="delete decided/expired workspaces (result.patch is kept)")
    p_clean.add_argument("--days", type=float, default=None)
    args = parser.parse_args()

    cfg = Config.load()
    if args.cmd == "serve":
        host = args.host or cfg.serve_host
        if host not in LOOPBACK_HOSTS:
            if not cfg.auth_token:
                sys.exit("다른 기기에서 접속하게 하려면 먼저 토큰을 설정하세요: relay remote on "
                         "(또는 환경 변수 KATAE_TOKEN / relay.config.local.yaml 의 auth_token)")
            print("  원격 접속 켜짐 — 다른 기기: " + (", ".join(lan_urls(args.port)) or f"<이 PC IP>:{args.port}")
                  + "  (토큰: relay remote status)")
        import uvicorn

        uvicorn.run("relay_agent.server:app", host=host, port=args.port)
        return
    if args.cmd == "remote":
        print("\n".join(remote_command(args.action, cfg, new_token=args.new_token, tailscale=args.tailscale)))
        return
    if args.cmd == "autostart":
        print("\n".join(autostart_command(args.action, args.port, start_now=not args.no_start)))
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
