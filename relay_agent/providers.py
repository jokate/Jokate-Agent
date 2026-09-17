"""AI providers and automatic switching.

A stage names a primary provider (claude by default) plus optional alternates. Before each stage
the engine asks the registry for candidates in order and skips providers that are
- not installed / not configured (no CLI binary, no API key),
- over their configured spend quota (e.g. daily_usd, from the usage log), or
- marked exhausted because they recently reported a usage/rate limit (cooldown).

If a stage fails with a quota error, the provider is marked exhausted and the same stage is
retried on the next candidate with the same baton. The baton is small by design, so handing
work to another AI costs little context.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel, Field


class ProviderSpec(BaseModel):
    name: str
    kind: str  # claude_cli | api | cli | openai | mock
    enabled: bool = True
    label: str = ""
    # cli: command template. Placeholders: {cwd} {out_file} {schema_file}. Model args are added from model_args.
    command: list[str] = []
    model_args: list[str] = []  # e.g. ["-m", "{model}"]; omitted when no model is mapped
    prompt_via: str = "stdin"  # stdin | arg
    # openai-compatible HTTP
    base_url: str = ""
    api_key_env: str = ""
    # tier alias (haiku/sonnet/opus/fable) -> this provider's model id. Unmapped -> provider default.
    model_map: dict[str, str] = {}
    # model id -> [input $/MTok, output $/MTok] for cost estimates
    prices: dict[str, list[float]] = {}
    daily_usd: float | None = Field(None, description="stop using this provider after this spend in 24h")
    cooldown_min: int = 60  # how long to skip after a usage-limit error
    switch_at_utilization: float = 0.95  # reported subscription window usage at which to switch away
    # Only these windows decide switching to another AI: overall usage. Model-specific or overage windows
    # (e.g. a Fable-only 7-day window) are shown but don't bench the whole provider — the stage's own
    # model fallback (Fable -> Opus) handles those.
    gate_windows: list[str] = ["five_hour", "seven_day"]
    tools: bool = False  # can edit files / run commands (needed by build stages)
    # cli login check: usable if any auth file holds credentials or any env var is set (both empty = no check)
    auth_files: list[str] = []
    auth_env: list[str] = []

    def logged_in(self) -> bool:
        if not self.auth_files and not self.auth_env:
            return True
        if any(os.environ.get(e) for e in self.auth_env):
            return True
        for f in self.auth_files:
            path = Path(os.path.expanduser(f))
            try:
                data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
            except (OSError, ValueError):
                continue
            if data:  # a non-empty JSON object/list means at least one credential
                return True
        return False

    def binary(self) -> str | None:
        return self.command[0] if self.kind == "cli" and self.command else None

    def availability(self) -> tuple[bool, str]:
        if not self.enabled:
            return False, "disabled"
        if self.kind == "claude_cli":
            return (shutil.which("claude") is not None, "claude CLI not found")
        if self.kind == "api":
            ok = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
            return ok, "ANTHROPIC_API_KEY not set"
        if self.kind == "cli":
            if shutil.which(self.binary() or "") is None:
                return False, "미설치"
            return self.logged_in(), "설치됨 · 로그인 필요"
        if self.kind == "openai":
            if self.api_key_env and not os.environ.get(self.api_key_env):
                return False, f"{self.api_key_env} not set"
            return bool(self.base_url), "base_url not set"
        return True, ""

    def resolve_model(self, model: str | None) -> str | None:
        if not model:
            return None
        if self.kind in ("claude_cli", "api", "mock"):
            return model
        return self.model_map.get(model, model if model not in ("haiku", "sonnet", "opus", "fable") else None)


# Presets. CLI flags for codex / opencode / gemini follow their public docs at the time of writing
# and are not verified on this machine (none installed) — adjust in relay.config.yaml if they differ.
BUILTIN: dict[str, dict] = {
    "mock": {"kind": "mock", "label": "Mock"},
    "claude": {"kind": "claude_cli", "label": "Claude Code", "tools": True},
    "anthropic_api": {"kind": "api", "label": "Claude API"},
    "codex": {
        "kind": "cli", "label": "Codex CLI", "tools": True,
        "command": ["codex", "exec", "--skip-git-repo-check", "--sandbox", "workspace-write",
                    "--cd", "{cwd}", "--output-last-message", "{out_file}", "-"],
        "model_args": ["-m", "{model}"], "auth_files": ["~/.codex/auth.json"], "auth_env": ["OPENAI_API_KEY"],
    },
    "opencode": {  # flags verified against opencode 1.18 `opencode run --help`
        "kind": "cli", "label": "OpenCode", "tools": True,
        "command": ["opencode", "run", "--dir", "{cwd}", "--auto"], "model_args": ["-m", "{model}"],
        "prompt_via": "arg", "auth_files": ["~/.local/share/opencode/auth.json"],
    },
    "gemini": {
        "kind": "cli", "label": "Gemini CLI", "tools": True,
        "command": ["gemini", "--yolo"], "model_args": ["-m", "{model}"], "prompt_via": "arg_p",
        "auth_files": ["~/.gemini/oauth_creds.json"], "auth_env": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
    },
    "openai": {"kind": "openai", "label": "OpenAI API", "base_url": "https://api.openai.com/v1",
               "api_key_env": "OPENAI_API_KEY"},
    "openrouter": {"kind": "openai", "label": "OpenRouter", "base_url": "https://openrouter.ai/api/v1",
                   "api_key_env": "OPENROUTER_API_KEY"},
    "ollama": {"kind": "openai", "label": "Ollama (local)", "base_url": "http://localhost:11434/v1", "enabled": False},
}

# Stage `runner:` values from before providers existed map onto providers.
LEGACY_RUNNER = {"claude_cli": "claude", "api": "anthropic_api", "mock": "mock"}


class ProviderRegistry:
    def __init__(self, overrides: dict[str, dict] | None = None, fallback_chain: list[str] | None = None,
                 usage=None, history=None):
        self.specs: dict[str, ProviderSpec] = {}
        for name, preset in BUILTIN.items():
            self.specs[name] = ProviderSpec(name=name, **(preset | (overrides or {}).get(name, {})))
        for name, data in (overrides or {}).items():
            if name not in BUILTIN:
                self.specs[name] = ProviderSpec(name=name, **data)
        self.fallback_chain = fallback_chain or []
        self.usage = usage
        self.history = history

    def get(self, name: str) -> ProviderSpec:
        if name not in self.specs:
            raise KeyError(f"unknown provider: {name}")
        return self.specs[name]

    def status(self, name: str) -> dict:
        spec = self.get(name)
        ok, reason = spec.availability()
        state = {"name": name, "label": spec.label or name, "kind": spec.kind, "available": ok,
                 "reason": "" if ok else reason, "tools": spec.tools}
        if self.history is not None:
            now = datetime.now(timezone.utc).timestamp()
            limits = []
            for row in self.history.limits(name):
                resets = row["resets_at"]
                fresh = resets is None or resets > now  # a window past its reset time no longer applies
                gating = not spec.gate_windows or row["window"] in spec.gate_windows
                limits.append({**row, "utilization": row["utilization"] if fresh else 0.0, "stale": not fresh,
                               "gating": gating})
                if ok and state["available"] and fresh and gating and (row["utilization"] or 0) >= spec.switch_at_utilization:
                    reset_txt = datetime.fromtimestamp(resets).strftime("%m-%d %H:%M") if resets else "?"
                    state.update(available=False,
                                 reason=f"{row['window']} 사용률 {row['utilization']:.0%} — {reset_txt} 초기화")
            state["limits"] = limits
            until = self.history.provider_exhausted_until(name)
            if ok and state["available"] and until:
                state.update(available=False, reason=f"사용량 한도 — {until} 까지 대기", exhausted_until=until)
        if spec.daily_usd is not None and self.usage is not None:
            since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
            spent = self.usage.cost_since(name, since)
            state.update(spent_24h_usd=round(spent, 4), daily_usd=spec.daily_usd)
            if ok and state["available"] and spent >= spec.daily_usd:
                state.update(available=False, reason=f"24시간 예산 ${spec.daily_usd} 소진 (${spent:.2f})")
        return state

    def candidates(self, primary: str, alternates: list[dict], needs_tools: bool) -> tuple[list[dict], list[dict]]:
        """Ordered usable options [{provider, model?, effort?}] and the skipped ones with reasons."""
        chain = [{"provider": primary}] + list(alternates) + [{"provider": p} for p in self.fallback_chain]
        seen, usable, skipped = set(), [], []
        for option in chain:
            name = option["provider"]
            if name in seen or name not in self.specs:
                continue
            seen.add(name)
            st = self.status(name)
            if needs_tools and not self.specs[name].tools and self.specs[name].kind != "mock":
                skipped.append({**option, "reason": "파일 편집 도구 없음"})
            elif not st["available"]:
                skipped.append({**option, "reason": st["reason"]})
            else:
                usable.append(option)
        return usable, skipped

    def mark_exhausted(self, name: str, reason: str) -> str | None:
        if self.history is None:
            return None
        minutes = self.specs[name].cooldown_min if name in self.specs else 60
        until = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(timespec="seconds")
        self.history.set_provider_exhausted(name, until, reason)
        return until


# Specific phrases only. Bare "429", "quota" or "billing" also appear in ordinary code and answers,
# and a false match would bench a working provider for its whole cooldown.
QUOTA_RE = re.compile(
    r"usage limit|limit (?:has been )?reached|rate[ _-]?limit(?:ed)?\b|too many requests|insufficient_quota"
    r"|out of (?:extra )?usage|hit your (?:usage )?limit|quota exceeded|exceeded your (?:current )?quota"
    r"|credit balance is too low|out of credits",
    re.IGNORECASE,
)


def looks_like_quota(text: str) -> bool:
    return bool(QUOTA_RE.search(text or ""))
