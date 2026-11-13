#!/usr/bin/env python
"""freebuff-mcp — MCP bridge that drives the installed Freebuff CLI.

Freebuff (codebuff-based) ships only a full-screen TUI; it exposes no headless
prompt mode. This server owns a ConPTY, drives the TUI like a user would
(type the prompt, press Enter), and reads the authoritative run transcript the
CLI persists under ~/.config/manicode/projects/<project>/chats/<id>/.

Tools:
  freebuff_status     installed version, account, mode/model, live instances
  freebuff_run        run one prompt, return the final answer + tool usage
  freebuff_chats      list persisted chats for a project
  freebuff_transcript read a persisted chat transcript
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

from winpty import PtyProcess

SERVER_NAME = "freebuff-mcp"
SERVER_VERSION = "1.1.0"
PROTOCOL_VERSION = "2024-11-05"

HOME = Path(os.path.expanduser("~"))
CONFIG_DIR = Path(os.environ.get("FREEBUFF_CONFIG_DIR", HOME / ".config" / "manicode"))
PROJECTS_DIR = CONFIG_DIR / "projects"
SETTINGS_PATH = CONFIG_DIR / "settings.json"
SETTINGS_SNAPSHOT = CONFIG_DIR / "freebuff-mcp-settings-snapshot.json"
API_BASE = os.environ.get("FREEBUFF_API_BASE", "https://www.codebuff.com").rstrip("/")
CATALOG_PATH = "/api/v1/freebuff/models"
CATALOG_TTL_SECONDS = 300

ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[a-zA-Z]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[()][0-9A-B]"
    r"|\x1b[=>]"
)

READY_MARKERS = ("Enter a coding task or", "for commands", "hours remaining")
TAKEOVER_MARKER = "already running elsewhere"
SESSION_LIMIT_MARKERS = ("You've reached", "No hours left", "daily limit")


def log(message: str) -> None:
    print(f"[{SERVER_NAME}] {message}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Freebuff installation discovery
# --------------------------------------------------------------------------- #
def freebuff_binary() -> str:
    override = os.environ.get("FREEBUFF_EXE")
    if override and Path(override).is_file():
        return override
    native = CONFIG_DIR / ("freebuff.exe" if os.name == "nt" else "freebuff")
    if native.is_file():
        return str(native)
    found = shutil.which("freebuff") or shutil.which("freebuff.cmd")
    if found:
        return found
    raise RuntimeError(
        "Freebuff CLI not found. Expected %s or a 'freebuff' on PATH." % native
    )


def project_dir_for(cwd: Path) -> Path:
    """Freebuff keys chat storage by the working directory's basename."""
    return PROJECTS_DIR / cwd.name


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return default


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def live_instances() -> list[dict]:
    now = time.time()
    out = []
    for path in sorted(CONFIG_DIR.glob("freebuff-live-*.json")):
        record = read_json(path, {}) or {}
        pid = int(record.get("ownerPid") or 0)
        expires_ms = record.get("expiresAt")
        expires_at = expires_ms / 1000.0 if isinstance(expires_ms, (int, float)) else None
        out.append(
            {
                "file": path.name,
                "pid": pid,
                "alive": pid_alive(pid),
                "instance_id": record.get("instanceId"),
                "model": record.get("model"),
                "expires_at": expires_at,
                "expires_in_seconds": None if expires_at is None else int(expires_at - now),
                "expired": None if expires_at is None else expires_at <= now,
            }
        )
    return out


def active_session() -> dict | None:
    """Newest live record whose owner runs and whose lease has not expired."""
    records = [r for r in live_instances() if r["alive"] and not r["expired"]]
    if not records:
        return None
    records.sort(key=lambda r: r["expires_at"] or 0, reverse=True)
    return records[0]


def preflight_session() -> dict:
    """Drop dead live-session records and decide whether to reuse or mint one.

    A record is reusable only when its owner process is alive *and* its lease has
    not expired. Records whose owner died are removed; records owned by a live
    process are kept even when expired (that process may renew them) but are
    reported as stale so the run knows it must start a fresh session.
    """
    removed = []
    for path in sorted(CONFIG_DIR.glob("freebuff-live-*.json")):
        record = read_json(path, {}) or {}
        pid = int(record.get("ownerPid") or 0)
        if pid_alive(pid):
            continue
        try:
            path.unlink()
            removed.append(
                {
                    "file": path.name,
                    "pid": pid,
                    "instance_id": record.get("instanceId"),
                    "expired": (
                        record.get("expiresAt") / 1000.0 <= time.time()
                        if isinstance(record.get("expiresAt"), (int, float))
                        else None
                    ),
                }
            )
        except OSError as exc:
            log(f"could not remove stale session file {path.name}: {exc}")
    active = active_session()
    return {
        "action": "reuse" if active else "new",
        "active": active,
        "removed": removed,
        "ignored_stale": [
            r for r in live_instances() if r["alive"] and r["expired"]
        ],
    }


# --------------------------------------------------------------------------- #
# Model catalog and settings
# --------------------------------------------------------------------------- #
_catalog_lock = threading.Lock()
_catalog_cache: dict = {"fetched_at": 0.0, "data": None}
_settings_lock = threading.Lock()


def credentials_target() -> str:
    return f"freebuff-cli/{CONFIG_DIR / 'credentials.json'}"


def api_key() -> str | None:
    """Bearer used by the CLI: env override, else the Windows Credential Manager."""
    env = (os.environ.get("FREEBUFF_API_KEY") or os.environ.get("CODEBUFF_API_KEY") or "").strip()
    if env:
        return env
    if os.name != "nt":
        return None
    try:
        import ctypes
        import ctypes.wintypes as wintypes

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        advapi32 = ctypes.windll.advapi32
        pointer = ctypes.POINTER(CREDENTIAL)()
        if not advapi32.CredReadW(credentials_target(), 1, 0, ctypes.byref(pointer)):
            return None
        try:
            credential = pointer.contents
            blob = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        finally:
            advapi32.CredFree(pointer)
        text = blob.decode("utf-8", "replace").strip()
        try:
            parsed = json.loads(text)
        except Exception:
            return text or None
        for field in ("token", "apiKey", "accessToken", "key"):
            if isinstance(parsed.get(field), str):
                return parsed[field]
        return None
    except Exception as exc:  # pragma: no cover - platform dependent
        log(f"could not read the CLI credential: {exc}")
        return None


def client_version() -> str:
    metadata = read_json(CONFIG_DIR / "freebuff-metadata.json", {}) or {}
    return str(metadata.get("version") or "0.0.0")


def fetch_catalog(force: bool = False) -> dict:
    with _catalog_lock:
        cached = _catalog_cache["data"]
        if cached and not force and time.time() - _catalog_cache["fetched_at"] < CATALOG_TTL_SECONDS:
            return cached
        key = api_key()
        if not key:
            raise FreebuffError(
                "No Freebuff API key found. Expected the Credential Manager entry "
                f"'{credentials_target()}' or FREEBUFF_API_KEY."
            )
        request = urllib.request.Request(
            API_BASE + CATALOG_PATH,
            headers={
                "Authorization": f"Bearer {key}",
                "x-freebuff-client-version": client_version(),
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            body = exc.read(300).decode("utf-8", "replace")
            raise FreebuffError(f"Model catalog request failed: HTTP {exc.code} {body}") from None
        except Exception as exc:
            raise FreebuffError(f"Model catalog request failed: {type(exc).__name__}: {exc}") from None
        if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
            raise FreebuffError("Model catalog response did not contain model rows.")
        _catalog_cache.update(fetched_at=time.time(), data=data)
        return data


def resolve_model(spec: str) -> dict:
    spec = (spec or "").strip()
    if not spec:
        raise FreebuffError("'model' is required: a catalog key like m-096e75164d, or a display name.")
    try:
        rows = fetch_catalog()["rows"]
    except FreebuffError:
        if re.fullmatch(r"m-[0-9a-f]{6,}", spec):
            return {"key": spec, "displayName": None, "efforts": None}
        raise
    for row in rows:
        if row.get("key") == spec:
            return row
    folded = spec.casefold()
    exact = [r for r in rows if str(r.get("displayName", "")).casefold() == folded]
    if len(exact) == 1:
        return exact[0]
    partial = [
        r
        for r in rows
        if folded in str(r.get("displayName", "")).casefold()
        or folded in str(r.get("key", "")).casefold()
    ]
    if len(partial) == 1:
        return partial[0]
    names = ", ".join(str(r.get("displayName")) for r in rows)
    raise FreebuffError(f"Model {spec!r} is not in the catalog. Available: {names}")


def validate_effort(row: dict, effort: str | None) -> str | None:
    if not effort:
        return None
    allowed = row.get("efforts") or []
    if not allowed:
        raise FreebuffError(f"{row.get('displayName') or row['key']} does not accept a reasoning effort.")
    if effort not in allowed:
        raise FreebuffError(
            f"Effort {effort!r} is not valid for {row.get('displayName') or row['key']}. Allowed: {', '.join(allowed)}"
        )
    return effort


def read_settings_text() -> str:
    try:
        return SETTINGS_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "{}\n"


def write_settings_text(text: str) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_PATH.with_suffix(".json.freebuff-mcp.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, SETTINGS_PATH)


def model_from_settings() -> dict:
    settings = read_json(SETTINGS_PATH, {}) or {}
    key = settings.get("freebuffModelKey")
    efforts = settings.get("freebuffReasoningEfforts") or {}
    return {
        "key": key,
        "label": settings.get("freebuffModel"),
        "effort": efforts.get(key) if key else None,
    }


def settings_with_model(settings: dict, row: dict, effort: str | None) -> dict:
    merged = dict(settings)
    merged["freebuffModelKey"] = row["key"]
    if row.get("displayName"):
        merged["freebuffModel"] = row["displayName"]
    if effort:
        efforts = dict(merged.get("freebuffReasoningEfforts") or {})
        efforts[row["key"]] = effort
        merged["freebuffReasoningEfforts"] = efforts
    return merged


class ModelOverride:
    """Temporarily points settings.json at one model for a single run.

    The original bytes are snapshotted next to settings.json so an interrupted
    run can be repaired on the next server start.
    """

    def __init__(self, row: dict, effort: str | None):
        self.row = row
        self.effort = effort
        self.original: str | None = None
        self.written: str | None = None

    def __enter__(self):
        with _settings_lock:
            self.original = read_settings_text()
            try:
                settings = json.loads(self.original)
            except Exception:
                settings = {}
            merged = settings_with_model(settings, self.row, self.effort)
            self.written = json.dumps(merged, ensure_ascii=False, indent=2)
            SETTINGS_SNAPSHOT.write_text(
                json.dumps(
                    {"path": str(SETTINGS_PATH), "original": self.original, "written": self.written},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            write_settings_text(self.written)
        return self

    def __exit__(self, *_exc):
        with _settings_lock:
            try:
                current = read_settings_text()
            except Exception:
                current = None
            if current == self.written and self.original is not None:
                write_settings_text(self.original)
            elif current != self.written:
                log("settings.json changed during the run; keeping the newer content")
            try:
                SETTINGS_SNAPSHOT.unlink()
            except OSError:
                pass
        return False


def recover_settings() -> None:
    """Undo a model override left behind by a crashed run."""
    if not SETTINGS_SNAPSHOT.is_file():
        return
    data = read_json(SETTINGS_SNAPSHOT, {}) or {}
    original = data.get("original")
    written = data.get("written")
    try:
        current = read_settings_text()
        if isinstance(original, str) and current == written:
            write_settings_text(original)
            log("restored settings.json from an interrupted model override")
        else:
            log("kept settings.json: it changed after the interrupted override")
    finally:
        try:
            SETTINGS_SNAPSHOT.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# TUI driver
# --------------------------------------------------------------------------- #
class FreebuffError(RuntimeError):
    pass


class TuiSession:
    """A single Freebuff TUI process driven through a ConPTY."""

    TYPING_CHUNK = 24
    TYPING_DELAY = 0.012

    def __init__(self, cwd: Path, argv: list[str] | None = None, cols: int = 160, rows: int = 48):
        self.cwd = cwd
        self.argv = argv or []
        self.buffer: list[str] = []
        self.cost_notice: str | None = None
        self._lock = threading.Lock()
        self.proc = PtyProcess.spawn(
            [freebuff_binary(), *self.argv], cwd=str(cwd), dimensions=(rows, cols)
        )
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        while True:
            try:
                data = self.proc.read(8192)
            except Exception:
                return
            if not data:
                return
            with self._lock:
                self.buffer.append(data)

    def raw(self) -> str:
        with self._lock:
            return "".join(self.buffer)

    def screen(self) -> str:
        return strip_ansi(self.raw())

    def tail(self, limit: int = 2000) -> str:
        return self.screen()[-limit:]

    def wait_ready(self, timeout: float = 90.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            screen = self.screen()
            if TAKEOVER_MARKER in screen:
                raise FreebuffError(
                    "Freebuff reports another instance holds the live session "
                    "('Freebuff is already running elsewhere'). Close the other "
                    "Freebuff window and retry."
                )
            if any(marker in screen for marker in READY_MARKERS):
                time.sleep(1.5)
                return
            if self.proc.exitstatus is not None:
                raise FreebuffError(
                    "Freebuff exited before its prompt was ready.\n" + self.tail()
                )
            time.sleep(0.4)
        raise FreebuffError(
            "Freebuff prompt did not become ready within %ss.\n%s" % (timeout, self.tail())
        )

    CONFIRM_MARKERS = ("confirm and send", "enter to confirm", "press enter to confirm")

    def _cost_notice(self, screen: str) -> str | None:
        """Pull the "takes N hours from your balance" sentence out of the TUI dump."""
        low = screen.lower()
        start = low.find("takes")
        if start == -1:
            start = low.find("from your wallet")
            if start == -1:
                start = low.find("from your balance")
            if start == -1:
                return None
            start = max(0, start - 90)
        window = screen[max(0, start - 90) : start + 260]
        window = re.split(r"(?:Enter:|Esc:|\x1b)", window)[0]
        text = " ".join(window.split())
        text = re.sub(r"[^\w\s.\-:,/%+()]", "", text).strip(" -:/")
        return text[:200] or None

    def confirm_pending_cost(self) -> bool:
        """Press Enter on the CLI's "spend wallet hours?" prompt, if present."""
        screen = self.screen()
        low = screen.lower()
        if not any(marker in low for marker in self.CONFIRM_MARKERS):
            return False
        notice = self._cost_notice(screen) or "wallet-hours confirmation"
        self.cost_notice = notice
        log(f"auto-confirming wallet cost: {notice}")
        self.proc.write("\r")
        return True

    def type_prompt(self, prompt: str) -> None:
        # The composer submits on Enter; a literal newline would split the paste
        # into a premature submit, so multi-line prompts are flattened.
        text = " ".join(line.strip() for line in prompt.splitlines() if line.strip())
        if not text:
            raise FreebuffError("Prompt is empty after normalization.")
        # Long prompts get dropped by the TUI's input handling when written too
        # fast (observed: ~1.4k chars truncated mid-word), so type them slower.
        length = len(text)
        chunk = 16 if length > 800 else self.TYPING_CHUNK
        delay = 0.03 if length > 800 else self.TYPING_DELAY
        for start in range(0, length, chunk):
            self.proc.write(text[start : start + chunk])
            time.sleep(delay)
        time.sleep(0.8)
        self.proc.write("\r")

        # Free session exhausted => CLI asks to spend wallet hours before sending.
        # The user enabled automatic confirmation, so answer that prompt and
        # record what it said.
        for _ in range(8):
            time.sleep(0.5)
            if self.confirm_pending_cost():
                break

    def close(self) -> None:
        for _ in range(2):
            try:
                self.proc.write("\x03")
            except Exception:
                break
            time.sleep(0.25)
        try:
            self.proc.terminate()
        except Exception:
            pass
        # A TUI that survives teardown keeps its free session alive, so escalate.
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if self.proc.exitstatus is not None:
                return
            time.sleep(0.2)
        pid = getattr(self.proc, "pid", None)
        if pid:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                )
            except Exception:
                log(f"failed to force-kill freebuff pid {pid}")


# --------------------------------------------------------------------------- #
# Chat persistence
# --------------------------------------------------------------------------- #
def chat_dirs(project: Path) -> list[Path]:
    if not project.is_dir():
        return []
    return sorted(
        (p for p in project.glob("chats/*") if p.is_dir()),
        key=lambda p: p.name,
    )


def load_messages(chat: Path) -> list[dict]:
    data = read_json(chat / "chat-messages.json", [])
    return data if isinstance(data, list) else []


def log_counts(chat: Path) -> dict:
    path = chat / "log.jsonl"
    counts = {"sent": 0, "finished": 0, "compactions": 0}
    if not path.is_file():
        return counts
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return counts
    counts["sent"] = text.count("[send-message] Sending message with sdk run config")
    counts["finished"] = text.count("Main prompt finished")
    counts["compactions"] = text.count("Context compaction completed")
    return counts


def summarize_ai(message: dict) -> dict:
    blocks = message.get("blocks") or []
    texts = [b.get("content", "") for b in blocks if b.get("type") == "text"]
    tools = [b for b in blocks if b.get("type") == "tool"]
    tool_counts: dict[str, int] = {}
    for block in tools:
        name = block.get("toolName") or "unknown"
        tool_counts[name] = tool_counts.get(name, 0) + 1
    return {
        "answer": texts[-1] if texts else "",
        "reasoning": "\n\n".join(texts[:-1]),
        "tool_counts": tool_counts,
        "tool_calls": len(tools),
        "is_complete": message.get("isComplete") is True,
        "credits": message.get("credits"),
        "completion_time": message.get("completionTime"),
    }


def find_completed_answer(chat: Path, expected_prompt: str | None, deadline: float, baseline: dict | None = None):
    """Poll the chat dir until the assistant finishes the prompt we sent."""
    base = baseline or {"sent": 0, "finished": 0}
    last_state = {"messages": 0}
    stable_since = None
    while time.time() < deadline:
        counts = log_counts(chat)
        messages = load_messages(chat)
        last_state = counts | {"messages": len(messages)}
        if counts["sent"] > base["sent"]:
            users = [m for m in messages if m.get("variant") == "user"]
            ais = [m for m in messages if m.get("variant") == "ai"]
            if users and ais:
                sent_text = (users[-1].get("content") or "").strip()
                if expected_prompt and sent_text and sent_text != expected_prompt:
                    raise FreebuffError(
                        "Freebuff received a different prompt than requested "
                        "(typed text was mangled).\nrequested: %r\nreceived:  %r"
                        % (expected_prompt, sent_text)
                    )
                summary = summarize_ai(ais[-1])
                if summary["is_complete"] and counts["finished"] > base["finished"]:
                    summary["chat_message_count"] = len(messages)
                    return summary
                if summary["is_complete"] and summary["answer"]:
                    stable_since = stable_since or time.time()
                    if time.time() - stable_since > 4:
                        summary["chat_message_count"] = len(messages)
                        return summary
        time.sleep(1.0)
    raise FreebuffError(
        "Timed out waiting for Freebuff to finish. Last observed state: %s"
        % json.dumps(last_state, ensure_ascii=False)
    )


# --------------------------------------------------------------------------- #
# Tool implementations
# --------------------------------------------------------------------------- #
RUN_LOCK = threading.Lock()


def tool_models(args: dict) -> dict:
    catalog = fetch_catalog(force=bool(args.get("refresh")))
    settings = read_json(SETTINGS_PATH, {}) or {}
    current_key = settings.get("freebuffModelKey")
    efforts = settings.get("freebuffReasoningEfforts") or {}
    rows = []
    for row in catalog["rows"]:
        rows.append(
            {
                "key": row.get("key"),
                "name": row.get("displayName"),
                "tagline": row.get("tagline"),
                "premium": bool(row.get("premium")),
                "access": row.get("access"),
                "efforts": row.get("efforts"),
                "default_effort": row.get("defaultEffort"),
                "context_window": row.get("contextWindow"),
                "warning": row.get("warning"),
                "current": row.get("key") == current_key,
            }
        )
    return {
        "protocol": catalog.get("protocol"),
        "catalog_version": catalog.get("version"),
        "issued_at": catalog.get("issuedAt"),
        "refresh_at": catalog.get("refreshAt"),
        "current": {
            "key": current_key,
            "label": settings.get("freebuffModel"),
            "effort": efforts.get(current_key) if current_key else None,
        },
        "count": len(rows),
        "models": rows,
    }


def tool_set_model(args: dict) -> dict:
    row = resolve_model(args.get("model") or "")
    effort = validate_effort(row, args.get("effort"))
    with _settings_lock:
        original = read_settings_text()
        try:
            settings = json.loads(original)
        except Exception:
            settings = {}
        previous = {
            "key": settings.get("freebuffModelKey"),
            "label": settings.get("freebuffModel"),
        }
        merged = settings_with_model(settings, row, effort)
        write_settings_text(json.dumps(merged, ensure_ascii=False, indent=2))
    return {
        "ok": True,
        "previous": previous,
        "current": {
            "key": row["key"],
            "label": merged.get("freebuffModel"),
            "effort": (merged.get("freebuffReasoningEfforts") or {}).get(row["key"]),
        },
        "applies_to": "the next freebuff_run",
    }


def tool_status(_args: dict) -> dict:
    metadata = read_json(CONFIG_DIR / "freebuff-metadata.json", {}) or {}
    settings = read_json(CONFIG_DIR / "settings.json", {}) or {}
    credentials = read_json(CONFIG_DIR / "credentials.json", {}) or {}
    account = credentials.get("default") or {}
    binary = None
    try:
        binary = freebuff_binary()
    except Exception as exc:  # pragma: no cover - environment dependent
        binary = f"not found: {exc}"
    return {
        "version": metadata.get("version"),
        "target": metadata.get("target"),
        "binary": binary,
        "config_dir": str(CONFIG_DIR),
        "logged_in": bool(account.get("email")),
        "account": {
            "email": account.get("email"),
            "name": account.get("name"),
        },
        "mode": settings.get("mode"),
        "model": settings.get("freebuffModel"),
        "model_key": settings.get("freebuffModelKey"),
        "ads_enabled": settings.get("adsEnabled"),
        "session": {"active": active_session(), "records": live_instances()},
        "live_instances": live_instances(),
        "projects": sorted(p.name for p in PROJECTS_DIR.glob("*") if p.is_dir())
        if PROJECTS_DIR.is_dir()
        else [],
    }


def _normalize(text: str) -> str:
    return " ".join(line.strip() for line in text.splitlines() if line.strip())


def tool_run(args: dict) -> dict:
    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        raise FreebuffError("'prompt' is required.")
    cwd = Path(args.get("cwd") or os.getcwd()).expanduser().resolve()
    if not cwd.is_dir():
        raise FreebuffError(f"Working directory does not exist: {cwd}")
    timeout = float(args.get("timeout_seconds") or 900)
    chat_id = (args.get("chat_id") or "").strip()
    model_spec = (args.get("model") or "").strip()
    model_row = resolve_model(model_spec) if model_spec else None
    model_effort = validate_effort(model_row, args.get("effort")) if model_row else None

    project = project_dir_for(cwd)
    known = {p.name for p in chat_dirs(project)}
    argv = ["--continue", chat_id] if chat_id else []
    if args.get("trust_agents"):
        argv.append("--trust-agents")
    expected = _normalize(prompt)

    if not RUN_LOCK.acquire(blocking=False):
        raise FreebuffError("Another Freebuff run is already in progress in this MCP server.")
    session = None
    started = time.time()
    preflight = preflight_session()
    override = ModelOverride(model_row, model_effort) if model_row else None
    try:
        if override is not None:
            override.__enter__()
        session = TuiSession(cwd, argv=argv)
        session.wait_ready()

        existing = project / "chats" / chat_id if chat_id else None
        if existing and not existing.is_dir():
            raise FreebuffError(f"Unknown chat_id '{chat_id}' for project '{project.name}'.")

        session.type_prompt(prompt)

        chat = existing
        if chat is None:
            discovery_deadline = time.time() + 90
            while time.time() < discovery_deadline and chat is None:
                new = [p for p in chat_dirs(project) if p.name not in known]
                if new:
                    chat = new[-1]
                    break
                # The spend-wallet-hours prompt can appear a beat after Enter.
                session.confirm_pending_cost()
                if session.proc.exitstatus is not None:
                    raise FreebuffError("Freebuff exited before starting a chat.\n" + session.tail())
                time.sleep(0.5)
            if chat is None:
                screen = session.screen()
                hint = ""
                if any(marker in screen for marker in SESSION_LIMIT_MARKERS):
                    hint = " Freebuff reports the free session limit was reached."
                raise FreebuffError(
                    "Freebuff accepted the prompt but never started a chat." + hint + "\n" + session.tail()
                )

        summary = find_completed_answer(
            chat,
            expected,
            time.time() + timeout,
            baseline=log_counts(existing) if existing else None,
        )
        width = max(40, min(int(args.get("transcript_lines") or 0) or 0, 2000))
        transcript = None
        if width:
            transcript = transcript_of(chat, last_n=width)
        result = {
            "ok": True,
            "answer": summary["answer"],
            "reasoning": summary["reasoning"][:8000],
            "tool_counts": summary["tool_counts"],
            "tool_calls": summary["tool_calls"],
            "credits": summary["credits"],
            "completion_time": summary["completion_time"],
            "chat_id": chat.name,
            "chat_dir": str(chat),
            "cwd": str(cwd),
            "project": project.name,
            "elapsed_seconds": round(time.time() - started, 1),
            "cost_confirmation": {
                "auto_confirmed": bool(session.cost_notice),
                "notice": session.cost_notice,
            },
            "session": {
                "preflight_action": preflight["action"],
                "reused": preflight["active"],
                "removed_stale": preflight["removed"],
                "ignored_stale": preflight["ignored_stale"],
                "current": active_session(),
            },
            "model": {
                **(model_from_settings() or {}),
                "source": "override" if model_row else "settings",
            },
        }
        if transcript:
            result["transcript"] = transcript
        return result
    finally:
        if session is not None:
            session.close()
        if override is not None:
            override.__exit__(None, None, None)
        RUN_LOCK.release()


def transcript_of(chat: Path, last_n: int = 0) -> list[dict]:
    messages = load_messages(chat)
    if last_n:
        messages = messages[-last_n:]
    out = []
    for message in messages:
        variant = message.get("variant")
        blocks = message.get("blocks") or []
        text = "".join(
            b.get("content", "") for b in blocks if b.get("type") == "text"
        ) or (message.get("content") or "")
        tools = [
            {
                "name": b.get("toolName"),
                "input": b.get("input"),
                "output": (b.get("output") or "")[:400],
            }
            for b in blocks
            if b.get("type") == "tool"
        ]
        out.append(
            {
                "variant": variant,
                "timestamp": message.get("timestamp"),
                "text": text,
                "tools": tools,
                "is_complete": message.get("isComplete"),
            }
        )
    return out


def tool_chats(args: dict) -> dict:
    cwd = Path(args.get("cwd") or os.getcwd()).expanduser().resolve()
    project = project_dir_for(cwd)
    entries = []
    for chat in chat_dirs(project):
        meta = read_json(chat / "chat-meta.json", {}) or {}
        state = read_json(chat / "run-state.json", {}) or {}
        root = ((state.get("sessionState") or {}).get("fileContext") or {}).get("projectRoot")
        entries.append(
            {
                "chat_id": chat.name,
                "message_count": meta.get("messageCount"),
                "first_prompt": meta.get("firstPrompt"),
                "project_root": root,
                "updated": chat.stat().st_mtime,
            }
        )
    entries.sort(key=lambda e: e["updated"], reverse=True)
    return {"project": project.name, "chat_count": len(entries), "chats": entries[:50]}


def tool_transcript(args: dict) -> dict:
    cwd = Path(args.get("cwd") or os.getcwd()).expanduser().resolve()
    project = project_dir_for(cwd)
    chat_id = (args.get("chat_id") or "").strip()
    chats = chat_dirs(project)
    if not chats:
        raise FreebuffError(f"No Freebuff chats recorded for project '{project.name}'.")
    if chat_id:
        chat = project / "chats" / chat_id
        if not chat.is_dir():
            raise FreebuffError(f"Unknown chat_id '{chat_id}'.")
    else:
        chat = chats[-1]
    last_n = int(args.get("last_n") or 20)
    messages = transcript_of(chat, last_n=last_n)
    return {
        "chat_id": chat.name,
        "chat_dir": str(chat),
        "returned": len(messages),
        "messages": messages,
    }


TOOLS = [
    {
        "name": "freebuff_status",
        "description": (
            "Report the installed Freebuff CLI: version, signed-in account, configured mode/model, "
            "live session instances and known projects."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": tool_status,
    },
    {
        "name": "freebuff_models",
        "description": (
            "List the models the signed-in Freebuff account can use, with catalog keys, reasoning "
            "efforts, context windows and premium flags. Marks the currently configured model."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "refresh": {"type": "boolean", "description": "Ignore the 5-minute catalog cache."}
            },
            "additionalProperties": False,
        },
        "handler": tool_models,
    },
    {
        "name": "freebuff_set_model",
        "description": (
            "Change the Freebuff CLI's model (and optional reasoning effort) in settings.json. "
            "Validated against the live catalog; applies to the next freebuff_run or TUI start."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "Catalog key (m-...) or display name (exact or unique substring)."},
                "effort": {"type": "string", "description": "Optional reasoning effort supported by that model."},
            },
            "required": ["model"],
            "additionalProperties": False,
        },
        "handler": tool_set_model,
    },
    {
        "name": "freebuff_run",
        "description": (
            "Run one coding prompt through the Freebuff CLI (free agent, real file edits in cwd) and "
            "return the assistant's final answer plus tool usage. Freebuff only ships a TUI, so this "
            "bridge drives it through a ConPTY and reads the transcript Freebuff persists. Long-running: "
            "set timeout_seconds for big tasks."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "The task/question to send. Newlines are flattened to spaces."},
                "cwd": {"type": "string", "description": "Working directory Freebuff operates in (defaults to this server's cwd)."},
                "chat_id": {"type": "string", "description": "Existing chat id to continue instead of starting a new chat."},
                "model": {"type": "string", "description": "Per-run model override (catalog key like m-096e75164d, or display name). settings.json is restored afterwards."},
                "effort": {"type": "string", "description": "Reasoning effort for the override (must be one the model supports: low/medium/high/max/xhigh)."},
                "trust_agents": {"type": "boolean", "description": "Pass --trust-agents so Freebuff loads this repo's .agents files and mcp.json without the trust prompt (CI-style). Default false."},
                "timeout_seconds": {"type": "number", "description": "How long to wait for completion (default 900)."},
                "transcript_lines": {"type": "number", "description": "Include the last N transcript messages in the result (0 disables)."},
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
        "handler": tool_run,
    },
    {
        "name": "freebuff_chats",
        "description": "List Freebuff chats persisted for a working directory, newest first.",
        "inputSchema": {
            "type": "object",
            "properties": {"cwd": {"type": "string", "description": "Project working directory."}},
            "additionalProperties": False,
        },
        "handler": tool_chats,
    },
    {
        "name": "freebuff_transcript",
        "description": (
            "Read a persisted Freebuff chat transcript (messages with text and executed tool calls), "
            "for the newest chat or a specific chat_id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cwd": {"type": "string", "description": "Project working directory."},
                "chat_id": {"type": "string", "description": "Chat id (defaults to newest)."},
                "last_n": {"type": "number", "description": "Return only the last N messages (default 20)."},
            },
            "additionalProperties": False,
        },
        "handler": tool_transcript,
    },
]

TOOL_MAP = {tool["name"]: tool for tool in TOOLS}


# --------------------------------------------------------------------------- #
# JSON-RPC / MCP plumbing (stdio, newline-delimited)
# --------------------------------------------------------------------------- #
def send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def reply(msg_id, result=None, error=None) -> None:
    payload = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    send(payload)


def text_result(payload, is_error: bool = False) -> dict:
    body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": body}], "isError": is_error}


def handle(request: dict) -> None:
    method = request.get("method")
    msg_id = request.get("id")

    if method == "initialize":
        requested = (request.get("params") or {}).get("protocolVersion") or PROTOCOL_VERSION
        reply(
            msg_id,
            {
                "protocolVersion": requested,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
        return

    if method in ("notifications/initialized", "notifications/cancelled", "initialized"):
        return

    if method == "ping":
        reply(msg_id, {})
        return

    if method == "tools/list":
        reply(
            msg_id,
            {
                "tools": [
                    {k: tool[k] for k in ("name", "description", "inputSchema")}
                    for tool in TOOLS
                ]
            },
        )
        return

    if method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        tool = TOOL_MAP.get(name)
        if tool is None:
            reply(msg_id, text_result(f"Unknown tool: {name}", is_error=True))
            return
        try:
            reply(msg_id, text_result(tool["handler"](args)))
        except FreebuffError as exc:
            reply(msg_id, text_result(f"{exc}", is_error=True))
        except Exception as exc:  # noqa: BLE001
            log(traceback.format_exc())
            reply(msg_id, text_result(f"{type(exc).__name__}: {exc}", is_error=True))
        return

    if msg_id is not None:
        reply(msg_id, error={"code": -32601, "message": f"Method not found: {method}"})


def main() -> None:
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    log(f"ready (pid {os.getpid()}, config {CONFIG_DIR})")
    recover_settings()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except Exception:
            log(f"ignored non-JSON input: {line[:120]}")
            continue
        if isinstance(request, list):
            for item in request:
                handle(item)
        else:
            handle(request)


if __name__ == "__main__":
    main()
