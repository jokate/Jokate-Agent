"""PreToolUse hook: the studio guard (design/studio/ARCHITECTURE.md §10).

    python studio_guard.py <guard_ctx.json>        env: STUDIO_URL, STUDIO_ACTOR_TOKEN

Claude Code runs this before every tool call of a studio run and passes the call as JSON on stdin. The guard
refuses the block list (§10.3) on the spot, asks the server about the ask list (§10.4), and refuses what it
cannot read (`unparsed`) or cannot judge (`guard_error`). Pass = exit 0 with no output; refusal = exit 2 with
the sentence on stderr (§10.6). It fails closed — the opposite of backup_hook.py.

Standard library only, no `relay_agent` import: the hook is a bare process. The server may import this module
(`default_secrets`, `Verdict`), never the other way round.
"""

from __future__ import annotations

import base64
import fnmatch
import ipaddress
import json
import ntpath
import os
import re
import socket
import sys
import time as _time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime as _datetime
from urllib.parse import urlsplit

try:
    import msvcrt  # Windows: an append is a seek and then a write, so the audit log takes turns (`_hold`)
except ImportError:  # elsewhere O_APPEND appends in one step
    msvcrt = None

BLOCK_RULES = ("disk_format", "system_delete", "secret_access", "security_config", "server_control",
               "protected_write", "payment_account")  # §10.3, in table order
ASK_RULES = ("git_risky", "outside_delete", "install_env", "power_state", "net_transfer")  # §10.4, in table order
SHELL_TOOLS = {"Bash": "bash", "PowerShell": "ps"}
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
MAX_DEPTH = 5        # nested shells (§10.2 의 2)
ASK_TIMEOUT = 5.0    # seconds to wait for POST /studio/guard/check (§10.6)
HEAD_TIMEOUT = 3.0   # seconds to wait for the size of a download (§10.4 의 5)
GB = 1024 ** 3

KNOWN_ENV = ("USERPROFILE", "HOME", "TEMP", "TMP", "TMPDIR", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES",
             "PROGRAMFILES(X86)", "PROGRAMW6432", "PROGRAMDATA", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE")
SERVER_IMAGES = ("python", "pythonw", "python3", "py", "uv", "relay")  # killing these by name kills the server
RELAY_BLOCKED = ("serve", "remote", "autostart")  # `relay` subcommands that start the server or change its setup

PAY_HOSTS = ("*.stripe.com", "*.paypal.com", "*.tosspayments.com", "*.iamport.kr", "*.portone.io", "*.squareup.com",
             "*.braintreegateway.com", "*.adyen.com", "*.wise.com", "*.transferwise.com", "*.kakaopay.com",
             "*.coinbase.com", "*.binance.com", "*.upbit.com", "*.bithumb.com")
PAY_CLI = (("stripe", "charges"), ("stripe", "payment_intents"), ("stripe", "payouts"), ("stripe", "transfers"),
           ("stripe", "refunds"), ("stripe", "subscriptions"), ("stripe", "invoices"), ("stripe", "checkout"),
           ("stripe", "accounts"), ("stripe", "post"), ("stripe", "trigger"), ("paypal",),
           ("aws", "organizations", "create-account"), ("aws", "iam", "create-user"), ("gcloud", "billing"),
           ("az", "account", "create"), ("az", "ad", "user", "create"), ("npm", "adduser"), ("heroku", "signup"),
           ("firebase", "projects:create"))

DELETE = {"rm", "del", "erase", "rd", "rmdir", "ri", "remove-item", "unlink", "shred", "rimraf"}
MOVE = {"mv", "move", "move-item", "mi", "ren", "rename", "rename-item", "rni"}
COPY = {"cp", "copy", "copy-item", "cpi", "xcopy", "robocopy", "scp", "rsync", "install"}
WRITE = {"tee", "tee-object", "out-file", "set-content", "add-content", "clear-content", "ac", "clc", "new-item",
         "ni", "touch", "truncate", "mkdir", "md", "export-csv", "icacls", "attrib", "takeown", "chmod", "chown",
         "ln", "mklink", "set-acl"}
READ = {"cat", "type", "get-content", "gc", "more", "less", "head", "tail", "strings", "xxd", "od", "hexdump",
        "base64", "certutil", "findstr", "grep", "egrep", "fgrep", "rg", "select-string", "sls", "awk", "sed", "sort",
        "wc", "cut", "diff", "cmp", "nl", "tac", "tar", "zip", "unzip", "7z", "7za", "compress-archive",
        "expand-archive", "get-childitem", "gci", "dir", "ls", "find", "tree", "du", "stat", "source", "dd",
        "get-filehash", "format-hex", "import-csv", "openssl", "gpg"}
FETCH = {"curl", "wget", "invoke-webrequest", "iwr", "invoke-restmethod", "irm", "start-bitstransfer", "bitsadmin",
         "aria2c"}
SEND = {"scp", "sftp", "rsync", "rclone", "aws"}
KILL = {"taskkill", "stop-process", "spps", "kill", "pkill", "killall", "tskill"}
PATH_CMDS = DELETE | MOVE | COPY | WRITE | READ | FETCH | KILL | {"sftp", "rclone"}
ALWAYS_DEEP = {"rg", "find", "tree", "robocopy", "xcopy", "tar", "zip", "7z", "7za", "compress-archive", "rsync",
               "du"}
DEEP_IF_FLAG = {"grep", "egrep", "fgrep", "ls", "cp", "copy-item", "cpi", "scp", "get-childitem", "gci", "dir",
                "findstr", "copy", "select-string", "sls"}
WIN_NATIVE = {"del", "erase", "rd", "taskkill", "robocopy", "xcopy", "findstr", "dir", "msiexec", "shutdown", "reg",
              "regedit", "net", "net1", "sc", "format", "wmic", "netsh", "cmd", "tskill", "psshutdown", "setx",
              "bitsadmin", "start", "copy", "move", "ren", "type", "tree", "attrib", "icacls", "takeown", "mklink"}
SHELLS = {"bash": "bash", "sh": "bash", "zsh": "bash", "dash": "bash", "ksh": "bash", "cmd": "cmd",
          "powershell": "ps", "pwsh": "ps"}
PREFIXES = {"sudo": ("-u", "-g", "-h", "-p"), "doas": ("-u",), "nohup": (), "time": (), "command": (), "exec": (),
            "builtin": (), "call": (), "winpty": (), "busybox": (), "nice": ("-n",), "stdbuf": ()}
SETENV = re.compile(r"setenvironmentvariable\s*\([^)]*['\"](user|machine)['\"]", re.I)
URL = re.compile(r"(?i)\b(https?|wss?|ftp)://(?:[^/@\s'\"]+@)?(\[[0-9a-f:.]+\]|[a-z0-9._~%-]+)(?::(\d{1,5}))?")
HOSTPORT = re.compile(r"(?i)(?<![\w/.\\-])(\[[0-9a-f:]+\]|[a-z0-9.-]+):(\d{2,5})(?!\w)")
FILE_SECRET = re.compile(r"\.(json|ya?ml|db|sqlite3?|txt|key|pem|ini|toml)$", re.I)


class Unparsed(Exception):
    """The command could not be read (§10.2 의 5). Refused."""


class GuardError(Exception):
    """The guard itself could not judge (§10.5 의 2). Refused."""


@dataclass
class Tok:
    text: str            # quotes and escapes removed, known variables expanded
    dyn: bool = False    # holds a variable or substitution the guard could not expand
    quoted: bool = False


@dataclass
class Seg:
    toks: list = field(default_factory=list)
    redirs: list = field(default_factory=list)  # (">" | ">>" | "<" | "<<<", Tok)
    piped: bool = False                          # stdin is the segment before
    heredoc: str | None = None


@dataclass
class Cmd:
    name: str
    args: list
    seg: Seg
    dialect: str         # bash | ps | cmd | tool (a tool call that is not a shell)
    head: str = ""       # the name as written
    cwd: object = None   # the folder this command runs in: a path, None (= the run's cwd), or BROKEN_CWD


@dataclass
class Verdict:
    """One judgement. T5's audit log (§10.7) writes these fields as they are."""
    verdict: str                 # allow | block | ask
    rule_id: str | None = None
    outcome: str = "allowed"     # allowed | denied
    message: str = ""
    server: dict | None = None   # ask only: the server's answer, or {"error": ...}
    tool: str = ""
    command: str = ""


# ---- 1. splitting a command line into segments (§10.2 의 1) ----

def _close(text: str, i: int, opener: str = "(", closer: str = ")") -> int:
    """Index of the bracket closing the one just before `i`."""
    depth, quote = 1, ""
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 1
            elif c == quote:
                quote = ""
        elif c in "'\"":
            quote = c
        elif c == "\\":
            i += 1
        elif c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise Unparsed("unbalanced bracket")


def scan(text: str, dialect: str, env: dict) -> tuple[list, list]:
    """Segments of one command line, and the bodies found inside `$( )`, backticks and `<( )`."""
    segs, inner = [], []
    state = {"seg": Seg(), "buf": [], "has": False, "dyn": False, "quoted": False, "redir": None}
    heredocs: list = []
    n = len(text)

    def put(s: str, dyn: bool = False) -> None:
        state["buf"].append(s)
        state["has"] = True
        state["dyn"] = state["dyn"] or dyn

    def flush() -> None:
        if state["has"]:
            tok = Tok("".join(state["buf"]), state["dyn"], state["quoted"])
            if state["redir"]:
                state["seg"].redirs.append((state["redir"], tok))
                state["redir"] = None
            else:
                state["seg"].toks.append(tok)
        state.update(buf=[], has=False, dyn=False, quoted=False)

    def end(piped: bool = False) -> None:
        flush()
        seg = state["seg"]
        if seg.toks or seg.redirs or seg.heredoc is not None:
            segs.append(seg)
        state["seg"] = Seg(piped=piped)

    def lookup(name: str, raw: str) -> None:
        key = name.upper()
        val = env.get(key) or (env.get("USERPROFILE") if key == "HOME" else None)
        if key in KNOWN_ENV and val:
            put(val)
        else:
            put(raw, dyn=True)

    def dollar(i: int) -> int:
        """A `$…` in bash or PowerShell. Returns the index after it."""
        nxt = text[i + 1] if i + 1 < n else ""
        if nxt == "(":
            j = _close(text, i + 2)
            inner.append((text[i + 2:j], dialect))
            put(text[i:j + 1], dyn=True)
            return j + 1
        if nxt == "{":
            j = text.find("}", i)
            if j < 0:
                raise Unparsed("unbalanced brace")
            name = text[i + 2:j]
            name = name[4:] if name.lower().startswith("env:") else name
            if re.fullmatch(r"[A-Za-z_][\w()]*", name):
                lookup(name, text[i:j + 1])
            else:
                put(text[i:j + 1], dyn=True)
            return j + 1
        m = re.compile(r"(?i)(env:)?([A-Za-z_]\w*)").match(text, i + 1)
        if m:
            word = m.group(2)
            if dialect == "ps" and not m.group(1) and word.lower() in ("true", "false", "null"):
                put(text[i:m.end()])
            elif dialect == "ps" and not m.group(1) and word.lower() != "home":
                put(text[i:m.end()], dyn=True)
            else:
                lookup(word, text[i:m.end()])
            return m.end()
        specials = "?@*#$!-0123456789" if dialect == "bash" else "_?$^"
        if nxt in specials:
            put(text[i:i + 2], dyn=True)  # $? $1 $@ $_ …
            return i + 2
        put("$")  # a literal `$`: the `c$` of an admin share, `$5.00`, a trailing `$`
        return i + 1

    def quote(i: int) -> int:
        """A quoted run starting at `i`. Returns the index after the closing quote."""
        q = text[i]
        state["quoted"] = state["has"] = True
        i += 1
        while i < n:
            c = text[i]
            if c == q:
                if dialect == "ps" and text[i + 1:i + 2] == q:  # '' and "" are one quote
                    put(q)
                    i += 2
                    continue
                return i + 1
            if q == '"' and dialect == "bash" and c == "\\" and text[i + 1:i + 2] in ('"', "$", "\\", "`", "\n"):
                put(text[i + 1] if text[i + 1] != "\n" else "")
                i += 2
            elif q == '"' and dialect == "ps" and c == "`" and i + 1 < n:
                put(text[i + 1])
                i += 2
            elif q == '"' and dialect in ("bash", "ps") and c == "$":
                i = dollar(i)
            elif q == '"' and dialect == "bash" and c == "`":
                i = backtick(i)
            elif q == '"' and dialect == "cmd" and c == "%":
                i = percent(i)
            else:
                put(c)
                i += 1
        raise Unparsed("unbalanced quote")

    def backtick(i: int) -> int:
        j = text.find("`", i + 1)
        if j < 0:
            raise Unparsed("unbalanced backtick")
        inner.append((text[i + 1:j], "bash"))
        put(text[i:j + 1], dyn=True)
        return j + 1

    def percent(i: int) -> int:
        m = re.compile(r"%([A-Za-z_][\w()]*)%").match(text, i)
        if m:
            lookup(m.group(1), m.group(0))
            return m.end()
        m = re.compile(r"%%?~?[A-Za-z0-9*]").match(text, i)  # %i, %%i, %1, %~dp0
        if m:
            put(m.group(0), dyn=True)
            return m.end()
        put("%")
        return i + 1

    i = 0
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if c in "\r\n":
            end()
            i += 1
            while heredocs and c == "\n":
                delim, seg, strip = heredocs.pop(0)
                lines = []
                while i < n:
                    j = text.find("\n", i)
                    j = n if j < 0 else j
                    line = text[i:j].rstrip("\r")
                    i = j + 1
                    if (line.lstrip("\t") if strip else line) == delim:
                        break
                    lines.append(line)
                seg.heredoc = "\n".join(lines)
        elif c in " \t":
            flush()
            i += 1
        elif c == "'" and dialect != "cmd" or c == '"':
            i = quote(i)
        elif c == "\\" and dialect == "bash":
            if nxt in "\r\n":
                i += 3 if text[i + 1:i + 3] == "\r\n" else 2
            elif nxt and (nxt.isspace() or nxt in ";&|<>()$`\"'*?[]{}!#~=\\"):
                put(nxt)   # an escaped character
                i += 2
            else:
                put(c)     # kept: C:\Windows stays a path; canon_name drops it from r\m
                i += 1
        elif c == "`" and dialect == "ps" or c == "^" and dialect == "cmd":
            if nxt not in "\r\n":
                put(nxt)
            i += 2
        elif c == "`" and dialect == "bash":
            i = backtick(i)
        elif c == "$" and dialect in ("bash", "ps"):
            i = dollar(i)
        elif c == "%" and dialect == "cmd":
            i = percent(i)
        elif c == "!" and dialect == "cmd" and re.compile(r"!\w+!").match(text, i):
            j = text.find("!", i + 1)
            put(text[i:j + 1], dyn=True)
            i = j + 1
        elif c == "#" and dialect in ("bash", "ps") and not state["has"]:
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif c == ";":
            end() if dialect != "cmd" else flush()
            i += 1
        elif c == "&":
            if nxt == ">" and dialect == "bash":
                flush()
                state["redir"] = ">"
                i += 3 if text[i + 2:i + 3] == ">" else 2
            elif dialect == "ps" and nxt != "&" and not state["has"] and not state["seg"].toks:
                i += 1  # the call operator
            else:
                end()
                i += 2 if nxt == "&" else 1
        elif c == "|":
            end(piped=nxt != "|")
            i += 2 if nxt in "|&" else 1
        elif c in "<>" and nxt == "(" and dialect == "bash":
            j = _close(text, i + 2)
            inner.append((text[i + 2:j], "bash"))
            put(text[i:j + 1], dyn=True)
            i = j + 1
        elif c in "()" or c in "{}" and (dialect == "ps" or dialect == "bash" and not state["has"]
                                         and (c == "}" or not nxt or nxt.isspace())):
            end()
            i += 1
        elif c == ">":
            if state["has"] and not state["quoted"] and re.fullmatch(r"\d+|\*", "".join(state["buf"])):
                state.update(buf=[], has=False)  # a file descriptor: 2> *>
            flush()
            op = ">>" if nxt == ">" else ">"
            i += len(op)
            if text[i:i + 1] == "&" and (text[i + 1:i + 2].isdigit() or text[i + 1:i + 2] == "-"):
                i += 2  # 2>&1
            else:
                i += 1 if text[i:i + 1] == "|" else 0
                state["redir"] = op
        elif c == "<":
            flush()
            if dialect == "bash" and text[i:i + 3] == "<<<":
                state["redir"] = "<<<"
                i += 3
            elif dialect == "bash" and nxt == "<":
                m = re.compile(r"<<(-?)\s*(['\"]?)([\w.-]+)\2").match(text, i)
                if not m:
                    raise Unparsed("heredoc")
                state["seg"].heredoc = ""
                heredocs.append((m.group(3), state["seg"], bool(m.group(1))))
                i = m.end()
            else:
                state["redir"] = "<"
                i += 1
        else:
            put(c)
            i += 1
    end()
    return segs, inner


# ---- 2. names and paths (§10.2 의 3 · 4) ----

def canon_name(text: str, dialect: str) -> str:
    """`C:\\Windows\\System32\\shutdown.exe` -> `shutdown`; in bash `r\\m` -> `rm`."""
    t = text.strip().lstrip("@")
    if dialect == "bash" and not re.match(r"^([A-Za-z]:\\|\.{1,2}\\|\\)", t):
        t = t.replace("\\", "")
    base = re.split(r"[\\/]", t)[-1].lower()
    for ext in (".exe", ".cmd", ".bat", ".com"):
        if base.endswith(ext):
            base = base[:-len(ext)]
    return base


def _resolve(path: str) -> str:
    """Follow 8.3 short names, symlinks and junctions of what exists."""
    try:
        real = os.path.realpath(path)
    except (OSError, ValueError):
        return path
    if real[:8].upper() == "\\\\?\\UNC\\":
        return "\\\\" + real[8:]  # \\?\UNC\server\share\x -> \\server\share\x (a mapped network drive)
    return real[4:] if real.startswith("\\\\?\\") else real


BROKEN_CWD = object()  # the cwd after `cd <variable>` · `cd -` · popd: relative paths can no longer be read
CD_NAMES = {"cd", "chdir", "pushd", "set-location", "sl"}


def _is_relative(raw: str) -> bool:
    """A path spelling that needs a cwd to read (not a drive · UNC · root · `~` · variable · URL)."""
    p = raw.strip().strip("\"'")
    return bool(p) and not re.match(r"(?i)^([A-Za-z]:|\\\\|[\\/]|~|%|\$|file:)", p)


def norm_path(raw: str, cwd: str, env: dict, resolve=None, dialect: str | None = None, local=()) -> str | None:
    """One spelling for a path: absolute, backslashes, no `.`/`..`. A glob stays in its component."""
    p = raw.strip().strip("\"'")
    if not p or len(p) > 600 or "://" in p and not p.lower().startswith("file://") or re.search(r'[<>|"\n]', p):
        return None
    low = p.lower()
    if low.startswith("file://"):
        p = p[7:].lstrip("/") if re.match(r"^file:///[A-Za-z]:", p) else p[7:]
    p = re.sub(r"(?i)^(microsoft\.powershell\.core\\)?filesystem::", "", p).replace("/", "\\")
    if p.lower().startswith("\\\\?\\unc\\"):
        p = "\\\\" + p[8:]
    for pre in ("\\\\?\\", "\\??\\", "\\\\.\\"):
        if p.startswith(pre) and re.match(r"^[A-Za-z]:", p[len(pre):]):
            p = p[len(pre):]
    share = re.match(r"^\\\\([^\\]+)\\([A-Za-z])\$(\\.*)?$", p)  # \\localhost\c$\… admin share of a local disk
    if share and _loopback(share.group(1), {x.lower() for x in local}):
        p = share.group(2).upper() + ":" + (share.group(3) or "\\")
    if dialect == "bash":
        temp = env.get("TEMP") or env.get("TMP")
        if temp:
            p = re.sub(r"(?i)^\\tmp(?=\\|$)", lambda _m: temp.rstrip("\\"), p)  # bash `/tmp` is the temp folder
        p = re.sub(r"(?i)^\\(?:proc\\)?cygdrive\\([A-Za-z])(?=\\|$)", lambda _m: _m.group(1).upper() + ":", p)
    home = env.get("USERPROFILE") or env.get("HOME") or ""
    if home and (p == "~" or p.startswith("~\\")):
        p = home + p[1:]
    m = re.match(r"^\\(?:mnt\\|cygdrive\\)?([A-Za-z])(\\.*)?$", p)  # /c/… /mnt/c/…
    if m:
        p = m.group(1).upper() + ":" + (m.group(2) or "\\")
    elif re.match(r"^[A-Za-z]:([^\\]|$)", p):
        p = p[:2] + "\\" + p[2:]  # C:x and C: are read as the root: the stricter side
    base = (cwd or "").replace("/", "\\")
    if p.startswith("\\") and not p.startswith("\\\\"):
        p = base[:2] + p if re.match(r"^[A-Za-z]:", base) else p  # `/` is the root of this drive
    elif not re.match(r"^([A-Za-z]:\\|\\\\)", p):
        p = base.rstrip("\\") + "\\" + p
    parts = ntpath.normpath(p).split("\\")
    keep = 3 if p.startswith("\\\\") else 1
    parts = parts[:keep] + [(c.split(":")[0].rstrip(". ") or c) for c in parts[keep:]]  # ADS, trailing dots
    p = "\\".join(parts)
    if len(p) == 2 and p[1] == ":":
        p += "\\"
    fixed = _fixed(p)
    if resolve and fixed:
        real = resolve(fixed)
        p = real.rstrip("\\") + p[len(fixed.rstrip("\\")):] if fixed != p else real
    return p


def _fixed(path: str) -> str:
    """The part of a path before its first glob component."""
    parts = path.split("\\")
    for i, comp in enumerate(parts):
        if re.search(r"[*?\[{]", comp):
            return "\\".join(parts[:i]) + ("\\" if i == 1 else "")
    return path


def _parts(path: str) -> list:
    return [re.sub(r"\{[^}]*\}", "*", c) for c in path.lower().replace("/", "\\").split("\\") if c]


def _same(a: str, b: str) -> bool:
    return a == b or fnmatch.fnmatchcase(a, b) or fnmatch.fnmatchcase(b, a)


def under(path: str, entry: str) -> bool:
    """`path` is `entry` or below it. Component by component, case-blind; `*` on either side matches."""
    a, b = _parts(path), _parts(entry)
    if "**" in a:  # what lies above the entry is the recursive-read rule's to judge
        a = a[:a.index("**")]
    return len(a) >= len(b) and all(_same(x, y) for x, y in zip(a, b))


def above(path: str, entry: str) -> bool:
    """`path` is `entry` or an ancestor of it."""
    a, b = _parts(path), _parts(entry)
    return len(a) <= len(b) and all(_same(x, y) for x, y in zip(a, b))


# ---- 3. inner shells and wrappers (§10.2 의 2) ----

SKIP = {"bash": {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "time"},
        "ps": {"if", "elseif", "else", "while", "do", "try", "catch", "finally", "foreach", "switch", "return", "."},
        "cmd": {"do", "else", "@"}}
DROP = {"for", "case", "select", "function", "esac", "done", "fi", "in"}
PS_VALUED = ("executionpolicy", "windowstyle", "version", "inputformat", "outputformat", "workingdirectory",
             "configurationname", "custompipename", "settingsfile", "psconsolefile")
SAPS_VALUED = ("verb", "workingdirectory", "windowstyle", "credential", "redirectstandardoutput",
               "redirectstandarderror", "redirectstandardinput")


def is_opt(text: str, name: str, dialect: str) -> bool:
    if dialect == "tool":
        return False
    if len(text) > 1 and text[0] == "-":
        return True
    m = re.fullmatch(r"/{1,2}([A-Za-z?]{1,12})([:=].*)?", text)
    if not m or dialect == "bash" and name not in WIN_NATIVE:
        return False
    return len(m.group(1)) <= 2 or name in WIN_NATIVE and name not in DELETE | MOVE  # `/Windows` stays a path


def opts(cmd: Cmd) -> list:
    return [a.text.lower() for a in cmd.args if is_opt(a.text, cmd.name, cmd.dialect)]


def pos(cmd: Cmd) -> list:
    return [a for a in cmd.args if not is_opt(a.text, cmd.name, cmd.dialect)]


def words(cmd: Cmd) -> list:
    return [a.text.lower() for a in pos(cmd)]


def cluster(cmd: Cmd, letters: str) -> bool:
    """`-rf`: a single-dash cluster holding one of the letters."""
    return any(re.fullmatch(r"-[a-z]+", o) and set(o[1:]) & set(letters) for o in opts(cmd))


def psflag(cmd: Cmd, name: str) -> bool:
    """`-Recurse`, `-rec`, `-r`: PowerShell takes any unambiguous prefix."""
    return any(o[0] == "-" and len(o) > 1 and name.startswith(o[1:].split(":")[0]) for o in opts(cmd))


def recursive(cmd: Cmd) -> bool:
    o = opts(cmd)
    return ("/s" in o or "--recursive" in o or psflag(cmd, "recurse")
            or cmd.name in ("rm", "cp", "grep", "egrep", "fgrep", "ls", "scp", "chmod", "chown")
            and cluster(cmd, "ra" if cmd.name == "cp" else "r"))


def _skip(args: list, valued: tuple = ()) -> list:
    """Drop leading options (and the values of those that take one)."""
    i = 0
    while i < len(args) and args[i].text.startswith("-") and len(args[i].text) > 1:
        i += 2 if args[i].text in valued else 1
    return args[i:]


def _line(toks: list) -> str:
    """Tokens back into one line for an inner shell."""
    if any(t.dyn for t in toks):
        raise Unparsed("the inner command holds a variable")
    if len(toks) == 1:
        return toks[0].text
    return " ".join(f'"{t.text}"' if not t.text or re.search(r"\s", t.text) else t.text for t in toks)


def _static(cmd: Cmd) -> None:
    """A command the path rules read must name its targets (§10.2 의 5). `echo $f` is not one of them."""
    if any(t.dyn for op, t in cmd.seg.redirs if op != "<<<"):
        raise Unparsed("redirection to a variable")
    if cmd.name not in PATH_CMDS:
        return
    skip = False
    for a in cmd.args:
        if skip:
            skip = False
        elif cmd.name in FETCH and a.text.lower() in ("-h", "--header", "-a", "--user-agent", "-u", "--user", "-x",
                                                      "--request", "-b", "--cookie"):
            skip = True
        elif a.dyn and not is_opt(a.text, cmd.name, cmd.dialect):
            raise Unparsed("the target is a variable")


def _stdin_body(seg: Seg, prev: Seg | None) -> str | None:
    """What a shell with no command reads: a heredoc, a here-string, or the echo piped into it."""
    for op, t in seg.redirs:
        if op == "<<<":
            if t.dyn:
                raise Unparsed("here-string holds a variable")
            return t.text
    if seg.heredoc is not None:
        return seg.heredoc
    if not seg.piped or prev is None:
        return None
    if prev.heredoc is not None:
        return prev.heredoc
    if prev.toks and canon_name(prev.toks[0].text, "bash") in ("echo", "printf", "write-output") \
            and not any(t.dyn for t in prev.toks):
        return " ".join(t.text for t in prev.toks[1:])
    raise Unparsed("a shell reading a pipe")


def _newcwd(cur, text: str, dyn: bool, env: dict, resolve, dialect: str | None):
    """The folder after a `cd`/`-WorkingDirectory` to `text`. A variable or `-` makes it BROKEN_CWD."""
    if dyn or text.strip().strip("\"'") == "-":
        return BROKEN_CWD
    if cur is BROKEN_CWD:
        if _is_relative(text):
            return BROKEN_CWD
        base = ""
    else:
        base = cur or ""
    return norm_path(text, base, env, resolve, dialect) or BROKEN_CWD


def _apply_cd(cur, toks: list, dialect: str, env: dict, resolve):
    """Follow `cd` · `chdir` · `pushd` · `popd` · `Set-Location` · `sl` so the next command's cwd is right."""
    rest = list(toks)
    while rest:
        head = rest[0]
        if dialect == "ps" and len(rest) > 1 and rest[1].text in ("=", "+=") and not rest[1].quoted:
            rest = rest[2:]
        elif not head.quoted and (head.text.lower() in SKIP[dialect]
                                  or dialect == "bash" and re.match(r"^[A-Za-z_]\w*\+?=", head.text)):
            rest = rest[1:]
        else:
            break
    if not rest or rest[0].dyn:
        return cur
    name = canon_name(rest[0].text, dialect)
    if name == "popd":
        return BROKEN_CWD  # the stack is not tracked
    if name not in CD_NAMES:
        return cur
    posi = [a for a in rest[1:] if not is_opt(a.text, name, dialect)]
    if not posi:
        if name == "pushd":
            return BROKEN_CWD
        if dialect == "cmd":
            return cur  # bare `cd` prints, does not move
        return env.get("USERPROFILE") or env.get("HOME") or cur
    return _newcwd(cur, posi[0].text, posi[0].dyn, env, resolve, dialect)


def expand(text: str, dialect: str, env: dict, depth: int = 0, cwd=None, resolve=None) -> list:
    """Every command a line would run, inner shells opened, each stamped with the folder it runs in."""
    if depth > MAX_DEPTH:
        raise Unparsed("nested too deep")
    segs, inner = scan(text, dialect, env)
    cmds: list = []
    for body, inner_dialect in inner:
        cmds += expand(body, inner_dialect, env, depth + 1, cwd, resolve)
    cur, prev = cwd, None
    for seg in segs:
        cmds += classify(seg.toks, seg, dialect, env, depth, prev, cwd=cur, resolve=resolve)
        cur = _apply_cd(cur, seg.toks, dialect, env, resolve)
        prev = seg
    if SETENV.search(text):
        cmds.append(Cmd("setenvironmentvariable", [], Seg(), dialect, cwd=cur))
    return cmds


def classify(toks: list, seg: Seg, dialect: str, env: dict, depth: int, prev: Seg | None = None,
             cwd=None, resolve=None) -> list:
    if depth > MAX_DEPTH:
        raise Unparsed("nested too deep")
    toks = list(toks)
    while toks:
        head = toks[0]
        if dialect == "ps" and len(toks) > 1 and toks[1].text in ("=", "+=") and not toks[1].quoted:
            toks = toks[2:]  # $x = <command>
        elif not head.quoted and (head.text.lower() in SKIP[dialect]
                                  or dialect == "bash" and re.match(r"^[A-Za-z_]\w*\+?=", head.text)):
            toks = toks[1:]
        else:
            break
    bare = [Cmd("", [], seg, dialect, cwd=cwd)] if seg.redirs else []
    for cmd in bare:
        _static(cmd)
    if not toks:
        return bare
    head, args = toks[0], toks[1:]
    low = head.text.lower()
    if not head.quoted and low in DROP and dialect != "ps" or dialect == "ps" and low in ("for", "function"):
        return bare
    if dialect == "cmd" and low == "if":
        rest = args
        while rest and rest[0].text.lower() in ("not", "/i"):
            rest = rest[1:]
        if rest and rest[0].text.lower() in ("exist", "defined", "errorlevel"):
            rest = rest[2:]
        elif len(rest) > 2 and rest[1].text.lower() in ("equ", "neq", "lss", "leq", "gtr", "geq", "=="):
            rest = rest[3:]
        else:
            rest = rest[1:]
        return classify(rest, seg, dialect, env, depth, prev, cwd=cwd, resolve=resolve)
    if head.dyn:
        raise Unparsed("the command name is a variable")
    name = canon_name(head.text, dialect)
    cmd = Cmd(name, args, seg, dialect, head.text, cwd=cwd)
    _static(cmd)
    out = [cmd]

    def again(rest: list, inner_dialect: str = dialect, own_seg: bool = False, into=cwd) -> list:
        return out + (classify(rest, Seg() if own_seg else seg, inner_dialect, env, depth + 1,
                               cwd=into, resolve=resolve) if rest else [])

    if name in SHELLS:
        kind = SHELLS[name]
        body = None
        if kind == "cmd":
            for i, a in enumerate(args):
                if a.text.lower() in ("/c", "/k", "//c", "//k", "-c", "-k"):
                    body = _line(args[i + 1:])
                    break
        elif kind == "ps":
            i = 0
            while i < len(args) and body is None:
                a, key = args[i], args[i].text.lower()[1:].rstrip(":")
                if args[i].text[:1] in "-/" and key and not a.quoted:
                    if key == "ec" or "encodedcommand".startswith(key):
                        if i + 1 >= len(args) or args[i + 1].dyn:
                            raise Unparsed("encoded command")
                        try:
                            body = base64.b64decode(args[i + 1].text, validate=True).decode("utf-16-le")
                        except (ValueError, UnicodeDecodeError) as exc:
                            raise Unparsed("encoded command") from exc
                    elif "command".startswith(key):
                        body = _line(args[i + 1:])
                    elif "file".startswith(key):
                        return out  # a script file: not opened (§10.8)
                    i += 2 if key in ("ep", "ex", "w", "wd") or any(v.startswith(key) for v in PS_VALUED) else 1
                elif key.endswith(".ps1") or args[i].text == "-":
                    break
                else:
                    body = _line(args[i:])
        else:
            for i, a in enumerate(args):
                if re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", a.text):
                    if i + 1 >= len(args) or args[i + 1].dyn:
                        raise Unparsed("the inner command holds a variable")
                    body = args[i + 1].text
                    break
                if not a.text.startswith("-"):
                    return out  # a script file: not opened (§10.8)
        if body is None:
            body = _stdin_body(seg, prev)
        return out + (expand(body, kind, env, depth + 1, cwd, resolve) if body is not None else [])
    if name == "wsl":
        rest = _skip(args, ("-d", "--distribution", "-u", "--user", "--cd"))
        return again(rest, "bash")
    if name == "eval":
        return out + expand(_line(args), "bash", env, depth + 1, cwd, resolve)
    if name in ("invoke-expression", "iex"):
        rest = [a for a in args if a.quoted or not a.text.lower().startswith("-c")]
        if not rest:
            raise Unparsed("Invoke-Expression of a value the guard cannot see")
        return out + expand(_line(rest), "ps", env, depth + 1, cwd, resolve)
    if name in ("start-process", "saps") or name == "start" and dialect == "ps":
        rest, i, wd = [], 0, None
        while i < len(args):
            key = args[i].text.lower()[1:].rstrip(":")
            if args[i].text.startswith("-") and key and not args[i].quoted:
                valued = any(v.startswith(key) for v in SAPS_VALUED)
                if valued and "workingdirectory".startswith(key) and i + 1 < len(args):
                    wd = args[i + 1]
                i += 2 if valued else 1
            else:
                rest += [Tok(p, args[i].dyn) for p in args[i].text.split(",") if p]  # -ArgumentList "a","b"
                i += 1
        into = _newcwd(cwd, wd.text, wd.dyn, env, resolve, dialect) if wd is not None else cwd
        return again(rest, into=into)
    if name == "start":
        rest = [a for a in args if not is_opt(a.text, "start", "cmd")]
        return again(rest[1:] if rest and rest[0].quoted and len(rest) > 1 else rest)
    if name == "xargs":
        rest = _skip(args, ("-I", "-n", "-P", "-L", "-d", "-a", "-E", "-s"))
        return again(rest + [Tok("{}", dyn=True)] if rest else [], own_seg=True)
    if name == "env":
        i, wd = 0, None
        while i < len(args) and args[i].text.startswith("-") and len(args[i].text) > 1:
            if args[i].text in ("-C", "--chdir") and i + 1 < len(args):
                wd = args[i + 1]
                i += 2
            else:
                i += 2 if args[i].text in ("-u", "--unset", "-S", "--split-string") else 1
        rest = args[i:]
        while rest and re.match(r"^[A-Za-z_]\w*=", rest[0].text):
            rest = rest[1:]
        into = _newcwd(cwd, wd.text, wd.dyn, env, resolve, dialect) if wd is not None else cwd
        return again(rest, into=into)
    if name in PREFIXES:
        return again(_skip(args, PREFIXES[name]))
    if name == "timeout" and dialect == "bash":
        return again(_skip(args, ("-s", "-k", "--signal", "--kill-after"))[1:])
    if name == "uv" and args and args[0].text == "run":
        return again(_skip(args[1:], ("--with", "--project", "--directory", "-p", "--python", "--extra", "--group",
                                      "--env-file", "--with-requirements")))
    if name == "find":
        for i, a in enumerate(args):
            if a.text in ("-exec", "-execdir", "-ok", "-okdir"):
                inner = []
                for b in args[i + 1:]:
                    if b.text in (";", "+"):
                        break
                    inner.append(Tok(b.text, b.dyn or "{}" in b.text, b.quoted))
                out += classify(inner, Seg(), dialect, env, depth + 1, cwd=cwd, resolve=resolve)
    return out


# ---- 4. hosts ----

def _loopback(host: str, local: set) -> bool:
    h = host.strip("[]").lower()
    if h in ("localhost", "0.0.0.0", "::1", "::", "0:0:0:0:0:0:0:1") or h.endswith(".localhost") or h in local:
        return True
    try:
        if re.fullmatch(r"\d+|0x[0-9a-f]+", h):
            return ipaddress.ip_address(int(h, 0)).is_loopback  # 2130706433, 0x7f000001
        if re.fullmatch(r"127(\.\d+){1,3}", h):
            return True
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _private(host: str, local: set) -> bool:
    if _loopback(host, local):
        return True
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return ip.is_private or ip.is_link_local


def hosts(cmd: Cmd) -> list:
    """(host, port, url or None) for every address the command names."""
    found = []
    for t in [Tok(cmd.name), *cmd.args, *(r for _, r in cmd.seg.redirs)]:
        for m in URL.finditer(t.text):
            default = {"http": 80, "ws": 80, "https": 443, "wss": 443, "ftp": 21}[m.group(1).lower()]
            end = re.search(r"[\s'\"<>)]", t.text[m.start():])
            full = t.text[m.start():m.start() + end.start()] if end else t.text[m.start():]
            found.append((m.group(2).lower(), int(m.group(3) or default), full))
        for m in HOSTPORT.finditer(t.text):
            found.append((m.group(1).lower(), int(m.group(2)), None))
    if cmd.name in FETCH:
        for t in pos(cmd):
            m = re.match(r"^([A-Za-z0-9.-]+\.[A-Za-z]{2,})(?::(\d+))?(/|$)", t.text)
            if m:
                found.append((m.group(1).lower(), int(m.group(2) or 80), "//" + t.text))  # no scheme written
    return found


# ---- 5. the rules: block seven (§10.3), ask five (§10.4) ----

class Ctx:
    """What a rule reads: guard_ctx.json with its paths in one spelling, the cwd, and the injected lookups."""

    def __init__(self, data: dict, cwd: str, env: dict, resolve, local: set, head) -> None:
        self.env, self.resolve, self.local, self.head = env, resolve, local, head
        self.dialect_now = None
        self.cwd = norm_path(cwd, cwd, env, resolve, local=local) or cwd
        self.cwd_now = self.cwd           # set per command in judge(): the folder that command runs in
        self.data = dict(data)
        for key in ("protected", "secrets", "delete_roots"):
            self.data[key] = [p for p in (self.path(e) for e in data[key]) if p]

    def path(self, text: str) -> str | None:
        base = self.cwd_now
        if base is BROKEN_CWD:
            if _is_relative(text):
                raise Unparsed("a relative path after `cd` to a folder the guard cannot resolve")
            base = self.cwd  # an absolute path is still judged
        return norm_path(text, base, self.env, self.resolve, self.dialect_now, self.local)

    def paths(self, toks: list) -> list:
        return [p for p in (self.path(t.text) for t in toks) if p]


DISK = {"format", "diskpart", "format-volume", "clear-disk", "initialize-disk", "new-partition", "remove-partition",
        "resize-partition", "set-partition", "fdisk", "parted", "sfdisk", "gdisk", "wipefs"}
DEFENDER = ("windefend", "mpssvc", "securityhealthservice", "wscsvc")
REG_READ = {"get-itemproperty", "gp", "get-item", "gi", "get-childitem", "gci", "ls", "dir", "test-path",
            "get-itempropertyvalue", "gpv", "reg", "echo", "write-output", "write-host"}
PYTHONS = ("python", "python3", "py", "pythonw")
INTERP = re.compile(r"(python|pythonw|py|pypy|node|nodejs|bun|deno|ruby|perl|php)[\d.]*")
ROOTED = re.compile(r"^([A-Za-z]:[\\/]|\\\\|[\\/~]|\.{1,2}[\\/])")  # a string that starts the way a path does


def _interp(name: str) -> bool:
    """An interpreter that takes its program on the command line: `python -c` · `node -e` · `ruby -e`."""
    return bool(INTERP.fullmatch(name))


def _literals(cmd: Cmd) -> list:
    """The quoted strings of an interpreter one-liner that look like a path (§10.8 의 1). The body is not
    parsed, so a path written out is judged whether the line reads it or writes it. A script file is not opened."""
    found = []
    for a in cmd.args if _interp(cmd.name) else ():
        for m in re.finditer(r"(['\"`])(.*?)\1", a.text):
            s = m.group(2)
            if len(s) < 600 and (ROOTED.match(s) or re.search(r"[^\\/\s][\\/][^\\/\s]", s)) \
                    and ("://" not in s or s.lower().startswith("file:")):
                found.append(s)
    return found


def r_disk_format(cmd: Cmd, c: Ctx) -> bool:
    if cmd.name in DISK or cmd.name.startswith("mkfs"):
        return True
    for a in cmd.args if cmd.name == "dd" else ():
        v = a.text.lower()[3:] if a.text.lower().startswith("of=") else ""
        if re.match(r"^\\{1,2}\.\\(physicaldrive|[a-z]:)", v.replace("/", "\\")) \
                or re.match(r"^/dev/(sd|hd|nvme|disk|vd|mmcblk)", v):
            return True
    return False


def r_system_delete(cmd: Cmd, c: Ctx) -> bool:
    o = opts(cmd)
    if cmd.name in DELETE and recursive(cmd):
        targets = c.paths(pos(cmd))
    elif cmd.name == "robocopy" and {"/mir", "/purge"} & set(o):
        targets = c.paths(pos(cmd)[1:2])
    elif cmd.name == "find" and "-delete" in o:
        targets = c.paths(pos(cmd)[:1]) or [c.cwd]
    else:
        return False
    drive = c.env.get("SYSTEMDRIVE") or "C:"
    system = [c.env.get("SYSTEMROOT") or c.env.get("WINDIR") or drive + "\\Windows",
              c.env.get("PROGRAMFILES") or drive + "\\Program Files",
              c.env.get("PROGRAMFILES(X86)") or drive + "\\Program Files (x86)"]
    profile = c.env.get("USERPROFILE") or ""
    for t in targets:
        parts = _parts(t)
        if len(parts) <= 1 or any(under(t, r) for r in system):
            return True
        if any(above(t, r) for r in [*system, profile] if r):  # C:\Users and the roots themselves
            return True
        if re.search(r"[*?\[{]", parts[-1]) and (len(parts) == 2 or profile and above("\\".join(parts[:-1]), profile)
                                                 and len(parts) == len(_parts(profile)) + 1):
            return True  # everything right below a drive root or the profile root
    return False


def _candidates(cmd: Cmd):
    """Every string of a command that may be a path."""
    for t in [*cmd.args, *(r for _, r in cmd.seg.redirs)]:
        s = t.text
        if is_opt(s, cmd.name, cmd.dialect):
            m = re.match(r"^[-/][^=:]*[=:](.+)$", s)
            if not m:
                continue
            s = m.group(1)
        elif re.match(r"^[A-Za-z_]\w*=", s):
            s = s.split("=", 1)[1]  # of=path
        yield s.lstrip("@")         # curl -d @file
    yield from _literals(cmd)       # python -c "open('<path>')"


def r_secret_access(cmd: Cmd, c: Ctx) -> bool:
    secrets = c.data["secrets"]
    paths = [p for p in (c.path(s) for s in _candidates(cmd)) if p]
    if any(under(p, s) for p in paths for s in secrets):
        return True
    if cmd.name == "#deep" or cmd.name in ALWAYS_DEEP or cmd.name in DEEP_IF_FLAG and recursive(cmd):
        roots = c.paths(pos(cmd))
        if len(roots) <= 1 and not cmd.name.startswith("#"):
            roots.append(c.cwd)  # `grep -r x` reads the cwd
        folders = [s for s in secrets if not FILE_SECRET.search(s)]
        return any(above(r, s) for r in roots for s in folders)
    return False


def _hive(text: str, hive: str, long: str) -> bool:
    t = re.sub(r"(?i)^(registry::|\\\\[^\\]+\\)", "", text.strip("'\"")).lower()
    return t.startswith((hive, long))


def r_security_config(cmd: Cmd, c: Ctx) -> bool:
    n, w, o = cmd.name, words(cmd), opts(cmd)
    low = " ".join(a.text.lower() for a in cmd.args)
    if n in ("set-mppreference", "add-mppreference", "remove-mppreference", "passwd", "chpasswd", "useradd",
             "userdel", "usermod", "set-netfirewallprofile"):
        return True
    if n in ("sc", "stop-service", "set-service", "spsv", "net", "net1", "remove-service", "suspend-service") \
            and any(d in low for d in DEFENDER) and w[:1] not in (["query"], ["qc"], ["queryex"]):
        return True
    if n == "netsh" and ("advfirewall" in w or "firewall" in w) and "show" not in w:
        return True
    if not n.startswith("get-") and any(fnmatch.fnmatchcase(n, p) for p in (
            "*-netfirewallrule", "*-localuser", "*-localgroupmember", "*-localgroup")):
        return True
    if n in ("net", "net1") and w[:1] in (["user"], ["localgroup"], ["accounts"]) and len(cmd.args) > 1:
        return True
    if n == "wmic" and "useraccount" in low:
        return True
    if n == "reg" and w:
        if w[0] in ("import", "restore", "load", "unload"):
            return True
        if w[0] in ("add", "delete", "copy"):
            if any(t.dyn for t in pos(cmd)[1:]):
                raise Unparsed("the registry key is a variable")
            return any(_hive(k, "hklm", "hkey_local_machine") for k in w[1:])
    if n == "regedit" and ("/s" in o or "-s" in o or any(x.endswith(".reg") for x in w)):
        return True
    return n not in REG_READ and any(_hive(a.text, "hklm:", "hkey_local_machine") for a in cmd.args)


def _server(c: Ctx) -> tuple:
    u = urlsplit(c.data["server_url"])
    return (u.hostname or "").lower(), u.port or (443 if u.scheme == "https" else 80)


def r_server_control(cmd: Cmd, c: Ctx) -> bool:
    n, w = cmd.name, words(cmd)
    low = [a.text.lower() for a in cmd.args]
    pid = str(c.data["server_pid"])
    images = [i.lower() for i in c.data.get("server_images") or SERVER_IMAGES]

    def hit(value: str) -> bool:
        v = value.strip("'\"").lstrip("-")
        name = canon_name(v, "cmd").replace("%", "*")
        return v == pid or name in images or bool(re.search(r"[*?]", name)) and any(
            fnmatch.fnmatchcase(i, name) for i in images)

    if n == "taskkill":
        vals = []
        for i, a in enumerate(low[:-1]):
            if a.lstrip("/-") in ("pid", "im"):
                vals.append(low[i + 1])
            elif a.lstrip("/-") == "fi":
                vals += re.findall(r"(?:pid|imagename)\s+eq\s+(\S+)", low[i + 1])
        return not vals or any(hit(v) for v in vals)  # a kill with no target the guard can see is refused
    if n == "wmic" and "process" in low and ("terminate" in " ".join(low) or "delete" in low):
        vals = re.findall(r"(?:processid|name)\s*(?:=|like)\s*['\"]?([\w.%*-]+)", " ".join(low))
        return not vals or any(hit(v) for v in vals)
    if n in ("stop-process", "spps", "kill", "tskill", "pkill", "killall"):
        vals = [v for t in pos(cmd) for v in t.text.lower().split(",") if v]
        if not vals and n in ("stop-process", "spps"):
            return True
        if any(hit(v) for v in vals) or pid in [a.lstrip("-") for a in low]:
            return True
        if n in ("pkill", "killall") and any(k in v for v in vals for k in ("relay", "uvicorn")):
            return True
    host, port = _server(c)
    if any(p == port and (h == host or _loopback(h, c.local)) for h, p, _ in hosts(cmd)):
        return True
    if str(port) in low and any(t == host or _loopback(t, c.local) for t in low if not t.isdigit()):
        return True  # nc localhost 8765
    if n == "relay":
        return bool(w) and w[0] in RELAY_BLOCKED
    if n == "uvicorn" or n in PYTHONS and "-m" in low:
        mod = low[low.index("-m") + 1] if n in PYTHONS and low.index("-m") + 1 < len(low) else "uvicorn"
        rest = [x for x in low[low.index("-m") + 2:] if not x.startswith("-")] if n in PYTHONS else []
        if mod == "uvicorn":
            return any("relay_agent" in x for x in low)
        return mod.startswith("relay_agent") and (mod.startswith("relay_agent.server") or rest[:1] != []
                                                  and rest[0] in RELAY_BLOCKED)
    return False


def _aria2c_out(args: list) -> list:
    """`aria2c -d <folder> -o <name>`: one path, the name always below the folder. With no name it comes from the
    URL, so everything right below the folder is the target. Case-sensitive: `-D` · `-O` are other options."""
    def value(short: str, long: str):
        got = None
        for i, a in enumerate(args):
            if a.text in (short, long) and i + 1 < len(args):
                got = args[i + 1]
            elif a.text.startswith(long + "="):
                got = Tok(a.text[len(long) + 1:], a.dyn)
            elif a.text.startswith(short) and len(a.text) > 2:
                got = Tok(a.text[2:], a.dyn)  # -d<folder>
        return got if got is not None and got.text else None

    folder, name = value("-d", "--dir"), value("-o", "--out")
    if folder is None:
        return [name] if name is not None else []
    tail = name.text.lstrip("/\\") if name is not None else "*"
    return [Tok(folder.text.rstrip("/\\") + "/" + tail, folder.dyn or name is not None and name.dyn)]


def _download_out(cmd: Cmd) -> list:
    """Where a download or an archive extraction writes: `curl -o` · `wget -O` · `-OutFile` · `aria2c -d/-o`,
    `tar -C` · `unzip -d` · `Expand-Archive -DestinationPath` · `7z x -o`. Not every form is caught (§10.8)."""
    n, args, toks = cmd.name, cmd.args, []

    def after(flags: tuple, glued: tuple = ()) -> None:
        for i, a in enumerate(args):
            if a.text.lower() in flags and i + 1 < len(args):
                toks.append(args[i + 1])
            for g in glued:
                if len(a.text) > len(g) and a.text.lower().startswith(g):
                    toks.append(Tok(a.text[len(g):], a.dyn))

    if n == "curl":
        after(("-o", "--output"), ("-o",))
    elif n == "wget":
        after(("--output-document",), ("--output-document=",))
        for i, a in enumerate(args):  # wget `-O` (case-sensitive: `-o` is the log file)
            if a.text == "-O" and i + 1 < len(args):
                toks.append(args[i + 1])
    elif n == "aria2c":
        toks += _aria2c_out(args)
    elif n in ("invoke-webrequest", "iwr", "invoke-restmethod", "irm"):
        after(("-outfile",), ("-outfile:",))
    elif n == "start-bitstransfer":
        after(("-destination",), ("-destination:",))
    elif n == "unzip":
        after(("-d",))
    elif n == "expand-archive":
        after(("-destinationpath",), ("-destinationpath:", "-dest:"))
        toks += [args[i + 1] for i, a in enumerate(args)
                 if a.text[:1] == "-" and len(a.text) > 2 and "destinationpath".startswith(a.text.lower()[1:])
                 and i + 1 < len(args)]
    elif n in ("7z", "7za") and any(re.fullmatch(r"-o.+", a.text, re.I) for a in args):
        toks += [Tok(a.text[2:], a.dyn) for a in args if re.fullmatch(r"-o.+", a.text, re.I)]
    elif n == "tar" and any(re.fullmatch(r"-[A-Za-z]*x[A-Za-z]*", a.text) or a.text.lower() == "--extract"
                            for a in args):
        for i, a in enumerate(args):
            if a.text == "-C" and i + 1 < len(args):
                toks.append(args[i + 1])
            elif a.text.lower().startswith("--directory="):
                toks.append(Tok(a.text.split("=", 1)[1], a.dyn))
            elif a.text.lower() == "--directory" and i + 1 < len(args):
                toks.append(args[i + 1])
    return toks


def _write_targets(cmd: Cmd, c: Ctx) -> list:
    n, o = cmd.name, opts(cmd)
    out = c.paths([t for op, t in cmd.seg.redirs if op in (">", ">>")])
    in_place = n == "sed" and any(x.startswith("--in-place") or re.fullmatch(r"-[a-z]*i.*", x) for x in o)
    if n in ("#write", "#any") or n in DELETE | MOVE | WRITE or n == "sc" and cmd.dialect == "ps" or in_place:
        out += c.paths(pos(cmd))
    elif n == "robocopy":
        out += c.paths(pos(cmd)[1:2])
    elif n in COPY:
        dest = [cmd.args[i + 1] for i, a in enumerate(cmd.args[:-1])
                if len(a.text) > 2 and "-destination".startswith(a.text.lower())]
        out += c.paths(dest or pos(cmd)[-1:])
    elif n == "dd":
        out += c.paths([Tok(a.text[3:]) for a in cmd.args if a.text.lower().startswith("of=")])
    out += c.paths(_download_out(cmd))
    out += c.paths([Tok(s) for s in _literals(cmd)])  # python -c "open('<path>','w')": read or write, not told apart
    return out


def r_protected_write(cmd: Cmd, c: Ctx) -> bool:
    targets, protected = _write_targets(cmd, c), c.data["protected"]
    if any(under(t, p) for t in targets for p in protected):
        return True
    return cmd.name in DELETE | MOVE | {"robocopy"} and any(above(t, p) for t in targets for p in protected)


def r_payment_account(cmd: Cmd, c: Ctx) -> bool:
    if any(fnmatch.fnmatchcase(h, pat) or h == pat[2:] for h, _, _ in hosts(cmd) for pat in PAY_HOSTS):
        return True
    w = words(cmd)
    for pattern in PAY_CLI:
        rest = iter(w)
        if pattern[0] == cmd.name and all(p in rest for p in pattern[1:]):
            return True
    return False


def r_git_risky(cmd: Cmd, c: Ctx) -> bool:
    if cmd.name != "git":
        return False
    rest, i = cmd.args, 0
    while i < len(rest) and rest[i].text.startswith("-"):
        i += 2 if rest[i].text in ("-C", "-c", "--git-dir", "--work-tree", "--namespace") else 1
    rest = rest[i:]
    if not rest:
        return False
    if rest[0].dyn:
        raise Unparsed("the git subcommand is a variable")
    sub, o = rest[0].text.lower(), [t.text for t in rest[1:] if t.text.startswith("-")]
    short = [x[1:] for x in o if re.fullmatch(r"-[A-Za-z]+", x)]
    if sub == "clean":
        return ("--force" in o or any("f" in x for x in short)) and not (
            "--dry-run" in o or any("n" in x for x in short))
    return sub == "push" or sub == "reset" and "--hard" in o


def r_outside_delete(cmd: Cmd, c: Ctx) -> bool:
    o = opts(cmd)
    if cmd.name in DELETE | MOVE:
        targets = c.paths(pos(cmd))
    elif cmd.name == "robocopy" and {"/mir", "/purge", "/move", "/mov"} & set(o):
        targets = c.paths(pos(cmd)[:2])
    elif cmd.name == "find" and "-delete" in o:
        targets = c.paths(pos(cmd)[:1]) or [c.cwd]
    else:
        return False
    return any(not any(under(_fixed(t), r) for r in c.data["delete_roots"]) for t in targets)


def r_install_env(cmd: Cmd, c: Ctx) -> bool:
    n, w, o = cmd.name, words(cmd), opts(cmd)
    low = [a.text.lower() for a in cmd.args]
    first, glob = (w[:1] or [""])[0], bool({"-g", "--global"} & set(o))
    if n in ("winget", "choco", "scoop", "brew", "apt", "apt-get", "dnf", "yum", "pipx", "cargo", "gem"):
        return first in ("install", "uninstall", "upgrade", "remove", "purge", "inject")
    if n == "msiexec":
        return any(re.match(r"^[/-]{1,2}(i|x|package|uninstall)", x) for x in low)
    if n in ("add-appxpackage", "remove-appxpackage", "add-appxprovisionedpackage", "install-package",
             "uninstall-package", "install-module", "uninstall-module", "install-script", "setx",
             "setenvironmentvariable"):
        return True
    if n in ("npm", "pnpm"):
        return glob and first in ("install", "i", "add", "uninstall", "un", "rm", "remove", "update", "upgrade", "link")
    if n == "yarn":
        return w[:1] == ["global"] and w[1:2] in (["add"], ["remove"], ["upgrade"])
    if n in ("pip", "pip3") or n in PYTHONS and w[:1] == ["pip"] and "-m" in low:
        sub = (w[1:2] or [""])[0] if n in PYTHONS else first
        in_venv = bool(c.env.get("VIRTUAL_ENV")) or bool(re.search(r"(?i)venv|[\\/]envs[\\/]", cmd.head))
        return sub in ("install", "uninstall") and ("--user" in o or not in_venv and not {"-t", "--target"} & set(o))
    if n == "uv":
        return w[:1] == ["tool"] and w[1:2] in (["install"], ["uninstall"], ["upgrade"]) \
            or w[:1] == ["pip"] and "--system" in o
    if n == "dotnet":
        return w[:1] == ["tool"] and w[1:2] in (["install"], ["uninstall"], ["update"]) and glob
    if n == "go":
        return first == "install"
    if n == "reg":
        return first in ("add", "delete") and any(
            re.match(r"^(hkcu|hkey_current_user)\\environment", k) for k in w[1:])
    return n not in REG_READ and any(
        re.match(r"(?i)^(registry::)?(hkcu:|hkey_current_user)\\environment", a.text) for a in cmd.args)


def r_power_state(cmd: Cmd, c: Ctx) -> bool:
    n, low = cmd.name, [a.text.lower() for a in cmd.args]
    if n == "shutdown":
        return not any(x in ("/a", "-a", "//a", "/?", "--help") for x in low)  # `-c` is a comment, not cancel
    if n == "rundll32":
        return "powrprof" in " ".join(low) and "setsuspendstate" in " ".join(low)
    if n == "systemctl":
        return bool({"poweroff", "reboot", "suspend", "hibernate", "halt"} & set(low))
    return n in ("stop-computer", "restart-computer", "psshutdown", "reboot", "poweroff", "halt")


def r_net_transfer(cmd: Cmd, c: Ctx) -> bool:
    n, w = cmd.name, words(cmd)
    raw = [a.text for a in cmd.args]
    known = [k.lower() for k in c.data["known_hosts"]]

    def ok(host: str) -> bool:
        return _private(host, c.local) or any(
            host == k or host.endswith("." + k) or fnmatch.fnmatchcase(host, k) for k in known)

    if n in FETCH:
        named = [(h, u) for h, _, u in hosts(cmd) if u]
        # `curl -T a.zip https://host/` : with a scheme written, a bare `a.zip` is a file, not a host
        urls = [x for x in named if not x[1].startswith("//")] or [(h, "http:" + u) for h, u in named]
        sending = "/upload" in [x.lower() for x in raw]
        for i, x in enumerate(raw):
            nxt = raw[i + 1] if i + 1 < len(raw) else ""
            if x in ("-T", "-F") or x[:2] in ("-T", "-F") and re.search(r"[=@./\\]", x[2:]) or x.split("=")[0] in ("--upload-file", "--form", "--form-string",
                                                            "--post-file", "--body-file"):
                sending = True
            elif x in ("-d", "--data", "--data-binary", "--data-ascii", "--json") and nxt.startswith("@") \
                    or x == "--data-urlencode" and "@" in nxt or x.startswith("-d@"):
                sending = True
            elif len(x) > 3 and "-infile".startswith(x.lower()):
                sending = True
        if sending:
            return any(not ok(h) for h, _ in urls)
        for h, u in urls:
            size = c.head(u) if c.head and u.lower().startswith("http") and not _private(h, c.local) else None
            if size is not None and size > GB:
                return True
        return False
    last = (pos(cmd)[-1:] or [Tok("")])[0].text
    if n in ("scp", "rsync", "sftp"):
        spec = pos(cmd)[0].text if n == "sftp" and pos(cmd) else last
        m = re.match(r"^(?:\w+://)?(?:[^@/\\:]+@)?([^:/\\@]{2,})(:|$)", spec)
        remote = m and (":" in spec or n == "sftp")
        return bool(remote) and not ok(m.group(1).lower())
    if n == "rclone" and w[:1] and w[0] in ("copy", "sync", "move", "copyto", "moveto"):
        m = re.match(r"^([^:/\\]{2,}):", last)
        return bool(m) and not ok(m.group(1).lower())
    if n == "aws" and w[:1] == ["s3"] and w[1:2] in (["cp"], ["sync"], ["mv"]) and last.lower().startswith("s3://"):
        return not ok(last[5:].split("/")[0].lower())
    return False


RULES = {"disk_format": r_disk_format, "system_delete": r_system_delete, "secret_access": r_secret_access,
         "security_config": r_security_config, "server_control": r_server_control,
         "protected_write": r_protected_write, "payment_account": r_payment_account, "git_risky": r_git_risky,
         "outside_delete": r_outside_delete, "install_env": r_install_env, "power_state": r_power_state,
         "net_transfer": r_net_transfer}

# ---- 6. the judgement (§10.5) and the answer (§10.6) ----

BLOCKED = "이 명령은 치명 동작 목록에 걸려 실행하지 않는다. 표기를 바꿔 다시 시도하지 말고, 다른 방법으로 진행하거나 ask_owner 로 물어라."
UNPARSED = "명령을 해석하지 못해 실행하지 않는다. 변수나 인코딩 없이 명령과 경로를 그대로 적어 다시 실행하라."
ERRORED = "가드가 판정하지 못해 실행하지 않는다. 같은 호출을 되풀이하지 말고 ask_owner 로 알려라."
UNREACHABLE = "결재를 물어야 하는 명령인데 서버에 닿지 못해 실행하지 않는다. 잠시 뒤 그대로 다시 시도하라."
ASKED = "owner 의 결재가 필요한 명령이라 실행하지 않는다."
CTX_FIELDS = {"run_id": str, "server_root": str, "runs_dir": str, "server_url": str, "server_pid": int,
              "protected": list, "secrets": list, "delete_roots": list, "known_hosts": list}


def sentence(rule_id: str, text: str, label: str = "차단") -> str:
    return f"[가드 · {rule_id} · {label}] {text}"


def _why(reason: str) -> str:
    """One more sentence after UNPARSED: what to rewrite. The verdict and its rule_id stay as they are."""
    if "variable" in reason:
        return " 변수 자리에 실제 값을 적어라."
    if "`cd`" in reason:
        return " 폴더를 옮긴 대상을 실제 경로로 적거나, 그 뒤의 경로를 절대 경로로 적어라."
    if "unbalanced" in reason:
        return " 따옴표와 괄호의 짝을 맞춰라."
    if re.search(r"pipe|encoded|nested|heredoc|Invoke-Expression", reason):
        return " 셸에 넘긴 본문을 명령으로 직접 적어라."
    return ""


def load_ctx(path: str) -> dict:
    """guard_ctx.json (§10.2). Anything missing or of the wrong type is a GuardError."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise GuardError(f"guard_ctx.json: {exc}") from exc
    if not isinstance(data, dict) or data.get("v") != 1:
        raise GuardError("guard_ctx.json: v != 1")
    for key, kind in CTX_FIELDS.items():
        value = data.get(key)
        if not isinstance(value, kind) or isinstance(value, bool) \
                or kind is list and not all(isinstance(x, str) for x in value):
            raise GuardError(f"guard_ctx.json: {key}")
    return data


def default_secrets(env: dict, server_root: str) -> list:
    """`secrets[]` of guard_ctx.json (§10.3 의 3). The server writes this list; nothing here opens a file."""
    e = {k.upper(): v for k, v in env.items()}
    home = e.get("USERPROFILE") or e.get("HOME") or ""
    local = e.get("LOCALAPPDATA") or ntpath.join(home, "AppData", "Local")
    roaming = e.get("APPDATA") or ntpath.join(home, "AppData", "Roaming")
    temp = e.get("TEMP") or e.get("TMP") or ntpath.join(local, "Temp")
    return [ntpath.join(home, ".ssh"),
            ntpath.join(local, "Google", "Chrome", "User Data"),
            ntpath.join(local, "Microsoft", "Edge", "User Data"),
            ntpath.join(roaming, "Mozilla", "Firefox", "Profiles"),
            ntpath.join(home, ".claude", ".credentials.json"),
            ntpath.join(home, ".claude.json"),
            ntpath.join(home, ".katae"),
            ntpath.join(server_root, "relay.config.local.yaml"),
            ntpath.join(temp, "katae-*", "mcp.json")]


def local_names() -> set:
    """This PC's name and addresses: loopback by another spelling (§10.3 의 5)."""
    names = set()
    try:
        host = socket.gethostname()
        names.add(host.lower())
        names.update(a.lower() for a in socket.gethostbyname_ex(host)[2])
    except OSError:
        pass
    return names


def http_ask(url: str, token: str, timeout: float = ASK_TIMEOUT):
    """The real `ask`: POST /studio/guard/check (§6.4). Raises unless the server answers 200 with an object."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def ask(tool: str, command: str, rule_id: str) -> dict:
        body = json.dumps({"tool": tool, "command": command, "rule_id": rule_id}).encode("utf-8")
        req = urllib.request.Request(url.rstrip("/") + "/studio/guard/check", data=body, method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        with opener.open(req, timeout=timeout) as resp:
            answer = json.loads(resp.read().decode("utf-8"))
        if resp.status != 200 or not isinstance(answer, dict):
            raise ValueError(f"HTTP {resp.status}")
        return answer

    return ask


def http_head(url: str, timeout: float = HEAD_TIMEOUT) -> int | None:
    """Content-Length of a download, or None when the server does not say (§10.4 의 5: then it passes)."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=timeout) as resp:
            return int(resp.headers.get("Content-Length") or "") if resp.headers.get("Content-Length") else None
    except (OSError, ValueError):
        return None


def _calls(tool: str, tin: dict, c: Ctx) -> list:
    """The tool call as commands the rules can read (§10.2 의 표)."""
    def one(name: str, values: list) -> list:
        return [Cmd(name, [Tok(v) for v in values if isinstance(v, str) and v], Seg(), "tool")]

    if tool in SHELL_TOOLS:
        if not isinstance(tin.get("command"), str):
            raise Unparsed("no command")
        return expand(tin["command"], SHELL_TOOLS[tool], c.env, cwd=c.cwd, resolve=c.resolve)
    if tool in EDIT_TOOLS:
        return one("#write", [tin.get("file_path"), tin.get("notebook_path")])
    if tool == "Read":
        return one("#read", [tin.get("file_path")])
    if tool in ("Grep", "Glob"):
        base = tin.get("path") if isinstance(tin.get("path"), str) and tin.get("path") else c.cwd
        pattern = tin.get("pattern") if tool == "Glob" else tin.get("glob")
        values = [base]
        if isinstance(pattern, str) and pattern:
            rooted = re.match(r"^([A-Za-z]:|[\\/~])", pattern)
            values.append(pattern if rooted else base.rstrip("\\/") + "\\" + pattern)
        return one("#deep", values)
    urls = [v for v in (tin.get("url"), tin.get("uri")) if isinstance(v, str) and re.match(r"(?i)^https?://", v)]
    if urls:
        return one("#url", urls)
    found: list = []

    def walk(value) -> None:
        if isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
        elif isinstance(value, str) and len(value) < 600 and "\n" not in value \
                and ROOTED.match(value):
            found.append(value)

    walk(tin)
    return one("#any", found)


def judge(event: dict, ctx: dict, env: dict | None = None, *, ask=None, head=None, resolve=_resolve,
          local: set | None = None) -> Verdict:
    """One tool call -> one Verdict. `ask(tool, command, rule_id) -> dict` is the server (§6.4); `head(url)`
    is the size of a download; `resolve(path)` follows links; `local` is this PC's names. All injected."""
    tool = str(event.get("tool_name") or "")
    tin = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    shell = tool in SHELL_TOOLS and isinstance(tin.get("command"), str)
    command = tin["command"] if shell else json.dumps(tin, ensure_ascii=False)

    def out(verdict: str, rule_id: str | None = None, message: str = "", server: dict | None = None) -> Verdict:
        return Verdict(verdict, rule_id, "denied" if message else "allowed", message, server, tool, command)

    if tool.startswith("mcp__studio__"):
        return out("allow")
    try:
        if not tool:
            raise GuardError("no tool_name")
        upper = {k.upper(): v for k, v in (os.environ if env is None else env).items()}
        cwd = event.get("cwd") if isinstance(event.get("cwd"), str) and event.get("cwd") else os.getcwd()
        c = Ctx(ctx, cwd, upper, resolve, set() if local is None else {x.lower() for x in local}, head)
        cmds = _calls(tool, tin, c)

        def fires(rule_id: str) -> bool:
            for cmd in cmds:
                c.cwd_now = c.cwd if cmd.cwd is None else cmd.cwd  # the folder this command runs in (cd-aware)
                c.dialect_now = cmd.dialect
                if RULES[rule_id](cmd, c):
                    return True
            return False

        for rule_id in BLOCK_RULES:
            if fires(rule_id):
                return out("block", rule_id, sentence(rule_id, BLOCKED))
        asked = next((r for r in ASK_RULES if fires(r)), None)
    except Unparsed as exc:
        return out("block", "unparsed", sentence("unparsed", UNPARSED + _why(str(exc))))
    except Exception as exc:  # noqa: BLE001 - closed side: whatever goes wrong inside the guard refuses the call
        return out("block", "guard_error", sentence("guard_error", ERRORED), {"error": f"{type(exc).__name__}: {exc}"[:200]})
    if asked is None:
        return out("allow")
    try:
        if ask is None:
            raise GuardError("no server to ask")
        answer = ask(tool, command, asked)
        if not isinstance(answer, dict):
            raise GuardError("the server's answer is not an object")
    except Exception as exc:  # noqa: BLE001 - unreachable, timed out, 401, 503, bad JSON: all refuse (§6.4)
        return out("ask", asked, sentence(asked, UNREACHABLE), {"error": f"{type(exc).__name__}: {exc}"[:200]})
    server = {"decision": answer.get("decision"), "inbox_id": answer.get("inbox_id"),
              "end_turn": bool(answer.get("end_turn"))}
    if answer.get("decision") == "allow":
        return out("ask", asked, server=server)
    label = "결재 필요" if answer.get("inbox_id") else "거부"
    return out("ask", asked, sentence(asked, str(answer.get("message") or ASKED), label), server)


# ---- 7. the audit log (§10.7) ----

AUDIT_COMMAND = 4000  # characters of `command` a line keeps


def claims(token) -> dict:
    """The claims of an actor token, read without checking its signature — the hook has no server secret (§10.2).
    For the audit log only, never for a judgement. {} when they cannot be read."""
    try:
        part = str(token).split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def audit_line(verdict: Verdict, event: dict, token, ms: int, run_id: str = "") -> dict:
    """One judgement as the line of §10.7. `run_id` (guard_ctx.json) stands in when the token names no run."""
    who = claims(token)
    cwd = event.get("cwd") if isinstance(event.get("cwd"), str) and event.get("cwd") else os.getcwd()
    line = {"v": 1, "at": _datetime.now().isoformat(timespec="seconds"), "run_id": who.get("run") or run_id or None,
            "employee_id": who.get("emp"), "room_id": who.get("room"), "task_id": who.get("task"),
            "mode": who.get("mode"), "tool": verdict.tool, "command": verdict.command[:AUDIT_COMMAND], "cwd": cwd,
            "verdict": verdict.verdict, "rule_id": verdict.rule_id, "outcome": verdict.outcome}
    if len(verdict.command) > AUDIT_COMMAND:
        line["truncated"] = True
    if verdict.verdict == "ask":
        line["server"] = verdict.server or {}
    elif verdict.rule_id == "guard_error" and (verdict.server or {}).get("error"):
        line["error"] = verdict.server["error"]  # why the guard could not judge; `server` is the ask's alone
    line["ms"] = ms
    return line


def _hold(fd: int, on: bool) -> None:
    """Windows only: the first byte of the file is the writers' turn. Two hooks that appended at once could
    both seek to the same end and one line would be lost. Raises OSError when the turn does not come."""
    if msvcrt is None:
        return
    os.lseek(fd, 0, os.SEEK_SET)
    if not on:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    for _ in range(600):  # about three seconds; then the line is not written and the call is refused
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            _time.sleep(0.005)
    raise OSError("the audit log is held by another writer")


def write_audit(runs_dir: str, line: dict) -> None:
    """Append the line to `<runs_dir>/studio/audit/guard-<YYYYMMDD>.jsonl` — the day of the line itself — with
    one write (§10.7). Raises (OSError, ValueError) when it could not be written: the caller refuses the call."""
    folder = os.path.join(runs_dir, "studio", "audit")
    os.makedirs(folder, exist_ok=True)
    data = (json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8")
    path = os.path.join(folder, "guard-" + str(line["at"])[:10].replace("-", "") + ".jsonl")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    try:
        _hold(fd, True)
        try:
            if os.write(fd, data) != len(data):
                raise OSError("the audit line was cut short")
        finally:
            try:
                _hold(fd, False)
            except OSError:
                pass  # closing the file gives the turn back
    finally:
        os.close(fd)


def _said(event: dict) -> tuple:
    """(tool, command) of a call, as `judge` names them (§10.2)."""
    tool = str(event.get("tool_name") or "")
    tin = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    if tool in SHELL_TOOLS and isinstance(tin.get("command"), str):
        return tool, tin["command"]
    return tool, json.dumps(tin, ensure_ascii=False)


def main(argv: list | None = None) -> int:
    argv = sys.argv if argv is None else argv
    started = _time.perf_counter()
    ctx, event, token = None, {}, os.environ.get("STUDIO_ACTOR_TOKEN")

    def record(verdict: Verdict) -> None:
        ms = int((_time.perf_counter() - started) * 1000)
        write_audit(ctx["runs_dir"], audit_line(verdict, event, token, ms, ctx["run_id"]))

    try:
        read = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
        if not isinstance(read, dict):
            raise GuardError("stdin is not a tool call")
        event = read
        if str(event.get("tool_name") or "").startswith("mcp__studio__"):
            try:  # §10.5 의 1: passed before guard_ctx.json is needed; its line is written when that can be read
                ctx = load_ctx(argv[1])
                record(Verdict("allow", None, "allowed", "", None, *_said(event)))
            except Exception:  # noqa: BLE001 - never a reason to stop a studio tool: ask_owner is the way out
                pass
            return 0
        if len(argv) < 2:
            raise GuardError("no guard_ctx.json path")
        ctx = load_ctx(argv[1])
        url = os.environ.get("STUDIO_URL")
        if not url or not token:
            raise GuardError("STUDIO_URL or STUDIO_ACTOR_TOKEN is missing")
        verdict = judge(event, ctx, ask=http_ask(url, token), head=http_head, local=local_names())
    except Exception as exc:  # noqa: BLE001 - closed side (§10.6)
        verdict = Verdict("block", "guard_error", "denied", sentence("guard_error", ERRORED),
                          {"error": f"{type(exc).__name__}: {exc}"[:200]}, *_said(event))
    if ctx is not None:  # without guard_ctx.json there is no place to write: refused as it is, and no line
        try:
            record(verdict)
        except Exception:  # noqa: BLE001 - §10.7: a judgement that left no line lets nothing run
            if verdict.outcome == "allowed":
                verdict = Verdict("block", "guard_error", "denied", sentence("guard_error", ERRORED))
    if verdict.outcome == "allowed":
        return 0
    sys.stderr.buffer.write((verdict.message + "\n").encode("utf-8"))
    sys.stderr.buffer.flush()
    return 2


if __name__ == "__main__":
    sys.exit(main())
