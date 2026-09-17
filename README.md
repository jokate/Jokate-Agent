# ⚡ Agent 카태

A relay agent server for programmers. Instead of one long conversation, **work passes from stage to stage like a baton in a relay**. Each stage receives only the **HANDOFF**, not the previous transcript.

```
Recommended: quick (single Sonnet stage)      big jobs: default
  build(Sonnet) → patch                         scout(Haiku) → plan(Fable, approval) → build(Sonnet → Fable on send-back) → review(Sonnet)
Claude near its usage limit → Codex/OpenCode/Gemini · run budget exceeded → pause for approval (not a failure)
```

## Windows: one-click
| File | What it does |
|---|---|
| `install.bat` | **New machine: grab just this file and run it** → clone into `Jokate-Agent` under **the folder where you ran it** (or a folder given as an argument), then run setup.bat. If run inside an existing clone, installs in place |
| `setup.bat` | Check git/uv/Claude CLI (offers to install uv via winget) → `uv sync` → create local config → `doctor` → prompt to register repos → offer to start |
| `start.bat [port]` | Start the server and open the dashboard (if already running, just opens the dashboard) |
| `update.bat` | `git pull --ff-only` → `uv sync` → `doctor` (stops if there are uncommitted changes) |

`KATAE_NONINTERACTIVE=1` skips the prompts; `KATAE_NO_BROWSER=1` doesn't open the browser.

## Running it (including a new machine)

```bash
git clone <this repo> && cd relay-agent
uv sync
uv run relay doctor                 # check git, Claude CLI login, AIs, config paths (no AI calls)
uv run relay serve                  # dashboard http://127.0.0.1:8020
```
- Machine-specific settings go in **`relay.config.local.yaml`** (git-ignored): `docs_root`, `extra_allowed_tools`, `auth_token`, and so on.
- The target machine needs **Claude Code installed and logged in** (`claude` once).
- To reach it from another device: set a token, then `uv run relay serve --host 0.0.0.0`. Without a token, non-localhost access is refused. The dashboard asks for the token once.
  ```bash
  KATAE_TOKEN=<random string> uv run relay serve --host 0.0.0.0
  ```
- Claude Code on another PC → katae MCP: set `KATAE_URL=http://<server>:8020` and `KATAE_TOKEN`. The conversation summary is **extracted on the PC you're working from** and only the summary is sent. The `workdir` must be a path on the server machine.

Terminal:
```bash
uv run relay run quick "goal" --workdir <folder>          # recommended
uv run relay run default "goal" --session <session_id>    # big jobs (includes design approval)
uv run relay approve|cancel|resume|status|log|patch|apply <run_id>
uv run relay sessions | history <session_id> | search "cooldown" | usage [run_id] | providers [--probe]
```

## Registering repositories
Register each working repo **once by name**; after that, give only the name.
```bash
uv run relay repo add mnys C:/.../MNYS --workspace inplace --docs Docs --notes "UE5 C++/GAS ..."
uv run relay repo add comact2-quiz C:/.../comact2-quiz --workspace copy --verify "python tools/build_questions.py"
uv run relay repos                                   # path · branch · uncommitted count
uv run relay run quick "goal" --repo comact2-quiz
uv run relay repo clone mnys --url <git url>          # on a new machine: clone + register
```
- **Applied automatically:** path, default workspace (copy/inplace), default relay, **verify commands** (pre-approved and given to the AI → no discovery or retry turns), docs-read root, short notes (≤400 chars), extra excludes.
- **Where settings live:** shared rules in `relay.config.yaml`, **this machine's `path` in `relay.config.local.yaml`** (the same repo can have a different path per machine).
- **Sessions:** a session remembers its repo, so follow-up questions don't need the name. Read-only relays (docs-qa) never create a workspace.
- **Remote requests** (token access) can only use paths **inside registered repos**.
- katae MCP: `katae_repos()`, `katae_start(goal, repo="mnys")`. With no repo given, it auto-matches from the current folder.

## Reading results (summary · checklist · diagrams · stop handoff)
- **Summary** (dashboard default tab, `relay status`, `katae_status`): one line + up to 3 key actions per stage + changed files + verify commands + MCP/tool usage + cost.
- **User checklist:** only what the AI can't verify itself, as checkboxes (state saved per browser).
- **Diagrams:** only when a flow or structure changed, the AI draws Mermaid and the dashboard renders it. Stage prompts never carry diagrams or highlights (no token cost).
- **Handoff on stop:** on cancel, failure, approval wait, or budget limit, a `## Stop point` section is added to `HANDOFF.md` automatically (stage stopped at, done/remaining stages, **what that stage already did**, how to resume). No AI call.
- **MCP recording:** `mcp_call` (server.tool + arguments) and `mcp_result` (result size); oversized tool results are flagged as `tool_result_large`.
- **Timeline:** tool calls fold into per-stage counts (click to expand). In the terminal, `relay log` is compact and `--full` shows everything.

## Large game projects (Unreal/Unity, SVN/git, assets included) — no snapshots
`workspace: inplace` **never copies or snapshots the project.**

| Step | What it does | Cost |
|---|---|---|
| 1. Journal | At run start, record only **size and modified time** of every file (code **and assets**). VCS metadata (`.svn` `.git`) and pure build/cache folders (`Intermediate Saved DerivedDataCache Binaries`) are skipped | Measured: **117K files / 13.6GB → 8.1s, record 1.7MB** |
| 2. Pre-edit backup | A **PreToolUse hook** in Claude Code copies a file **right before** Edit/Write changes it | Only files that were edited |
| 3. Uncommitted at start | Files already modified or unversioned when the run starts are copied (256MB per file / 2GB total cap) | Only uncommitted work |
| 4. Originals | Anything else that changed (Bash, **assets saved by the Unreal editor via MCP**, builds) is restored from the VCS's own original: **git HEAD, SVN BASE (read straight from `.svn/pristine`, no svn CLI needed)** | 0 |
| 5. Result | Text → `result.patch`; **binaries/assets → change list** (size before/after, restorable or not) | |
| 6. Roll back | Restores every restorable change, deletes files created during the run, reports the rest | |

- The repo itself is never written to (git status runs with `--no-optional-locks`, so even the index is untouched).
- **Assets aren't cached here**: `.uasset .umap .fbx .png .wav …` changed by the Unreal editor/MCP are **only listed, never copied**. Rollback uses only the VCS's original (git HEAD / SVN BASE); **git LFS pointers are never written back**, and assets that were already uncommitted at run start can't be restored.
- **The `Engine` folder isn't scanned** (touching an engine source tree would blow up the scan). There, only files the AI edits with Edit/Write are backed up by the hook, so Bash/build changes aren't tracked. Change it per repo with `hook_only: [Engine, ThirdParty]`.
- If there's no VCS or it can't be read, only hook-backed-up files can be restored; everything else shows as "no original".
- Roll back after closing the Unreal editor (or reload the assets) — files the editor holds open may fail to restore.
- Real test (Claude): code edited with Edit + asset changed with Bash → hook backup, asset detected, rollback leaves `git status` clean.

**Copy mode** (`copy`) is only for small code repos. It uses a snapshot, with these limits:
- one shared store per repo, `.gitignore` respected;
- refused if over 500MB, and the error message recommends `inplace`.
- `uv run relay disk` / `uv run relay cleanup`.

## Dashboard layout and selections
- **Resizing**: drag the column dividers (double-click to reset), drag the divider between top and bottom panels to change height, use the header buttons to collapse the session/history columns or enlarge the result view (⛶). Sizes are remembered per browser.
- **Choose paths instead of typing**: the new session and repo registration forms use a **📂 folder picker** (this PC only). Registered repos are chosen from a dropdown.
- **Relays** are shown by role and model tier (lightweight/standard/advanced/top), not vendor names; relays that support automatic switching show "switches to another AI when usage runs low".

## How results are applied (workspace) — default: immediate
| Mode | Behavior | Fits |
|---|---|---|
| **`inplace` (default)** | **Changes apply to the original at each step.** Only changed files are recorded, so rollback is possible | Game projects, anything that uses MCP (Unreal editor) |
| `copy` | Work in a copy → **applied automatically when done** (`auto_apply: true`). If the original changed meanwhile, it isn't applied and waits in the "Changes" tab | Small code repos you want isolated |
| `none` | Read-only | docs-qa |

- **Stages using an MCP that changes real state (e.g. Unreal) always run `inplace`, even if you choose `copy`.** MCP changes the real project, so a copy would split the results. (Read-only MCPs like `docs_read` are exempt.)
- `relay patch|rollback <run_id>` or the dashboard "Changes" tab. Rollback restores both code and assets.

## Multiple AIs and automatic switching
| Provider | Type | Status (this PC) | Remaining usage shown |
|---|---|---|---|
| claude | Claude Code CLI | ✅ | **5h / 7d utilization %** (from `rate_limit_event`) |
| codex | Codex CLI | Not installed | Captures `rate_limits` when present in output (unverified) |
| opencode | OpenCode CLI | Installed · **login needed** (`opencode auth login`) | 24h spend on this server |
| gemini | Gemini CLI | Not installed | 24h spend |
| anthropic_api / openai / openrouter / ollama | API | No key | 24h spend |

Switching rules (per stage, in order: primary → `alternates` → `fallback_chain`):
1. **Before calling:** skip if not installed or not logged in; if Claude's reported window is **95%+ (`switch_at_utilization`)**; if 24h spend exceeds `daily_usd`; if a build stage needs file-editing tools this provider lacks.
2. **During a call:** on a usage/rate-limit error, mark the provider exhausted (`cooldown_min`) and **rerun the same stage on the next AI with the same HANDOFF**.
3. Within Claude, model fallback first (Fable → Opus). `optional: true` stages (e.g. cross_review) are skipped when no AI is available.
- Token cost of switching: the next AI receives only the baton, so the added context is a few thousand tokens. External CLIs don't report token counts, so they're **estimated from text length** (marked `estimated`).
- Refreshing Claude usage: click the Claude chip in the top strip, or `relay providers --probe` (one Haiku call, a few hundred tokens).

## Continuing from Claude Code
- **Import:** dashboard "Import Claude Code conversation" or `relay import-cc --cwd <folder>`. Keeps **only questions plus a 400-char answer summary** (tool output, thinking, system messages dropped). A 7.6MB conversation → dozens of lines. Re-importing adds only new questions.
- **From inside Claude Code:** register the katae MCP to start relays, check status, approve, and apply patches from within a conversation.
  ```bash
  claude mcp add katae -- C:/Users/kkkk4017/Projects/relay-agent/.venv/Scripts/python.exe C:/Users/kkkk4017/Projects/relay-agent/mcp_servers/katae.py
  ```
  `katae_start(goal, import_this_conversation=True)` → imports this folder's latest conversation into a session, then starts the relay. Also `katae_providers` / `katae_status` / `katae_approve` / `katae_patch` / `katae_apply` / `katae_cancel`.

## Reliability
- **Cancel:** kills the running AI process immediately (`/runs/{id}/cancel`, dashboard ⛔). Resumable afterwards.
- **Server restart:** runs left "running" become "interrupted" on startup → resume re-runs from the stopped stage. Half-built workspaces are rebuilt.
- **Unexpected errors:** recorded as failures; runs are never left "running" forever.

## Sessions, history, and activity log
- **Session:** a line of work. Every question is saved as a turn (question, relay, status, result summary).
- **Continuing a session:** a new question inherits one-line summaries of the last 8 questions (160 chars each). Full transcripts are never carried over.
- **Activity log:** stage start and finish, every tool call (Read/Grep/Edit/Bash plus target), model switches, send-backs, approval waits, and budget limits. All stored in SQLite and shown live on the dashboard timeline.
- **History search:** search questions and results (`/history/search?q=` or the dashboard search bar).

## Model policy (token cost first)
| Relay | Stages / models | Use for |
|---|---|---|
| **quick** (dashboard default) | Single Sonnet stage (→ Opus → other AIs) | Most edits and fixes |
| quick-fable | Single Fable stage | Small but hard problems |
| default | scout Haiku → plan **Fable** (approval) → build Sonnet (**Fable on send-back**) → review Sonnet | Large or risky jobs |
| docs-qa | Single Sonnet(low) stage + docs-read MCP | Document questions |

## Token savings — measurements (real calls, same task)
| Change | Before → after |
|---|---|
| MCP off by default (`--strict-mcp-config`) | 184K → 7.5K (one-line question) |
| System prompt replaced (`system_mode: replace`) | 7.5K → 0.6K (one-line question) · **build stage 118K → 46K** |
| Split stages merged for small jobs (docs-qa) | 63K / $0.09 → **31K / $0.047** (same answer) |
| No retrying blocked commands + common test commands pre-allowed | **121K → 56K** (the denial happened 4 times) |
| Required result fields trimmed to 4 (fewer format errors) | removes 2 extra turns per error |
| Scout records the verification command + review runs tests only once | removes redundant verification turns |

Other policies applied:
- Stages pass only the baton; each reads only its `reads_outputs`. Prompt caps: outputs 3,000 chars, last 10 log lines, last 8 decisions, last 15 pointers, 8 session questions at 160 chars each.
- Expensive models and effort are raised only on a redo (`retry_model`/`retry_effort`); `max_retries: 1`; no extra stages like cross review in the defaults.
- Guards: per-stage `max_budget_usd`, per-run `max_run_cost_usd` (pause when exceeded), 24h `daily_usd` per AI.
- Add this machine's test/build commands to `extra_allowed_tools` (headless runs can't approve commands, and denials burn tokens on retries).

Deliberately not applied: `--bare` (needs an API key), `--resume` between stages (breaks the baton design), 1h TTL, compaction (stages are short), inflating prompts to meet the minimum cache length.

## Structure
`relay_agent/`: `baton.py` baton · `pipeline.py` engine · `runners.py` claude_cli/api/mock · `history.py` sessions, questions, events · `usage.py` token/cost log · `server.py` API · `dashboard.html` UI · `cli.py`
`mcp_servers/`: `docs_read.py`, `handoff.py` · `relays/`: default, quick, docs-qa, demo (mock, free) · `prompts/`: stage role prompts

## Roadmap
1. Real-call verification of the Codex / Gemini presets (after install), OpenCode login and a real run
2. Parallel fan-out stage (per-file Haiku workers → merge)
3. Unreal tool gateway MCP (expose 800 tools through search only)
4. Telegram/Discord notifications for approval waits and usage-limit switches
5. Reuse scout results within a session (same goal and files unchanged)
