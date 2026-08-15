#!/usr/bin/env python3
"""Independent Telegram control plane for recovering Hermes without an LLM."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import signal
import stat
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit

import httpx
import yaml
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    MenuButtonCommands,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import NetworkError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

ROOT = Path(__file__).resolve().parent
from rescue_core import (  # noqa: E402
    CredentialCommitError,
    CredentialError,
    CredentialRecoveryError,
    import_cpa_bundle,
)

ALLOWED_USER_ID = int(os.environ.get("RESCUE_ALLOWED_USER_ID", "0"))
PROJECT_REPAIR_ENABLED = os.environ.get(
    "RESCUE_ENABLE_PROJECT_REPAIR", "0"
).strip() == "1"
def _configured_path(key: str, default: Path | None = None) -> Path | None:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise RuntimeError(f"{key} must not contain control characters")
    path = Path(raw)
    if not path.is_absolute():
        raise RuntimeError(f"{key} must be an absolute path")
    return path


PROJECT_REPO = _configured_path("RESCUE_PROJECT_REPO")
_project_name_value = re.sub(
    r"[^\w .:+()-]", "", os.environ.get("RESCUE_PROJECT_NAME", "")
).strip()
PROJECT_NAME = _project_name_value[:80] or "Managed Project"
_UNIT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@:-]*\.service")
MAX_UNIT_NAME_LENGTH = 255


def _service_name(key: str, default: str) -> str:
    configured = os.environ.get(key)
    value = default if configured is None or configured == "" else configured
    if (
        len(value.encode("utf-8")) > MAX_UNIT_NAME_LENGTH
        or ".." in value
        or not _UNIT_RE.fullmatch(value)
    ):
        raise RuntimeError(f"{key} must be a safe systemd service unit name")
    return value


GATEWAY_SERVICE = _service_name("RESCUE_GATEWAY_SERVICE", "hermes-gateway.service")
CPA_SERVICE = _service_name("RESCUE_CPA_SERVICE", "hermes-rescue-cpa.service")
PROJECT_SERVICE = _service_name("RESCUE_PROJECT_SERVICE", "hermes-managed-project.service")
TRUSTED_UV_PYTHON_ROOT = Path.home() / ".local/share/uv/python"
STATE_ROOT = Path.home() / ".local" / "state" / "hermes-rescue-bot"
CPA_AUTH_DIR = _configured_path("CPA_AUTH_DIR", STATE_ROOT / "cpa-auth")
CPA_CONFIG = _configured_path("CPA_CONFIG", STATE_ROOT / "cpa-config.yaml")
AGY = os.environ.get("AGY_BIN", "").strip() or shutil.which("agy") or ""
BWRAP = os.environ.get("BWRAP_BIN", "").strip() or shutil.which("bwrap") or "/usr/bin/bwrap"
AGY_STATE_DIR = Path.home() / ".gemini" / "antigravity-cli"
AGY_TOKEN = _configured_path(
    "AGY_TOKEN_PATH", AGY_STATE_DIR / "antigravity-oauth-token"
)
AGY_SANDBOX_ROOT = STATE_ROOT / "agy-sandbox"
AGY_SANDBOX_STATE_DIR = AGY_SANDBOX_ROOT / "chat-state"
AGY_TASK_STATE_DIR = AGY_SANDBOX_ROOT / "task-state"
AGY_PROJECT_READ_STATE_DIR = AGY_SANDBOX_ROOT / "project-read-state"
AGY_SANDBOX_CONFIG_DIR = AGY_SANDBOX_ROOT / "chat-config"
AGY_TASK_CONFIG_DIR = AGY_SANDBOX_ROOT / "task-config"
AGY_PROJECT_READ_CONFIG_DIR = AGY_SANDBOX_ROOT / "project-read-config"
MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_OUTPUT = 14_000
MAX_SUBPROCESS_CAPTURE = 256 * 1024
MAX_HTTP_RESPONSE = 1024 * 1024
MAX_ARTIFACT_BYTES = 45 * 1024 * 1024
MAX_ARTIFACTS_PER_REPLY = 10
MAX_ARTIFACT_SCAN_ENTRIES = 10_000
MAX_ARTIFACT_SCAN_DIRECTORIES = 1_000
ARTIFACT_SUFFIXES = frozenset({
    ".doc", ".docx", ".rtf", ".odt", ".xls", ".xlsx", ".csv", ".ppt",
    ".pptx", ".pdf", ".txt", ".md", ".json", ".yaml", ".yml", ".xml",
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp3", ".m4a", ".ogg",
    ".wav", ".mp4", ".mov", ".webm", ".zip", ".tar", ".gz", ".7z",
})
CHAT_DIR = STATE_ROOT / "agy-chat"
ARTIFACT_STAGING_DIR = STATE_ROOT / "artifact-staging"
CHAT_SESSION_DIR = STATE_ROOT / "chat-sessions"
CPA_BASE_URL = os.environ.get("CPA_BASE_URL", "http://127.0.0.1:8317").rstrip("/")
_cpa_url = urlsplit(CPA_BASE_URL)
if _cpa_url.scheme not in {"http", "https"} or _cpa_url.hostname not in {
    "127.0.0.1", "::1", "localhost"
} or _cpa_url.username or _cpa_url.password:
    raise RuntimeError("CPA_BASE_URL must be a loopback HTTP(S) URL")
ACTIVE_PROCS: dict[int, asyncio.subprocess.Process] = {}
JOB_STARTING: set[int] = set()
CHAT_PRESPAWN: set[int] = set()
JOB_CANCEL_REQUESTED: set[int] = set()
SPAWN_REAPERS: set[asyncio.Task[None]] = set()
SPAWN_TASKS: set[asyncio.Task[asyncio.subprocess.Process]] = set()
CREDENTIAL_IMPORT_TASKS: set[asyncio.Task] = set()
CREDENTIAL_WRITES_QUARANTINED = False
ACTIVE_REPAIR_ROOTS: set[Path] = set()
PENDING_CONFIRMATIONS: dict[str, tuple[int, int, str, str, float]] = {}
CHAT_LOCK = asyncio.Lock()
MUTATION_LOCK = asyncio.Lock()
NO_LINK_PREVIEW = LinkPreviewOptions(is_disabled=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
# httpx logs full Telegram file-download URLs at INFO; those URLs embed the
# bot token. Never persist them in journald.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
log = logging.getLogger("hermes-rescue-bot")


def allowed(update: Update) -> bool:
    return bool(
        update.effective_user
        and update.effective_user.id == ALLOWED_USER_ID
        and update.effective_chat
        and update.effective_chat.type == "private"
    )


def document_size_allowed(file_size: int | None) -> bool:
    return file_size is not None and 0 < file_size <= MAX_DOCUMENT_BYTES


def sanitized_child_env(source: dict[str, str] | None = None) -> dict[str, str]:
    """Return a strict environment allowlist for every spawned subprocess."""
    source = os.environ if source is None else source
    allowed = {
        "HOME",
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "COLORTERM",
        "TMPDIR",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "XDG_RUNTIME_DIR",
        "NO_COLOR",
    }
    # Filter the host environment to a strict allowlist.
    env = {key: value for key, value in source.items() if key in allowed}

    # Explicitly purge any sensitive control plane tokens that might have
    # leaked into the process environment (e.g. from service manager).
    for sensitive in ("RESCUE_BOT_TOKEN", "TELEGRAM_TOKEN", "API_KEY", "SECRET_KEY"):
        env.pop(sensitive, None)

    env.setdefault("HOME", str(Path.home()))
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env.setdefault("LANG", "C.UTF-8")
    return env


def _isolated_agy_state_dirs() -> tuple[Path, ...]:
    return (
        AGY_SANDBOX_STATE_DIR,
        AGY_TASK_STATE_DIR,
        AGY_PROJECT_READ_STATE_DIR,
    )


def _isolated_agy_config_dirs() -> tuple[Path, ...]:
    return (
        AGY_SANDBOX_CONFIG_DIR,
        AGY_TASK_CONFIG_DIR,
        AGY_PROJECT_READ_CONFIG_DIR,
    )


def prepare_agy_sandbox_storage() -> None:
    """Create non-secret writable state used by non-login AGY sandboxes."""
    directories = (
        AGY_SANDBOX_ROOT,
        *_isolated_agy_state_dirs(),
        *_isolated_agy_config_dirs(),
    )
    for directory in directories:
        if directory.is_symlink():
            raise RuntimeError(f"AGY sandbox path must not be a symlink: {directory}")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise RuntimeError(f"AGY sandbox path is not a trusted owned directory: {directory}")
        os.chmod(directory, 0o700)

    # Bubblewrap needs a destination file for the read-only AGY-token
    # overmount. This placeholder never contains a credential.
    for state_dir in _isolated_agy_state_dirs():
        placeholder = state_dir / AGY_TOKEN.name
        if placeholder.is_symlink():
            raise RuntimeError(f"AGY token mountpoint must not be a symlink: {placeholder}")
        if not placeholder.exists():
            descriptor = os.open(
                placeholder,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            os.close(descriptor)
        info = placeholder.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise RuntimeError(f"AGY token mountpoint is not a private regular file: {placeholder}")
        os.chmod(placeholder, 0o600)


@contextmanager
def ephemeral_agy_repair_storage() -> Iterator[tuple[Path, Path]]:
    """Yield fresh repair-only AGY state/config and remove them afterwards."""
    prepare_agy_sandbox_storage()
    with tempfile.TemporaryDirectory(
        prefix="repair-run-",
        dir=AGY_SANDBOX_ROOT,
    ) as directory:
        run_root = Path(directory)
        state_dir = run_root / "state"
        config_dir = run_root / "config"
        for path in (run_root, state_dir, config_dir):
            if path.is_symlink():
                raise RuntimeError("ephemeral AGY repair path must not be a symlink")
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise RuntimeError("ephemeral AGY repair path is not trusted")
            os.chmod(path, 0o700)
        placeholder = state_dir / AGY_TOKEN.name
        descriptor = os.open(
            placeholder,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        os.close(descriptor)
        ACTIVE_REPAIR_ROOTS.add(run_root)
        try:
            yield state_dir, config_dir
        finally:
            ACTIVE_REPAIR_ROOTS.discard(run_root)


def _isolated_agy_state(mode: str) -> Path:
    return {
        "chat": AGY_SANDBOX_STATE_DIR,
        "task": AGY_TASK_STATE_DIR,
        "project-read": AGY_PROJECT_READ_STATE_DIR,
    }[mode]


def _isolated_agy_config(mode: str) -> Path:
    return {
        "chat": AGY_SANDBOX_CONFIG_DIR,
        "task": AGY_TASK_CONFIG_DIR,
        "project-read": AGY_PROJECT_READ_CONFIG_DIR,
    }[mode]


async def _await_task_uninterruptibly(task: asyncio.Task):
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError as cancelled:
            if task.cancelled():
                raise
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
            continue


def _lstat_without_symlink_components(path: Path, *, label: str) -> os.stat_result:
    if not path.is_absolute():
        raise RuntimeError(f"{label} path must be absolute")
    normalized = Path(os.path.normpath(str(path)))
    if normalized != path:
        raise RuntimeError(f"{label} path must be normalized")
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError as exc:
            raise RuntimeError(f"{label} path does not exist: {path}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError(f"{label} path must not contain symlinks: {path}")
    return path.lstat()


def _validate_regular_mount_source(
    path: Path,
    *,
    label: str,
    allow_root_owner: bool = False,
    executable: bool = False,
    secret: bool = False,
) -> None:
    info = _lstat_without_symlink_components(path, label=label)
    allowed_owners = {os.getuid()}
    if allow_root_owner:
        # User-level systemd mount namespaces can map host root-owned files to
        # the standard overflow uid while preserving their immutable mode.
        allowed_owners.update({0, 65534})
    if not stat.S_ISREG(info.st_mode) or info.st_uid not in allowed_owners or info.st_nlink != 1:
        raise RuntimeError(f"{label} must be a trusted regular file")
    permissions = stat.S_IMODE(info.st_mode)
    if permissions & 0o022:
        raise RuntimeError(f"{label} must not be group/other writable")
    if secret and permissions & 0o077:
        raise RuntimeError(f"{label} must not be accessible by group/other")
    if executable and not os.access(path, os.X_OK):
        raise RuntimeError(f"{label} is not executable")


def _validate_owned_mount_directory(path: Path, *, label: str) -> None:
    info = _lstat_without_symlink_components(path, label=label)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError(f"{label} must be a trusted owned directory")
    if stat.S_IMODE(info.st_mode) & 0o022:
        raise RuntimeError(f"{label} must not be group/other writable")


def _validate_project_repository() -> None:
    if PROJECT_REPO is None or not PROJECT_REPO.is_absolute():
        raise RuntimeError("managed project repository is not configured as an absolute path")
    configured = PROJECT_REPO.resolve(strict=True)
    if configured != PROJECT_REPO or configured == Path(configured.anchor):
        raise RuntimeError("managed project repository path is too broad or not normalized")
    _validate_owned_mount_directory(configured, label="managed project repository")
    _validate_owned_mount_directory(configured / ".git", label="managed project .git")


def _decode_mountinfo_path(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def _nested_mountpoints(root: Path) -> list[Path]:
    root = root.resolve(strict=True)
    nested: list[Path] = []
    try:
        lines = Path("/proc/self/mountinfo").read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except OSError as exc:
        raise RuntimeError("cannot inspect nested mounts") from exc
    for line in lines:
        fields = line.split()
        if len(fields) < 6:
            continue
        mountpoint = Path(_decode_mountinfo_path(fields[4]))
        try:
            relative = mountpoint.relative_to(root)
        except ValueError:
            continue
        if relative.parts:
            nested.append(mountpoint)
    return nested


def _validate_readonly_tree(
    root: Path,
    *,
    label: str,
    allowed_external_root: Path | None = None,
) -> None:
    root = root.resolve(strict=True)
    _validate_owned_mount_directory(root, label=label)
    if _nested_mountpoints(root):
        raise RuntimeError(f"{label} contains a nested mount")
    allowed_external = (
        allowed_external_root.resolve(strict=True)
        if allowed_external_root is not None
        else None
    )
    root_device = root.lstat().st_dev
    pending = [root]
    entries = 0
    while pending:
        directory = pending.pop()
        try:
            children = list(os.scandir(directory))
        except OSError as exc:
            raise RuntimeError(f"cannot scan {label}") from exc
        for child in children:
            entries += 1
            if entries > 100_000:
                raise RuntimeError(f"{label} is too large to validate")
            path = Path(child.path)
            try:
                info = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise RuntimeError(f"cannot inspect {label} entry") from exc
            if stat.S_ISLNK(info.st_mode):
                try:
                    target = path.resolve(strict=True)
                except (OSError, RuntimeError) as exc:
                    raise RuntimeError(f"{label} contains a broken symbolic link") from exc
                inside = target.is_relative_to(root)
                allowed = bool(
                    allowed_external is not None
                    and target.is_relative_to(allowed_external)
                )
                if not inside and not allowed:
                    raise RuntimeError(f"{label} symbolic link escapes its approved roots")
                target_info = target.lstat()
                if (
                    target_info.st_uid != os.getuid()
                    or not (
                        stat.S_ISDIR(target_info.st_mode)
                        or stat.S_ISREG(target_info.st_mode)
                    )
                ):
                    raise RuntimeError(f"{label} symbolic link target is untrusted")
                continue
            if info.st_uid != os.getuid():
                raise RuntimeError(f"{label} entry has an untrusted owner")
            if stat.S_IMODE(info.st_mode) & 0o022:
                raise RuntimeError(f"{label} entry is group/other writable")
            if info.st_dev != root_device:
                raise RuntimeError(f"{label} crosses a nested filesystem")
            if stat.S_ISDIR(info.st_mode):
                pending.append(path)
            elif stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    raise RuntimeError(f"{label} contains a hard link")
            else:
                raise RuntimeError(f"{label} contains a special file")


def validate_project_repair_tree(root: Path | None = None) -> None:
    root = (PROJECT_REPO if root is None else root).resolve(strict=True)
    trusted_uv_root: Path | None = None
    venv = root / ".venv"
    if venv.exists():
        trusted_uv_root = TRUSTED_UV_PYTHON_ROOT.resolve(strict=True)
        _validate_readonly_tree(trusted_uv_root, label="trusted uv Python root")
        _validate_readonly_tree(
            venv,
            label="Managed Project virtual environment",
            allowed_external_root=trusted_uv_root,
        )
    _validate_owned_mount_directory(root, label="repair repository")
    nested = _nested_mountpoints(root)
    if nested:
        raise RuntimeError("repair repository contains a nested mount")
    root_info = root.lstat()
    root_device = root_info.st_dev
    pending = [root]
    entries = 0
    while pending:
        directory = pending.pop()
        try:
            children = list(os.scandir(directory))
        except OSError as exc:
            raise RuntimeError("cannot scan repair repository") from exc
        for child in children:
            entries += 1
            if entries > 250_000:
                raise RuntimeError("repair repository is too large to validate")
            try:
                info = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise RuntimeError("cannot inspect repair repository entry") from exc
            path = Path(child.path)
            if stat.S_ISLNK(info.st_mode):
                try:
                    target = path.resolve(strict=True)
                except (OSError, RuntimeError) as exc:
                    raise RuntimeError(
                        f"repair repository contains a broken symbolic link: {path}"
                    ) from exc
                inside_repository = target.is_relative_to(root)
                allowed_venv_runtime = bool(
                    trusted_uv_root is not None
                    and path.is_relative_to(venv)
                    and target.is_relative_to(trusted_uv_root)
                )
                if not inside_repository and not allowed_venv_runtime:
                    raise RuntimeError(
                        f"repair repository symbolic link points outside repository: {path}"
                    )
                target_info = target.lstat()
                if (
                    target_info.st_uid != os.getuid()
                    or target_info.st_dev != root_device
                    or not (
                        stat.S_ISDIR(target_info.st_mode)
                        or stat.S_ISREG(target_info.st_mode)
                    )
                ):
                    raise RuntimeError(
                        f"repair repository symbolic link target is untrusted: {path}"
                    )
                continue
            if info.st_uid != os.getuid():
                raise RuntimeError(f"repair repository entry has an untrusted owner: {path}")
            if info.st_dev != root_device:
                raise RuntimeError(f"repair repository crosses a nested filesystem: {path}")
            if stat.S_ISDIR(info.st_mode):
                pending.append(path)
            elif stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    raise RuntimeError(f"repair repository contains a hard link: {path}")
            else:
                raise RuntimeError(f"repair repository contains a special file: {path}")


def cleanup_stale_repair_storage(
    *, now: float | None = None, min_age: float = 3600
) -> int:
    prepare_agy_sandbox_storage()
    if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
        raise RuntimeError("safe stale repair cleanup is unavailable")
    now = time.time() if now is None else now
    sandbox_root = AGY_SANDBOX_ROOT.resolve(strict=True)
    removed = 0
    for child in sandbox_root.iterdir():
        if not child.name.startswith("repair-run-") or child in ACTIVE_REPAIR_ROOTS:
            continue
        try:
            info = child.lstat()
        except FileNotFoundError:
            continue
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o022
            or now - info.st_mtime < min_age
        ):
            continue
        shutil.rmtree(child)
        removed += 1
    return removed


def _validate_repair_overrides(state_dir: Path, config_dir: Path) -> None:
    run_root = state_dir.parent
    if (
        config_dir.parent != run_root
        or state_dir.name != "state"
        or config_dir.name != "config"
        or run_root not in ACTIVE_REPAIR_ROOTS
    ):
        raise RuntimeError("repair overrides are not from an active ephemeral context")
    sandbox_root = AGY_SANDBOX_ROOT.resolve(strict=True)
    if run_root.parent != sandbox_root or not run_root.name.startswith("repair-run-"):
        raise RuntimeError("repair overrides are outside the repair sandbox root")
    _validate_owned_mount_directory(run_root, label="repair run root")
    _validate_owned_mount_directory(state_dir, label="repair AGY state")
    _validate_owned_mount_directory(config_dir, label="repair AGY config")
    token_mountpoint = state_dir / AGY_TOKEN.name
    _validate_regular_mount_source(token_mountpoint, label="repair token mountpoint")


def validate_agy_mount_sources(mode: str) -> None:
    _validate_regular_mount_source(
        Path(BWRAP), label="Bubblewrap", allow_root_owner=True, executable=True
    )
    _validate_regular_mount_source(
        Path(AGY), label="AGY executable", allow_root_owner=True, executable=True
    )
    agy_real = Path(AGY).parent / "agy-real"
    if agy_real.exists():
        _validate_regular_mount_source(
            agy_real, label="AGY real binary", allow_root_owner=True, executable=True
        )
    _validate_owned_mount_directory(AGY_STATE_DIR, label="AGY state directory")
    _validate_regular_mount_source(AGY_TOKEN, label="AGY token", secret=True)
    if mode in {"chat", "task"}:
        _validate_owned_mount_directory(CHAT_DIR, label="AGY chat workspace")
    if mode in {"project-read", "project-repair"}:
        _validate_project_repository()


def build_safe_git_status_args() -> list[str]:
    """Return a non-interactive git status command with executable extensions disabled."""
    return [
        "/usr/bin/git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.pager=cat",
        "-c",
        "pager.status=false",
        "-c",
        "credential.helper=",
        "-c",
        "diff.external=",
        "-c",
        "core.attributesFile=/dev/null",
        "-c",
        "core.excludesFile=/dev/null",
        "-C",
        str(PROJECT_REPO),
        "status",
        "--short",
        "--untracked-files=no",
        "--ignore-submodules=all",
    ]


def _validate_system_runtime_source(path: Path, *, label: str) -> os.stat_result:
    """Validate runtime material protected by non-writable directory ancestry."""
    if not path.is_absolute() or Path(os.path.normpath(str(path))) != path:
        raise RuntimeError(f"{label} path must be absolute and normalized")
    current = Path(path.anchor)
    for component in path.parts[1:-1]:
        current /= component
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError(f"{label} parent path must not contain symlinks")
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise RuntimeError(f"{label} parent path is not trusted")
    info = path.lstat()
    if not (
        stat.S_ISREG(info.st_mode)
        or stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
    ):
        raise RuntimeError(f"{label} has an unsupported type")
    if not stat.S_ISLNK(info.st_mode) and stat.S_IMODE(info.st_mode) & 0o022:
        raise RuntimeError(f"{label} is group/other writable")
    return info


def _append_destination_dirs(
    args: list[str], destination: Path, created: set[Path]
) -> None:
    """Create missing destination parents in stable top-down order."""
    if not destination.is_absolute() or Path(os.path.normpath(str(destination))) != destination:
        raise RuntimeError("sandbox destination must be absolute and normalized")
    if any(destination.is_relative_to(root) for root in map(Path, ("/usr", "/bin", "/sbin", "/lib", "/lib64"))):
        return
    current = Path(destination.anchor)
    for component in destination.parent.parts[1:]:
        current /= component
        if current not in created:
            args.extend(["--dir", str(current)])
            created.add(current)


def _minimal_runtime_root_args() -> tuple[list[str], set[Path]]:
    """Build an empty Bubblewrap root containing only Linux runtime material."""
    args = [
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--tmpfs", "/run",
    ]
    created = {Path("/proc"), Path("/dev"), Path("/tmp"), Path("/run")}

    usr = Path("/usr")
    usr_info = _validate_system_runtime_source(usr, label="system /usr")
    if not stat.S_ISDIR(usr_info.st_mode):
        raise RuntimeError("system /usr must be a directory")
    args.extend(["--ro-bind", str(usr), str(usr)])
    created.add(usr)

    for name in ("bin", "sbin", "lib", "lib64"):
        source = Path("/") / name
        try:
            info = _validate_system_runtime_source(source, label=f"system {source}")
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(source)
            resolved = source.resolve(strict=True)
            expected = usr / name
            if resolved != expected or not stat.S_ISDIR(resolved.lstat().st_mode):
                raise RuntimeError(f"system {source} has an unexpected symlink target")
            args.extend(["--symlink", target, str(source)])
        elif stat.S_ISDIR(info.st_mode):
            args.extend(["--ro-bind", str(source), str(source)])
        else:
            raise RuntimeError(f"system {source} must be a directory or merged-/usr symlink")
        created.add(source)

    runtime_material = (
        "/etc/resolv.conf",
        "/etc/hosts",
        "/etc/host.conf",
        "/etc/nsswitch.conf",
        "/etc/gai.conf",
        "/etc/localtime",
        "/etc/ssl/certs",
        "/etc/ssl/cert.pem",
        "/etc/pki/tls/certs",
        "/etc/pki/ca-trust/extracted",
        "/etc/ca-certificates.conf",
        "/etc/ld.so.cache",
        "/etc/ld.so.conf",
        "/etc/ld.so.conf.d",
    )
    for value in runtime_material:
        configured = Path(value)
        if not configured.exists():
            continue
        source = configured.resolve(strict=True)
        info = _validate_system_runtime_source(source, label=f"runtime source {configured}")
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise RuntimeError(f"runtime source {configured} has an unsupported type")
        _append_destination_dirs(args, configured, created)
        args.extend(["--ro-bind", str(source), str(configured)])
        created.add(configured)
    return args, created


def build_diagnostic_sandbox_args(
    inner_args: list[str],
    *,
    include_repo: bool = False,
    mounted_executables: tuple[Path, ...] = (),
) -> list[str]:
    """Build a no-network, no-token diagnostic sandbox around trusted commands."""
    _validate_regular_mount_source(
        Path(BWRAP), label="Bubblewrap", allow_root_owner=True, executable=True
    )
    if include_repo:
        _validate_project_repository()
    for executable in mounted_executables:
        _validate_regular_mount_source(
            executable,
            label=f"diagnostic executable {executable.name}",
            allow_root_owner=True,
            executable=True,
        )

    home = Path.home()
    runtime_args, created_dirs = _minimal_runtime_root_args()
    _append_destination_dirs(runtime_args, home, created_dirs)
    args = [
        BWRAP,
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        *runtime_args,
        "--tmpfs",
        str(home),
    ]
    created_dirs.add(home)
    for executable in mounted_executables:
        try:
            relative_parent = executable.parent.relative_to(home)
        except ValueError as exc:
            raise RuntimeError("diagnostic extra executable must be inside user home") from exc
        current = home
        for component in relative_parent.parts:
            current /= component
            if current not in created_dirs:
                args.extend(["--dir", str(current)])
                created_dirs.add(current)
        args.extend(["--ro-bind", str(executable), str(executable)])
    if include_repo:
        _append_destination_dirs(args, PROJECT_REPO, created_dirs)
        args.extend(["--ro-bind", str(PROJECT_REPO), str(PROJECT_REPO)])
    args.extend(
        [
            "--clearenv",
            "--setenv",
            "HOME",
            str(home),
            "--setenv",
            "PATH",
            "/usr/bin:/bin",
            "--setenv",
            "LANG",
            "C.UTF-8",
            "--setenv",
            "LC_ALL",
            "C.UTF-8",
            "--setenv",
            "GIT_CONFIG_NOSYSTEM",
            "1",
            "--setenv",
            "GIT_CONFIG_GLOBAL",
            "/dev/null",
            "--setenv",
            "GIT_PAGER",
            "cat",
            "--setenv",
            "PAGER",
            "cat",
            "--chdir",
            str(PROJECT_REPO if include_repo else Path("/tmp")),
            "--",
            *inner_args,
        ]
    )
    return args


def build_agy_sandbox_args(
    inner_args: list[str],
    *,
    mode: str,
    state_dir_override: Path | None = None,
    config_dir_override: Path | None = None,
) -> list[str]:
    """Wrap AGY in a mandatory Bubblewrap filesystem/process sandbox."""
    if mode not in {"chat", "task", "project-read", "project-repair"}:
        raise ValueError(f"unsupported AGY sandbox mode: {mode}")
    if mode == "project-repair":
        if state_dir_override is None or config_dir_override is None:
            raise RuntimeError("Managed Project repair requires ephemeral AGY state and config")
        _validate_repair_overrides(state_dir_override, config_dir_override)
        validate_project_repair_tree(PROJECT_REPO)
    elif state_dir_override is not None or config_dir_override is not None:
        raise RuntimeError("AGY state overrides are only allowed for repair mode")
    prepare_agy_sandbox_storage()
    validate_agy_mount_sources(mode)

    home_path = Path.home()
    home = str(home_path)
    runtime_args, created_dirs = _minimal_runtime_root_args()
    _append_destination_dirs(runtime_args, home_path, created_dirs)
    args = [
        BWRAP,
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--share-net",
        *runtime_args,
        "--tmpfs",
        home,
        "--dir",
        f"{home}/.local",
        "--dir",
        f"{home}/.local/bin",
    ]
    created_dirs.update({home_path, home_path / ".local", home_path / ".local/bin", home_path / ".gemini"})
    _append_destination_dirs(args, Path(AGY), created_dirs)
    args.extend(["--ro-bind", AGY, AGY])
    agy_real = Path(AGY).parent / "agy-real"
    if agy_real.exists():
        _append_destination_dirs(args, agy_real, created_dirs)
        args.extend(["--ro-bind", str(agy_real), str(agy_real)])
    args.extend(["--dir", f"{home}/.gemini"])

    sandbox_config = AGY_STATE_DIR.parent / "config"
    args.extend(["--dir", str(sandbox_config)])
    isolated_state = state_dir_override or _isolated_agy_state(mode)
    isolated_config = config_dir_override or _isolated_agy_config(mode)
    sandbox_token = AGY_STATE_DIR / AGY_TOKEN.name
    args.extend([
        "--dir", str(AGY_STATE_DIR), "--bind", str(isolated_state), str(AGY_STATE_DIR),
        "--ro-bind", str(AGY_TOKEN), str(sandbox_token),
        "--bind", str(isolated_config), str(sandbox_config),
    ])

    # Prevent direct host-service control even if repository content injects a
    # malicious instruction. The bot performs any approved service restart.
    for blocked in (
        "/usr/bin/systemctl",
        "/usr/bin/busctl",
        "/usr/bin/loginctl",
        "/usr/bin/machinectl",
        "/usr/bin/pkexec",
        "/usr/bin/sudo",
        "/usr/bin/su",
        "/usr/bin/nsenter",
        "/usr/bin/unshare",
        "/usr/bin/bwrap",
        "/usr/bin/aa-exec",
        "/usr/sbin/aa-exec",
        "/usr/bin/mount",
        "/usr/bin/umount",
    ):
        if Path(blocked).exists():
            args.extend(["--ro-bind", "/dev/null", blocked])

    if mode in {"chat", "task"}:
        args.extend(
            [
                "--dir",
                str(CHAT_DIR),
                "--bind",
                str(CHAT_DIR),
                str(CHAT_DIR),
                "--chdir",
                str(CHAT_DIR),
            ]
        )
    elif mode in {"project-read", "project-repair"}:
        mount_flag = "--ro-bind" if mode == "project-read" else "--bind"
        _append_destination_dirs(args, PROJECT_REPO, created_dirs)
        args.extend(
            [
                "--dir",
                str(PROJECT_REPO),
                mount_flag,
                str(PROJECT_REPO),
                str(PROJECT_REPO),
            ]
        )
        if mode == "project-repair":
            venv = PROJECT_REPO / ".venv"
            if venv.exists():
                for destination in (home_path / ".local/share/uv", TRUSTED_UV_PYTHON_ROOT):
                    _append_destination_dirs(args, destination, created_dirs)
                args.extend([
                    "--ro-bind",
                    str(venv),
                    str(venv),
                    "--dir",
                    f"{home}/.local/share",
                    "--dir",
                    f"{home}/.local/share/uv",
                    "--dir",
                    str(TRUSTED_UV_PYTHON_ROOT),
                    "--ro-bind",
                    str(TRUSTED_UV_PYTHON_ROOT),
                    str(TRUSTED_UV_PYTHON_ROOT),
                ])
        args.extend(["--chdir", str(PROJECT_REPO)])
    else:
        args.extend(["--chdir", "/tmp"])

    return [*args, "--", *inner_args]


def set_pending_confirmation(
    user_id: int,
    action: str,
    payload: str,
    *,
    chat_id: int,
    nonce: str | None = None,
    now: float | None = None,
    ttl: float = 300.0,
) -> str:
    for old_nonce, pending in list(PENDING_CONFIRMATIONS.items()):
        if pending[0] == user_id:
            PENDING_CONFIRMATIONS.pop(old_nonce, None)
    current = time.monotonic() if now is None else now
    selected = nonce or secrets.token_urlsafe(9)
    PENDING_CONFIRMATIONS[selected] = (
        user_id,
        chat_id,
        action,
        payload,
        current + ttl,
    )
    return selected


def take_pending_confirmation(
    nonce: str,
    *,
    user_id: int,
    chat_id: int,
    now: float | None = None,
) -> tuple[str, str] | None:
    pending = PENDING_CONFIRMATIONS.get(nonce)
    if pending is None:
        return None
    expected_user, expected_chat, action, payload, expires_at = pending
    current = time.monotonic() if now is None else now
    if current > expires_at:
        PENDING_CONFIRMATIONS.pop(nonce, None)
        return None
    if expected_user != user_id or expected_chat != chat_id:
        return None
    PENDING_CONFIRMATIONS.pop(nonce, None)
    return action, payload


def safe_text(value: str, limit: int = MAX_OUTPUT) -> str:
    # Redact Telegram Bot Tokens: handle raw tokens, URLs (bot123...),
    # and common authorization schemes.
    value = re.sub(
        r"(?i:bot)?\d{7,12}:[A-Za-z0-9_-]{35,}(?![A-Za-z0-9_-])",
        "[REDACTED_BOT_TOKEN]",
        value,
    )
    value = re.sub(
        r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;}]+",
        r"\1[REDACTED]",
        value,
    )
    value = re.sub(
        r"(?i)(cookie\s*:\s*)[^\r\n]+",
        r"\1[REDACTED]",
        value,
    )
    value = re.sub(
        r"(?i)([\"']?(?:access_token|refresh_token|session_token|id_token|api[_-]?key|client[_-]?secret|password|passwd)[\"']?\s*[:=]\s*)([\"'])(?:\\.|(?!\2).)*\2",
        r"\1\2[REDACTED]\2",
        value,
    )
    value = re.sub(
        r"(?i)([\"']?(?:access_token|refresh_token|session_token|id_token|api[_-]?key|client[_-]?secret|password|passwd)[\"']?\s*[:=]\s*)[^\s,;}]+",
        r"\1[REDACTED]",
        value,
    )
    value = re.sub(r"eyJ[A-Za-z0-9_.-]{80,}", "[REDACTED_JWT]", value)
    value = re.sub(
        r"\b(?:sk-[A-Za-z0-9_-]{20,}|(?:xai|gsk|hf)_[A-Za-z0-9_-]{20,})\b",
        "[REDACTED_API_KEY]",
        value,
    )
    if len(value) <= limit:
        return value
    marker = "\n\n…（输出过长，中间内容已省略）…\n\n"
    if limit <= len(marker):
        return marker[:limit]
    remaining = limit - len(marker)
    head_size = (remaining + 1) // 2
    tail_size = remaining - head_size
    tail = value[-tail_size:] if tail_size else ""
    return value[:head_size] + marker + tail


def split_message(value: str, *, limit: int = 3800) -> list[str]:
    """Split Telegram text without dropping or reordering content."""
    if limit < 1:
        raise ValueError("limit must be positive")
    if not value:
        return [""]

    chunks: list[str] = []
    start = 0
    while start < len(value):
        end = min(start + limit, len(value))
        if end < len(value):
            newline = value.rfind("\n", start, end)
            if newline >= start:
                end = newline + 1
        chunks.append(value[start:end])
        start = end
    return chunks


_AGY_PROCESS_PREFIXES = (
    "i will ", "i'll ", "let me ", "i am going to ", "i'm going to ",
    "next, i will ", "first, i will ", "now i will ",
)


def clean_agy_output(value: str) -> str:
    """Remove repetitive action narration while retaining a user-facing answer."""
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not value:
        return value
    kept: list[str] = []
    process_lines = 0
    for line in value.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith(_AGY_PROCESS_PREFIXES):
            lower = stripped.lower()
            signals = (
                "final answer", "result", "conclusion", "recommend", "risk",
                "failure", "failed", " down", "answer", "我已", "结论",
                "最终答案", "结果", "建议", "风险", "失败", "down",
                "### ", "## ", "# ",
            )
            signal_positions = [
                index for signal in signals
                if (index := lower.find(signal.lower())) >= 0
            ]
            if signal_positions:
                signal_at = min(signal_positions)
                boundaries = [
                    match.end() for match in re.finditer(r"(?:[.!?。！？]\s*|[:;：；]\s*)", stripped[:signal_at])
                ]
                start = boundaries[-1] if boundaries else 0
                candidate = stripped[start:].strip()
                if candidate:
                    kept.append(candidate)
                    continue
            process_lines += 1
            continue
        kept.append(line.rstrip())
    cleaned = "\n".join(kept).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    if cleaned:
        return cleaned
    if process_lines:
        return "反重力已完成检查，但没有生成可展示的结论。"
    return value


def utf16_plain_chunks(value: str, limit: int = 3500) -> list[str]:
    """Split plain text without exceeding Telegram's UTF-16 unit limit."""
    if limit < 1:
        raise ValueError("limit must be positive")
    if not value:
        return [""]
    chunks: list[str] = []
    start = 0
    while start < len(value):
        units = 0
        end = start
        last_newline = -1
        while end < len(value):
            char_units = 2 if ord(value[end]) > 0xFFFF else 1
            if units + char_units > limit:
                break
            units += char_units
            end += 1
            if value[end - 1] == "\n":
                last_newline = end
        if end == len(value):
            chunks.append(value[start:end])
            break
        if last_newline > start:
            end = last_newline
        chunks.append(value[start:end])
        start = end
    return chunks


def telegram_html(value: str) -> str:
    """Render a deterministic, non-overlapping Telegram Markdown subset."""
    import html

    def inline(text: str, *, bold: bool = True) -> str:
        rendered: list[str] = []
        plain: list[str] = []

        def flush() -> None:
            if plain:
                rendered.append(html.escape("".join(plain), quote=False))
                plain.clear()

        index = 0
        while index < len(text):
            if text[index] == "`":
                end = text.find("`", index + 1)
                if end >= 0:
                    flush()
                    rendered.append(
                        "<code>" + html.escape(text[index + 1:end], quote=False) + "</code>"
                    )
                    index = end + 1
                    continue
            if bold and text.startswith("**", index):
                end = text.find("**", index + 2)
                if end >= 0:
                    flush()
                    rendered.append("<b>" + inline(text[index + 2:end], bold=False) + "</b>")
                    index = end + 2
                    continue
            plain.append(text[index])
            index += 1
        flush()
        return "".join(rendered)

    lines: list[str] = []
    for line in value.splitlines(keepends=True):
        ending = "\n" if line.endswith("\n") else ""
        body = line[:-1] if ending else line
        heading = re.match(r"^(#{1,3})\s+(.+)$", body)
        bullet = re.match(r"^[-*]\s+", body)
        if heading:
            lines.append("<b>" + inline(heading.group(2)) + "</b>" + ending)
        elif bullet:
            lines.append("• " + inline(body[bullet.end():]) + ending)
        else:
            lines.append(inline(body) + ending)
    return "".join(lines)


def _utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def telegram_html_chunks(value: str, *, limit: int = 3500) -> list[str]:
    """Render first, then split into independently valid bounded HTML payloads."""
    if limit < 32:
        raise ValueError("limit is too small for safe HTML chunking")
    rendered = telegram_html(value)
    tokens = re.findall(r"</?(?:b|code)>|&(?:amp|lt|gt);|.", rendered, re.S)
    chunks: list[str] = []
    current: list[str] = []
    stack: list[str] = []

    def closers(tags: list[str]) -> str:
        return "".join(f"</{tag}>" for tag in reversed(tags))

    for token in tokens:
        next_stack = list(stack)
        if token in {"<b>", "<code>"}:
            next_stack.append(token[1:-1])
        elif token in {"</b>", "</code>"}:
            if not next_stack or next_stack[-1] != token[2:-1]:
                raise ValueError("invalid generated HTML")
            next_stack.pop()
        candidate = "".join(current) + token + closers(next_stack)
        if current and _utf16_units(candidate) > limit:
            chunks.append("".join(current) + closers(stack))
            current = [f"<{tag}>" for tag in stack]
            candidate = "".join(current) + token + closers(next_stack)
        if _utf16_units(candidate) > limit:
            raise ValueError("single HTML token exceeds chunk limit")
        current.append(token)
        stack = next_stack
    if current or not chunks:
        chunks.append("".join(current) + closers(stack))
    return chunks


def _artifact_metadata(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
        info.st_size, info.st_mtime_ns, info.st_ctime_ns,
    )


class ArtifactScanError(RuntimeError):
    """The artifact tree could not be completely and safely enumerated."""


def artifact_snapshot(root: Path = CHAT_DIR) -> dict[Path, tuple[int, ...]]:
    """Snapshot supported files reachable through trusted owned directories."""
    try:
        root_info = root.lstat()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ArtifactScanError("artifact root could not be inspected") from exc
    if (
        not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode)
        or root_info.st_uid != os.getuid() or stat.S_IMODE(root_info.st_mode) & 0o022
    ):
        return {}
    snapshot: dict[Path, tuple[int, ...]] = {}
    entries = 0
    directories = 0
    def walk_error(exc: OSError) -> None:
        if isinstance(exc, FileNotFoundError):
            return
        raise ArtifactScanError("artifact tree could not be enumerated") from exc

    for directory, dirnames, filenames in os.walk(
        root, followlinks=False, onerror=walk_error,
    ):
        directories += 1
        if directories > MAX_ARTIFACT_SCAN_DIRECTORIES:
            raise ArtifactScanError("artifact directory budget exceeded")
        directory_path = Path(directory)
        safe_dirs: list[str] = []
        for name in dirnames:
            try:
                info = (directory_path / name).lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ArtifactScanError(
                    "artifact directory could not be inspected"
                ) from exc
            if (
                stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)
                and info.st_uid == os.getuid() and info.st_dev == root_info.st_dev
                and not stat.S_IMODE(info.st_mode) & 0o022
            ):
                safe_dirs.append(name)
        dirnames[:] = safe_dirs
        for name in filenames:
            entries += 1
            if entries > MAX_ARTIFACT_SCAN_ENTRIES:
                raise ArtifactScanError("artifact entry budget exceeded")
            path = directory_path / name
            if path.suffix.lower() not in ARTIFACT_SUFFIXES:
                continue
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ArtifactScanError("artifact file could not be inspected") from exc
            if (
                not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != os.getuid() or info.st_dev != root_info.st_dev
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o022
                or not 0 < info.st_size <= MAX_ARTIFACT_BYTES
            ):
                continue
            snapshot[path.relative_to(root)] = _artifact_metadata(info)
    return snapshot


def new_artifacts(
    before: dict[Path, tuple[int, ...]], after: dict[Path, tuple[int, ...]]
) -> list[Path]:
    changed = [path for path, metadata in after.items() if before.get(path) != metadata]
    changed.sort(key=lambda path: (after[path][-1], str(path)), reverse=True)
    return changed[:MAX_ARTIFACTS_PER_REPLY]


def _artifact_warning_name(path: Path) -> str:
    name = re.sub(
        r"[\x00-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]",
        "", path.name,
    ).strip()
    return safe_text(name or "未命名文件", 100).replace("\n", " ")


@dataclass
class PreparedArtifact:
    handle: object
    filename: str

    def close(self) -> None:
        if not getattr(self.handle, "closed", False):
            self.handle.close()


def _valid_artifact_component(component: str) -> bool:
    return bool(component and component not in {".", ".."} and "/" not in component)


def _trusted_artifact_directory(info: os.stat_result, root_info: os.stat_result) -> bool:
    return bool(
        stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
        and info.st_dev == root_info.st_dev
        and not stat.S_IMODE(info.st_mode) & 0o022
    )


def prepare_artifacts(
    paths: list[Path], expected: dict[Path, tuple[int, ...]] | None = None,
) -> tuple[list[PreparedArtifact], list[str]]:
    """Freeze verified workspace artifacts into private Bot-owned files."""
    prepared: list[PreparedArtifact] = []
    warnings: list[str] = []
    staging = ARTIFACT_STAGING_DIR
    if staging.is_symlink():
        return [], ["产物暂存区安全校验未通过"]
    staging.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging_info = staging.lstat()
    if not stat.S_ISDIR(staging_info.st_mode) or staging_info.st_uid != os.getuid():
        return [], ["产物暂存区安全校验未通过"]
    os.chmod(staging, 0o700)
    root_fd = -1
    try:
        root_fd = os.open(
            CHAT_DIR,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        )
        root_info = os.fstat(root_fd)
        if not _trusted_artifact_directory(root_info, root_info):
            raise OSError("unsafe artifact root")
        for ordinal, relative in enumerate(paths[:MAX_ARTIFACTS_PER_REPLY], 1):
            warning_name = _artifact_warning_name(relative)
            dir_fds: list[int] = []
            directory_metadata: list[tuple[int, int, int, int]] = []
            source_fd = -1
            staged = None
            try:
                parts = relative.parts
                if (
                    relative.is_absolute() or not parts
                    or any(not _valid_artifact_component(part) for part in parts)
                    or relative.suffix.lower() not in ARTIFACT_SUFFIXES
                ):
                    raise OSError("unsafe artifact path")
                parent_fd = root_fd
                for component in parts[:-1]:
                    child_fd = os.open(
                        component,
                        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=parent_fd,
                    )
                    dir_fds.append(child_fd)
                    child_info = os.fstat(child_fd)
                    if not _trusted_artifact_directory(child_info, root_info):
                        raise OSError("unsafe artifact directory")
                    directory_metadata.append((
                        child_info.st_dev, child_info.st_ino,
                        child_info.st_mode, child_info.st_uid,
                    ))
                    parent_fd = child_fd
                source_fd = os.open(
                    parts[-1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent_fd,
                )
                before = os.fstat(source_fd)
                metadata = _artifact_metadata(before)
                if (
                    (expected is not None and expected.get(relative) != metadata)
                    or not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or before.st_dev != root_info.st_dev or before.st_nlink != 1
                    or stat.S_IMODE(before.st_mode) & 0o022
                    or not 0 < before.st_size <= MAX_ARTIFACT_BYTES
                ):
                    raise OSError("artifact changed or unsafe")
                staged = tempfile.TemporaryFile(mode="w+b", dir=staging)
                os.fchmod(staged.fileno(), 0o600)
                remaining = before.st_size
                while remaining:
                    chunk = os.read(source_fd, min(1024 * 1024, remaining))
                    if not chunk:
                        raise OSError("artifact short read")
                    staged.write(chunk)
                    remaining -= len(chunk)
                if os.read(source_fd, 1):
                    raise OSError("artifact grew during copy")
                after = os.fstat(source_fd)
                if _artifact_metadata(after) != metadata:
                    raise OSError("artifact changed during copy")
                verify_parent = root_fd
                verification_fds: list[int] = []
                try:
                    for component, trusted in zip(parts[:-1], directory_metadata):
                        verify_fd = os.open(
                            component,
                            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                            dir_fd=verify_parent,
                        )
                        verification_fds.append(verify_fd)
                        verify_info = os.fstat(verify_fd)
                        current = (
                            verify_info.st_dev, verify_info.st_ino,
                            verify_info.st_mode, verify_info.st_uid,
                        )
                        if current != trusted:
                            raise OSError("artifact parent changed during copy")
                        verify_parent = verify_fd
                finally:
                    for descriptor in reversed(verification_fds):
                        os.close(descriptor)
                staged.flush()
                staged.seek(0)
                prepared.append(PreparedArtifact(staged, warning_name))
                staged = None
            except Exception:
                error_id = secrets.token_hex(4)
                log.warning("artifact preparation failed ordinal=%s error_id=%s", ordinal, error_id)
                warnings.append(f"{warning_name}：安全校验未通过（错误编号 {error_id}）")
            finally:
                if staged is not None:
                    staged.close()
                if source_fd >= 0:
                    os.close(source_fd)
                for descriptor in reversed(dir_fds):
                    os.close(descriptor)
    except Exception:
        for item in prepared:
            item.close()
        return [], ["产物工作区安全校验未通过"]
    finally:
        if root_fd >= 0:
            os.close(root_fd)
    return prepared, warnings


async def freeze_artifacts(
    paths: list[Path], expected: dict[Path, tuple[int, ...]],
) -> tuple[list[PreparedArtifact], list[str]]:
    """Freeze artifacts without releasing owner locks during cancellation."""
    worker = asyncio.create_task(
        asyncio.to_thread(prepare_artifacts, paths, expected)
    )
    cancelled: asyncio.CancelledError | None = None
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError as exc:
            cancelled = exc
        except Exception:
            break
    try:
        prepared, warnings = worker.result()
    except Exception:
        if cancelled is not None:
            raise cancelled
        raise
    if cancelled is not None:
        for artifact in prepared:
            artifact.close()
        raise cancelled
    return prepared, warnings


async def upload_prepared_artifacts(bot, *, chat_id: int, artifacts: list[PreparedArtifact]) -> tuple[int, list[str]]:
    sent = 0
    warnings: list[str] = []
    try:
        for ordinal, artifact in enumerate(artifacts, 1):
            try:
                artifact.handle.seek(0)
                await bot.send_document(
                    chat_id=chat_id, document=artifact.handle, filename=artifact.filename,
                    caption=f"📎 {artifact.filename}"[:120],
                )
                sent += 1
            except Exception:
                error_id = secrets.token_hex(4)
                log.warning("artifact upload failed ordinal=%s error_id=%s", ordinal, error_id)
                warnings.append(f"{artifact.filename}：上传未通过（错误编号 {error_id}）")
    finally:
        for artifact in artifacts:
            artifact.close()
    return sent, warnings


async def send_artifacts(
    bot, *, chat_id: int, paths: list[Path],
    expected: dict[Path, tuple[int, ...]] | None = None,
) -> tuple[int, list[str]]:
    """Compatibility wrapper that freezes artifacts before any await."""
    prepared, warnings = prepare_artifacts(paths, expected)
    sent, upload_warnings = await upload_prepared_artifacts(
        bot, chat_id=chat_id, artifacts=prepared,
    )
    return sent, warnings + upload_warnings


async def deliver_result(notice, value: str, *, clean_agy: bool = True) -> None:
    """Replace a progress notice with concise mobile-friendly HTML chunks."""
    content = clean_agy_output(value) if clean_agy else value
    rendered = telegram_html_chunks(
        safe_text(content or "反重力已完成，但没有返回文本.")
    )
    await notice.edit_text(
        rendered[0], parse_mode=ParseMode.HTML, link_preview_options=NO_LINK_PREVIEW
    )
    bot = notice.get_bot()
    for chunk in rendered[1:]:
        await bot.send_message(
            chat_id=notice.chat_id,
            text=chunk,
            parse_mode=ParseMode.HTML,
            link_preview_options=NO_LINK_PREVIEW,
        )


async def deliver_plain_fallback(notice, value: str) -> None:
    """Deliver complete parse-free text while keeping every payload bounded."""
    chunks = utf16_plain_chunks(safe_text(value, sys.maxsize))
    try:
        await notice.edit_text(
            chunks[0], link_preview_options=NO_LINK_PREVIEW,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        error_id = secrets.token_hex(4)
        log.warning("plain result fallback edit failed error_id=%s", error_id)
        try:
            await notice.edit_text(
                "❌ 结果文本发送失败，但仍会继续返回可用产物。",
                link_preview_options=NO_LINK_PREVIEW,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            error_id = secrets.token_hex(4)
            log.warning("generic result fallback failed error_id=%s", error_id)
        return
    bot = notice.get_bot()
    for ordinal, chunk in enumerate(chunks[1:], 2):
        try:
            await bot.send_message(
                chat_id=notice.chat_id, text=chunk,
                link_preview_options=NO_LINK_PREVIEW,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            error_id = secrets.token_hex(4)
            log.warning(
                "plain result fallback chunk failed ordinal=%s error_id=%s",
                ordinal, error_id,
            )
            return


async def keep_typing(chat, *, interval: float = 4.0) -> None:
    """Keep Telegram's native typing indicator alive for a long task."""
    while True:
        try:
            await chat.send_action(ChatAction.TYPING)
        except asyncio.CancelledError as cancelled:
            raise
        except Exception:
            log.debug("typing indicator failed", exc_info=True)
        await asyncio.sleep(interval)


async def run_agent_job(
    update: Update,
    notice,
    args: list[str],
    *,
    timeout: int,
    cwd: Path,
    label: str,
    mutation: bool = False,
    artifact_workspace: bool = False,
) -> None:
    """Run one cancellable AGY task with shared busy and progress handling."""
    if CHAT_LOCK.locked():
        await notice.edit_text(
            "⏳ 另一个反重力任务正在运行，本任务未启动。\n"
            "请等待完成，或发送 /stop 后重试。"
        )
        return
    if mutation and MUTATION_LOCK.locked():
        await notice.edit_text(
            "⏳ 账号池导入或另一项维修正在写入。本次维修未启动，请稍后重试。"
        )
        return

    if mutation:
        await MUTATION_LOCK.acquire()
    await CHAT_LOCK.acquire()
    artifact_scan_enabled = artifact_workspace
    try:
        artifacts_before = artifact_snapshot() if artifact_scan_enabled else {}
    except ArtifactScanError:
        artifacts_before = {}
        artifact_scan_enabled = False
    prepared_artifacts: list[PreparedArtifact] = []
    artifact_warnings: list[str] = (
        [] if artifact_scan_enabled
        else ["产物扫描未完整完成，本次已禁用自动返回"]
    )
    code = 1
    output = ""
    typing_task = asyncio.create_task(keep_typing(update.effective_chat))
    try:
        try:
            code, output = await run(
                args,
                timeout=timeout,
                cwd=cwd,
                owner_id=update.effective_user.id,
            )
        except Exception:
            error_id = secrets.token_hex(4)
            log.exception("AGY job spawn failed error_id=%s", error_id)
            await notice.edit_text(
                f"❌ {label}未能启动（错误编号 {error_id}）。请先发送 /status 检查环境。"
            )
            return
        if agy_result_succeeded(code, output) and artifact_scan_enabled:
            try:
                artifacts_after = artifact_snapshot()
                artifacts = new_artifacts(artifacts_before, artifacts_after)
                if artifacts:
                    prepared_artifacts, artifact_warnings = await freeze_artifacts(
                        artifacts, artifacts_after,
                    )
            except Exception:
                artifact_warnings = ["产物扫描未完整完成，本次已禁用自动返回"]
    finally:
        typing_task.cancel()
        await asyncio.gather(typing_task, return_exceptions=True)
        CHAT_LOCK.release()
        if mutation:
            MUTATION_LOCK.release()

    try:
        if agy_result_succeeded(code, output):
            result_text = f"✅ {label}完成\n\n{output}"
            try:
                await deliver_result(notice, result_text)
            except asyncio.CancelledError:
                raise
            except Exception:
                error_id = secrets.token_hex(4)
                log.warning("formatted result delivery failed error_id=%s", error_id)
                await deliver_plain_fallback(notice, result_text)
            sent = 0
            upload_warnings: list[str] = []
            if prepared_artifacts:
                sent, upload_warnings = await upload_prepared_artifacts(
                    update.get_bot(), chat_id=update.effective_chat.id,
                    artifacts=prepared_artifacts,
                )
            warnings = artifact_warnings + upload_warnings
            if warnings:
                await update.get_bot().send_message(
                    chat_id=update.effective_chat.id,
                    text="⚠️ 部分产物未发送：\n" + "\n".join(
                        f"• {item}" for item in warnings[:MAX_ARTIFACTS_PER_REPLY]
                    ),
                )
            elif sent:
                log.info("sent %s AGY artifact(s) to Telegram", sent)
        elif code == 130:
            await notice.edit_text(f"⏹ {label}已停止。")
        elif code == 124:
            await deliver_result(notice, f"⏱ {label}超时并已自动停止。\n{output}")
        else:
            suggestion = "请发送 /agy_login 检查登录，或 /status 查看服务状态。"
            await deliver_result(
                notice,
                f"❌ {label}失败（exit={code}）\n{output or '(无输出)'}\n\n{suggestion}",
            )
    finally:
        for artifact in prepared_artifacts:
            artifact.close()


def confirmation_markup(nonce: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ 确认执行", callback_data=f"confirm:{nonce}"),
            InlineKeyboardButton("取消", callback_data=f"cancel:{nonce}"),
        ]]
    )


def bot_commands() -> list[BotCommand]:
    return [
        BotCommand("start", "打开反重力助手和快速使用说明"),
        BotCommand("ask", "和反重力聊天，自动延续当前对话"),
        BotCommand("new", "停止当前任务并开启全新对话"),
        BotCommand("stop", "立即停止正在运行的反重力任务"),
        BotCommand("status", "一键检查 Hermes、CPA 和反重力"),
        BotCommand("project_status", "检查账号池、模型和 CPA 服务"),
        BotCommand("project", "让反重力只读分析托管项目"),
        BotCommand("project_repair", "高风险维修（服务器默认关闭）"),
        BotCommand("agy_login", "查看官方 AGY 登录说明"),
        BotCommand("restart", "重启 Hermes Gateway"),
        BotCommand("help", "查看完整命令说明和示例"),
    ]


def agy_telegram_prompt(prompt: str) -> str:
    """Add stable generic Telegram output and artifact creation rules."""
    return f"""{prompt}

Output requirements for this Telegram conversation:
- Answer directly in concise Simplified Chinese unless the user requests another language.
- Lead with the conclusion. Use short headings and bullets suitable for a phone screen.
- Do not narrate tool usage or plans; hide internal reasoning, repeated probes, and waiting.
- Report important evidence, findings, risks, and actionable next steps inline.
- Do not expose file:// links or tell the user to use scp or an IDE download feature.
- Create requested deliverable files only in the current workspace and mention only the filename; the Telegram Bot will detect and upload it automatically.
- Create the real requested format. For Word documents, create a structurally valid .docx file, never plain text renamed to .docx.
- Use lightweight Markdown only: short headings, bullets, bold text, and inline code.
"""


def build_chat_args(prompt: str, *, continue_session: bool) -> list[str]:
    args = [AGY, "--sandbox"]
    if continue_session:
        args.append("--continue")
    args.extend(["--print-timeout", "10m", "-p", agy_telegram_prompt(prompt)])
    return args


def _known_agy_failure_line(line: str) -> bool:
    lowered = line.strip().lower()
    return lowered.startswith(
        (
            "error:",
            "fatal:",
            "authentication failed",
            "authorization failed",
            "permission denied",
            "unauthorized",
            "resource_exhausted",
            "resource exhausted",
            "quota exhausted",
            "quota exceeded",
            "jetski: no output produced",
            "warning: conversation ",
        )
    ) and not (
        lowered.startswith("warning: conversation ") and " not found" not in lowered
    )


def agy_result_succeeded(code: int, output: str) -> bool:
    if code != 0 or not output.strip():
        return False
    return not any(_known_agy_failure_line(line) for line in output.splitlines())


def should_retry_fresh(code: int, output: str) -> bool:
    if code in (124, 130):
        return False
    lowered = output.lower()
    if any(
        phrase in lowered
        for phrase in (
            "authentication",
            "invalid token",
            "unauthorized",
            "quota",
            "resource_exhausted",
            "rate limit",
        )
    ):
        return False
    if code == 0:
        return False
    for line in output.splitlines():
        diagnostic = line.strip().lower()
        if diagnostic.startswith("error:"):
            diagnostic = diagnostic.removeprefix("error:").strip()
        if diagnostic.startswith(
            (
                "conversation not found",
                "no conversation",
                "no active conversation",
                "session not found",
                "failed to resume conversation",
                "cannot continue conversation",
                "unable to continue conversation",
            )
        ):
            return True
        if diagnostic.startswith("warning: conversation ") and " not found" in diagnostic:
            return True
    return False


def _secure_chat_session_dir(root: Path, *, create: bool) -> bool:
    if root.is_symlink():
        raise RuntimeError("chat session directory must not be a symlink")
    if not root.exists():
        if not create:
            return False
        root.mkdir(parents=True, mode=0o700)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError("chat session directory is not a trusted owned directory")
    os.chmod(root, 0o700)
    return True


def chat_session_marker(user_id: int, *, root: Path = CHAT_SESSION_DIR) -> Path:
    return root / f"{user_id}.active"


def chat_session_active(user_id: int, *, root: Path = CHAT_SESSION_DIR) -> bool:
    if not _secure_chat_session_dir(root, create=False):
        return False
    marker = chat_session_marker(user_id, root=root)
    try:
        info = marker.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1


def mark_chat_session(user_id: int, *, root: Path = CHAT_SESSION_DIR) -> None:
    _secure_chat_session_dir(root, create=True)
    marker = chat_session_marker(user_id, root=root)
    fd, tmp_name = tempfile.mkstemp(prefix=".session-", suffix=".tmp", dir=root)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            fd = -1
            handle.write(b"active\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, marker)
        info = marker.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError("chat session marker is not a private regular file")
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def reset_chat_session(user_id: int, *, root: Path = CHAT_SESSION_DIR) -> None:
    if not _secure_chat_session_dir(root, create=False):
        return
    try:
        chat_session_marker(user_id, root=root).unlink()
    except FileNotFoundError:
        pass


async def configure_bot(application: Application) -> None:
    if ALLOWED_USER_ID <= 0:
        raise RuntimeError("RESCUE_ALLOWED_USER_ID is not configured")
    if not AGY:
        raise RuntimeError("official AGY CLI is unavailable; run it directly to complete login")
    for executable in (Path(AGY), Path(BWRAP)):
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise RuntimeError("a required sandbox executable is unavailable")
    try:
        _validate_regular_mount_source(AGY_TOKEN, label="AGY token", secret=True)
    except RuntimeError as exc:
        raise RuntimeError(
            "official AGY login token is unavailable; run agy in a trusted terminal"
        ) from exc
    prepare_agy_sandbox_storage()
    removed_repair_runs = cleanup_stale_repair_storage()
    if removed_repair_runs:
        log.warning("removed %s stale repair run(s)", removed_repair_runs)
    CHAT_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    _secure_chat_session_dir(CHAT_SESSION_DIR, create=True)
    os.chmod(CHAT_DIR, 0o700)
    await application.bot.set_my_commands(bot_commands())
    await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    await application.bot.set_my_short_description(
        f"独立反重力助手：聊天、{PROJECT_NAME} 管理与 Hermes 救援"
    )
    await application.bot.set_my_description(
        "直接发送文字即可和反重力连续对话；输入 / 可查看中文命令菜单。"
        f"支持 {PROJECT_NAME}/CPA 管理、凭证导入和 Hermes 故障救援。"
    )


async def deny(update: Update) -> None:
    if update.effective_message:
        await update.effective_message.reply_text("Unauthorized")


def active_job(user_id: int) -> bool:
    return user_id in CHAT_PRESPAWN or user_id in JOB_STARTING or user_id in ACTIVE_PROCS


def _process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
    pgid = proc.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    deadline = asyncio.get_running_loop().time() + 2.0
    while _process_group_exists(pgid) and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)

    if _process_group_exists(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        kill_deadline = asyncio.get_running_loop().time() + 2.0
        while _process_group_exists(pgid) and asyncio.get_running_loop().time() < kill_deadline:
            await asyncio.sleep(0.05)
        if _process_group_exists(pgid):
            log.error("subprocess group pgid=%s still exists after SIGKILL", pgid)

    if proc.returncode is None:
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            log.error("subprocess leader pid=%s did not exit after group cleanup", proc.pid)


async def stop_active_job(user_id: int) -> bool:
    if user_id in CHAT_PRESPAWN or user_id in JOB_STARTING:
        JOB_CANCEL_REQUESTED.add(user_id)
        return True
    proc = ACTIVE_PROCS.get(user_id)
    if not proc:
        return False
    JOB_CANCEL_REQUESTED.add(user_id)
    await _terminate_process(proc)
    return True


async def _read_bounded_output(
    proc: asyncio.subprocess.Process,
    *,
    limit: int = MAX_SUBPROCESS_CAPTURE,
) -> bytes:
    """Drain stdout without unbounded buffering, retaining the head and tail."""
    if proc.stdout is None:
        await proc.wait()
        return b""
    head_limit = limit // 2
    tail_limit = limit - head_limit
    head = bytearray()
    tail = bytearray()
    total = 0
    diagnostic_seen = False
    scan_tail = b""
    diagnostic_patterns = (
        b"error:",
        b"fatal:",
        b"authentication failed",
        b"authorization failed",
        b"permission denied",
        b"unauthorized",
        b"resource_exhausted",
        b"resource exhausted",
        b"quota exhausted",
        b"quota exceeded",
        b"jetski: no output produced",
    )
    while True:
        chunk = await proc.stdout.read(64 * 1024)
        if not chunk:
            break
        scan_data = (scan_tail + chunk).lower()
        if any(pattern in scan_data for pattern in diagnostic_patterns):
            diagnostic_seen = True
        if b"warning: conversation " in scan_data and b" not found" in scan_data:
            diagnostic_seen = True
        scan_tail = scan_data[-1024:]
        total += len(chunk)
        head_space = max(0, head_limit - len(head))
        if head_space:
            head.extend(chunk[:head_space])
            chunk = chunk[head_space:]
        if chunk:
            tail.extend(chunk)
            if len(tail) > tail_limit:
                del tail[: len(tail) - tail_limit]
    await proc.wait()
    if total <= limit:
        captured = bytes(head + tail)
    else:
        captured = bytes(head) + b"\n\n...[bounded output omitted]...\n\n" + bytes(tail)
    if diagnostic_seen:
        captured += b"\nError: AGY failure diagnostic detected in subprocess output\n"
    return captured


async def _reap_spawned_process(
    spawn_task: asyncio.Task[asyncio.subprocess.Process],
) -> None:
    """Terminate a process whose spawn outlived cancellation of its caller."""
    try:
        proc = await asyncio.shield(spawn_task)
    except BaseException:
        return
    await _terminate_process(proc)


def _finish_spawn_reaper(task: asyncio.Task[None]) -> None:
    SPAWN_REAPERS.discard(task)
    try:
        error = task.exception()
    except asyncio.CancelledError:
        return
    if error is not None:
        log.error(
            "spawn reaper failed type=%s",
            type(error).__name__,
            exc_info=(type(error), error, error.__traceback__),
        )


def _track_spawn_reaper(task: asyncio.Task[None]) -> None:
    SPAWN_REAPERS.add(task)
    task.add_done_callback(_finish_spawn_reaper)


def _strip_terminal_controls(value: str) -> str:
    """Remove ANSI terminal control sequences without removing visible text."""
    output: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        code = ord(character)
        if character == "\x1b":
            index += 1
            if index >= len(value):
                break
            introducer = value[index]
            if introducer == "[":
                index += 1
                while index < len(value) and not 0x40 <= ord(value[index]) <= 0x7E:
                    index += 1
                index += index < len(value)
                continue
            if introducer == "]":
                index += 1
                while index < len(value):
                    if value[index] == "\x07":
                        index += 1
                        break
                    if value[index] == "\x1b" and index + 1 < len(value) and value[index + 1] == "\\":
                        index += 2
                        break
                    index += 1
                continue
            index += 1
            continue
        if code == 0x9B:
            index += 1
            while index < len(value) and not 0x40 <= ord(value[index]) <= 0x7E:
                index += 1
            index += index < len(value)
            continue
        if code == 0x9D:
            index += 1
            while index < len(value) and ord(value[index]) not in (0x07, 0x9C):
                index += 1
            index += index < len(value)
            continue
        if code < 0x20 and character not in "\n\r\t":
            index += 1
            continue
        if 0x7F <= code <= 0x9F:
            index += 1
            continue
        output.append(character)
        index += 1
    return "".join(output)


async def run(
    args: list[str],
    timeout: int = 120,
    cwd: Path | None = None,
    *,
    owner_id: int | None = None,
) -> tuple[int, str]:
    if owner_id is not None:
        JOB_CANCEL_REQUESTED.discard(owner_id)
        JOB_STARTING.add(owner_id)
    spawn_task = asyncio.create_task(
        asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd) if cwd else None,
            env=sanitized_child_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
            limit=64 * 1024,
        )
    )
    SPAWN_TASKS.add(spawn_task)
    spawn_task.add_done_callback(SPAWN_TASKS.discard)
    try:
        try:
            proc = await asyncio.shield(spawn_task)
        except asyncio.CancelledError:
            reaper = asyncio.create_task(_reap_spawned_process(spawn_task))
            _track_spawn_reaper(reaper)
            try:
                await asyncio.wait_for(asyncio.shield(reaper), timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                # The tracked reaper keeps running even if shutdown cancellation
                # arrives again or the platform spawn takes unusually long.
                pass
            if owner_id is not None:
                JOB_CANCEL_REQUESTED.discard(owner_id)
            raise
        except OSError as exc:
            if owner_id is not None and owner_id in JOB_CANCEL_REQUESTED:
                JOB_CANCEL_REQUESTED.discard(owner_id)
                return 130, "任务已停止。"
            return 127, f"无法启动 {Path(args[0]).name}：{type(exc).__name__}"
    finally:
        if owner_id is not None:
            JOB_STARTING.discard(owner_id)

    if owner_id is not None and owner_id in JOB_CANCEL_REQUESTED:
        cleanup = asyncio.create_task(_terminate_process(proc))
        await _await_task_uninterruptibly(cleanup)
        JOB_CANCEL_REQUESTED.discard(owner_id)
        return 130, "任务已停止。"
    if owner_id is not None:
        ACTIVE_PROCS[owner_id] = proc
    cancelled_by_user = False
    try:
        try:
            output = await asyncio.wait_for(_read_bounded_output(proc), timeout=timeout)
        except asyncio.TimeoutError:
            cleanup = asyncio.create_task(_terminate_process(proc))
            await _await_task_uninterruptibly(cleanup)
            return 124, "任务超时，已自动停止。"
        except asyncio.CancelledError:
            cleanup = asyncio.create_task(_terminate_process(proc))
            await _await_task_uninterruptibly(cleanup)
            raise
    finally:
        if owner_id is not None and ACTIVE_PROCS.get(owner_id) is proc:
            ACTIVE_PROCS.pop(owner_id, None)
        if owner_id is not None:
            cancelled_by_user = owner_id in JOB_CANCEL_REQUESTED
            JOB_CANCEL_REQUESTED.discard(owner_id)
    if cancelled_by_user or (proc.returncode is not None and proc.returncode < 0):
        return 130, "任务已停止。"
    # A successful leader may have detached stdio while leaving descendants in
    # its process group. Reap that group before relinquishing ownership.
    if _process_group_exists(proc.pid):
        cleanup = asyncio.create_task(_terminate_process(proc))
        await _await_task_uninterruptibly(cleanup)
    return proc.returncode or 0, safe_text(output.decode(errors="replace"))


async def run_chat_prompt(
    update: Update,
    prompt: str,
    *,
    force_new: bool = False,
    reset_session: bool = False,
    wait_for_lock: bool = False,
) -> None:
    if not allowed(update):
        return await deny(update)
    prompt = prompt.strip()
    if not prompt:
        return await update.effective_message.reply_text("请输入你想让反重力处理的内容。")
    if CHAT_LOCK.locked() and not wait_for_lock:
        return await update.effective_message.reply_text(
            "⏳ 反重力正在处理上一条消息。\n"
            "如需打断，请发送 /stop；发送 /new 会停止当前任务并开启新对话。"
        )

    await CHAT_LOCK.acquire()
    user_id = update.effective_user.id
    CHAT_PRESPAWN.add(user_id)
    try:
        if reset_session:
            reset_chat_session(user_id)
        continue_session = chat_session_active(user_id) and not force_new
        CHAT_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(CHAT_DIR, 0o700)
        notice = await update.effective_message.reply_text(
            "🧠 正在继续当前对话……" if continue_session else "🆕 正在开启新对话……"
        )
        if user_id in JOB_CANCEL_REQUESTED:
            JOB_CANCEL_REQUESTED.discard(user_id)
            await notice.edit_text("⏹ 当前任务已停止。")
            return
        typing_task = asyncio.create_task(keep_typing(update.effective_chat))
        artifact_scan_enabled = True
        prepared_artifacts: list[PreparedArtifact] = []
        artifact_warnings: list[str] = []
        try:
            artifacts_before = artifact_snapshot()
        except ArtifactScanError:
            artifacts_before = {}
            artifact_scan_enabled = False
            artifact_warnings.append("产物扫描未完整完成，本次已禁用自动返回")
        try:
            code, out = await run(
                build_agy_sandbox_args(
                    build_chat_args(prompt, continue_session=continue_session),
                    mode="chat",
                ),
                timeout=630,
                cwd=CHAT_DIR,
                owner_id=user_id,
            )
            if continue_session and should_retry_fresh(code, out):
                reset_chat_session(user_id)
                code, out = await run(
                    build_agy_sandbox_args(
                        build_chat_args(prompt, continue_session=False),
                        mode="chat",
                    ),
                    timeout=630,
                    cwd=CHAT_DIR,
                    owner_id=user_id,
                )
        finally:
            typing_task.cancel()
            await asyncio.gather(typing_task, return_exceptions=True)
        if agy_result_succeeded(code, out):
            mark_chat_session(user_id)
            if artifact_scan_enabled:
                try:
                    artifacts_after = artifact_snapshot()
                    artifacts = new_artifacts(artifacts_before, artifacts_after)
                    if artifacts:
                        prepared_artifacts, prepare_warnings = await freeze_artifacts(
                            artifacts, artifacts_after,
                        )
                        artifact_warnings.extend(prepare_warnings)
                except Exception:
                    artifact_warnings.append(
                        "产物扫描未完整完成，本次已禁用自动返回"
                    )
        elif code == 130:
            await notice.edit_text("⏹ 当前任务已停止。")
        else:
            await deliver_result(
                notice,
                f"❌ 反重力调用失败（exit={code}）\n{out or '(无输出)'}",
            )
    finally:
        CHAT_PRESPAWN.discard(user_id)
        CHAT_LOCK.release()

    try:
        if agy_result_succeeded(code, out):
            try:
                await deliver_result(notice, out)
            except asyncio.CancelledError:
                raise
            except Exception:
                error_id = secrets.token_hex(4)
                log.warning("formatted chat delivery failed error_id=%s", error_id)
                await deliver_plain_fallback(notice, out)
            sent = 0
            upload_warnings: list[str] = []
            if prepared_artifacts:
                sent, upload_warnings = await upload_prepared_artifacts(
                    update.get_bot(), chat_id=update.effective_chat.id,
                    artifacts=prepared_artifacts,
                )
            warnings = artifact_warnings + upload_warnings
            if warnings:
                await update.get_bot().send_message(
                    chat_id=update.effective_chat.id,
                    text="⚠️ 部分产物未发送：\n" + "\n".join(
                        f"• {item}" for item in warnings[:MAX_ARTIFACTS_PER_REPLY]
                    ),
                )
    finally:
        for artifact in prepared_artifacts:
            artifact.close()


async def ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_chat_prompt(update, " ".join(context.args))


async def new_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return await deny(update)
    user_id = update.effective_user.id
    stopped = await stop_active_job(user_id)
    prompt = " ".join(context.args).strip()
    if prompt:
        await run_chat_prompt(
            update,
            prompt,
            force_new=True,
            reset_session=True,
            wait_for_lock=True,
        )
    else:
        async with CHAT_LOCK:
            reset_chat_session(user_id)
        prefix = "已停止上一项任务，并" if stopped else "已"
        await update.effective_message.reply_text(
            f"🆕 {prefix}开启新对话。现在直接发送文字即可。"
        )


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return await deny(update)
    user_id = update.effective_user.id
    stopped = await stop_active_job(user_id)
    if stopped:
        await update.effective_message.reply_text("⏹ 正在停止当前任务……")
    else:
        await update.effective_message.reply_text("当前没有正在运行的反重力任务。")


async def text_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_chat_prompt(update, update.message.text or "")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return await deny(update)
    await update.message.reply_text(
        "反重力助手已就绪。\n\n"
        "💬 直接发送文字：连续聊天，无需命令\n"
        "/new：停止当前任务并开启新对话\n"
        "/stop：立即停止长任务\n\n"
        "🩺 /status：检查 Hermes、CPA、反重力\n"
        "/project_status：检查账号池和模型\n"
        f"/project <任务>：只读分析 {PROJECT_NAME}\n"
        "/project_repair <任务>：高风险维修；服务器默认关闭，启用后仍需按钮确认\n\n"
        "🔐 /agy_login：登录反重力\n"
        "直接上传 Codex/CPA/Sub2 JSON：导入账号池\n\n"
        "在输入框键入 /，Telegram 会显示全部快捷命令和中文说明。"
    )


def format_status_summary(
    *,
    gateway: tuple[int, str],
    cpa: tuple[int, str],
    agy: tuple[int, str],
    account_count: int,
    model_count: int | None,
    session_active: bool,
) -> str:
    gateway_ok = gateway[0] == 0 and gateway[1].strip() == "active"
    cpa_ok = cpa[0] == 0 and cpa[1].strip() == "active"
    agy_line = agy[1].strip().splitlines()[-1] if agy[1].strip() else "不可用"
    agy_version = safe_text(agy_line, 200).replace("\n", " ")
    agy_ok = agy[0] == 0
    model_text = "检查失败" if model_count is None else f"{model_count} 个模型"

    lines = [
        "🩺 救援系统体检",
        "",
        f"{'✅' if gateway_ok else '❌'} Hermes Gateway：{'运行中' if gateway_ok else '未运行'}",
        f"{'✅' if cpa_ok else '❌'} 救援 CPA：{'运行中' if cpa_ok else '未运行'}",
        f"{'✅' if agy_ok else '❌'} Antigravity：{agy_version}",
        f"{'✅' if account_count and model_count else '⚠️'} 账号池：{account_count} 个账号 / {model_text}",
        f"💬 当前对话：{'已建立' if session_active else '全新会话'}",
    ]
    actions = []
    if not gateway_ok:
        actions.append("• Hermes 未运行：发送 /restart")
    if not cpa_ok or model_count is None:
        actions.append("• CPA 异常：发送 /project_status 查看详情")
    if not agy_ok:
        actions.append("• 反重力不可用：发送 /agy_login 重新登录")
    if actions:
        lines.extend(["", "建议操作：", *actions])
    return "\n".join(lines)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return await deny(update)
    notice = await update.message.reply_text("🩺 正在并行检查各项服务……")

    async def count_models() -> int | None:
        try:
            return len(await cpa_models())
        except Exception:
            return None

    gateway_result, cpa_result, agy_result, model_count = await asyncio.gather(
        run(["/usr/bin/systemctl", "--user", "is-active", GATEWAY_SERVICE], timeout=20),
        run(["/usr/bin/systemctl", "--user", "is-active", CPA_SERVICE], timeout=20),
        run(
            build_diagnostic_sandbox_args(
                [AGY, "--version"], mounted_executables=(Path(AGY),)
            ),
            timeout=20,
            cwd=ROOT,
        ),
        count_models(),
    )
    account_count = sum(1 for path in CPA_AUTH_DIR.glob("*.json") if path.is_file())
    summary = format_status_summary(
        gateway=gateway_result,
        cpa=cpa_result,
        agy=agy_result,
        account_count=account_count,
        model_count=model_count,
        session_active=chat_session_active(update.effective_user.id),
    )
    await notice.edit_text(summary)


async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return await deny(update)
    nonce = set_pending_confirmation(
        update.effective_user.id,
        "restart",
        "",
        chat_id=update.effective_chat.id,
    )
    await update.effective_message.reply_text(
        "⚠️ 确认重启 Hermes Gateway？\n"
        "影响：Hermes Telegram 主 Bot 会短暂离线，Rescue Bot 本身不受影响。\n"
        "确认按钮 5 分钟内有效。",
        reply_markup=confirmation_markup(nonce),
    )


async def agy_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return await deny(update)
    await update.effective_message.reply_text(
        "🔐 本 Bot 不处理登录链接、授权码，也不会代你接受 Google 条款。\n"
        "请在可信的服务器终端中直接运行官方 agy CLI，自行阅读并接受适用条款。"
    )


async def agy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return await deny(update)
    prompt = " ".join(context.args).strip()
    if not prompt:
        return await update.effective_message.reply_text("用法：/agy <任务>")
    notice = await update.effective_message.reply_text("🧠 AGY 正在安全沙箱中执行……")
    await run_agent_job(
        update,
        notice,
        build_agy_sandbox_args(
            [AGY, "--sandbox", "--print-timeout", "5m", "-p", agy_telegram_prompt(prompt)],
            mode="task",
        ),
        timeout=330,
        cwd=CHAT_DIR,
        label="AGY 任务",
        artifact_workspace=True,
    )


def format_project_status(
    *,
    service: tuple[int, str],
    version: tuple[int, str],
    account_count: int,
    model_count: int | None,
    repo_changes: int | None,
) -> str:
    service_text = safe_text(service[1] or str(service[0]), 160).replace("\n", " ")
    version_text = safe_text(version[1] or str(version[0]), 200).replace("\n", " ")
    return "\n".join(
        [
            f"{PROJECT_NAME} 状态",
            f"service={service_text}",
            f"version={version_text}",
            f"accounts={account_count}",
            f"models={'error' if model_count is None else model_count}",
            f"repo_changes={'unknown' if repo_changes is None else repo_changes}",
        ]
    )


async def project_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return await deny(update)
    if PROJECT_REPO is None:
        return await update.effective_message.reply_text("托管项目未配置；项目功能保持关闭。")

    async def count_models() -> int | None:
        try:
            return len(await cpa_models())
        except Exception:
            return None

    service_result, git_result, model_count = await asyncio.gather(
        run(["/usr/bin/systemctl", "--user", "is-active", PROJECT_SERVICE], timeout=20),
        run(
            build_diagnostic_sandbox_args(
                build_safe_git_status_args(), include_repo=True
            ),
            timeout=30,
            cwd=ROOT,
        ),
        count_models(),
    )
    version_result = (1, "版本探针已禁用：避免执行仓库控制的 Python 环境")
    git_code, git_out = git_result
    repo_changes = len(git_out.splitlines()) if git_code == 0 else None
    auth_count = sum(1 for path in CPA_AUTH_DIR.glob("*.json") if path.is_file())
    await update.effective_message.reply_text(
        format_project_status(
            service=service_result,
            version=version_result,
            account_count=auth_count,
            model_count=model_count,
            repo_changes=repo_changes,
        )
    )


def project_guard_prompt(task: str, *, repair: bool) -> str:
    mode = "authorized repository repair" if repair else "read-only repository analysis"
    filesystem_rule = (
        "The repository is the only writable project path. Runtime state, credential pools, "
        "the Rescue Bot source, other home files, and host service control are not mounted."
        if repair
        else "The repository is mounted read-only. Runtime state, credential pools, the Rescue Bot source, other home files, and host service control are not mounted."
    )
    return f"""You are managing the user's configured repository named {PROJECT_NAME} in {mode} mode.
User task: {task}

Repository: {PROJECT_REPO}
Enforced boundary: {filesystem_rule}

Mandatory rules:
1. Treat the managed repository as private code. Never push, publish, create a public repository, release, or upload source.
2. Never print or return access tokens, refresh tokens, session tokens, Bot tokens, API keys, emails, names, or raw credential JSON.
3. Keep every service bound to 127.0.0.1. Do not expose ports publicly.
4. Do not invoke systemctl, sudo, mount, namespace tools, or attempt to escape the sandbox. The Bot handles approved host actions.
5. Use the repository's existing tests. Do not claim runtime, account, or service verification that you cannot perform inside this boundary.
6. Limit work strictly to the mounted repository; in read-only mode, do not attempt any write.
7. Return a concise summary of inspected/changed files, real tests, and remaining risks, with all secrets redacted.
8. Answer in concise Simplified Chinese by default, lead with the conclusion, and use short headings or bullets.
9. Do not narrate tool calls, plans, internal reasoning, repeated probes, or waiting.
10. Do not return file:// links or scp/IDE-download instructions; include important conclusions inline.
"""


async def project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return await deny(update)
    if PROJECT_REPO is None:
        return await update.effective_message.reply_text("托管项目未配置；项目功能保持关闭。")
    task = " ".join(context.args).strip()
    if not task:
        return await update.effective_message.reply_text("用法：/project <只读分析任务>")
    notice = await update.effective_message.reply_text(f"🔎 AGY 正在沙箱中分析 {PROJECT_NAME}……")
    prompt = project_guard_prompt(task, repair=False)
    await run_agent_job(
        update,
        notice,
        build_agy_sandbox_args(
            [
                AGY,
                "--sandbox",
                "--print-timeout",
                "10m",
                "-p",
                prompt,
            ],
            mode="project-read",
        ),
        timeout=630,
        cwd=PROJECT_REPO,
        label=f"{PROJECT_NAME} 只读分析",
    )


async def project_repair(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return await deny(update)
    if PROJECT_REPO is None:
        return await update.effective_message.reply_text("托管项目未配置；维修功能保持关闭。")
    if not PROJECT_REPAIR_ENABLED:
        return await update.effective_message.reply_text(
            f"🔒 {PROJECT_NAME} 自动维修默认关闭。\n"
            "原因：维修模式会启用 AGY 工具执行，并使用已存在的官方登录令牌；"
            "只读分析仍可使用 /project。\n"
            "如明确接受该残余风险，再由服务器管理员设置 "
            "RESCUE_ENABLE_PROJECT_REPAIR=1 并重启 Bot。"
        )
    task = " ".join(context.args).strip()
    if task.startswith("CONFIRM "):
        task = task.removeprefix("CONFIRM ").strip()
    if not task:
        return await update.effective_message.reply_text(
            "用法：/project_repair <维修任务>\nBot 会显示范围并让你点击按钮确认。"
        )
    if len(task) > 3000:
        return await update.effective_message.reply_text("维修任务说明过长，请缩短到 3000 字以内。")
    nonce = set_pending_confirmation(
        update.effective_user.id,
        "project_repair",
        task,
        chat_id=update.effective_chat.id,
    )
    await update.effective_message.reply_text(
        f"⚠️ 确认执行 {PROJECT_NAME} 受控维修？\n"
        f"任务：{safe_text(task, 700)}\n\n"
        f"强制可写范围：仅 {PROJECT_REPO}\n"
        "不可访问：Rescue Bot、账号凭证池、其他 Home 文件和 host systemd。\n"
        "权限：AGY 内部跳过交互提示，但外层 Bubblewrap 会强制限制文件系统与进程范围。\n"
        "确认按钮 5 分钟内有效。",
        reply_markup=confirmation_markup(nonce),
    )


async def confirmation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    if not allowed(update):
        await query.answer("无权限", show_alert=True)
        return

    callback_match = re.fullmatch(
        r"(confirm|cancel):([A-Za-z0-9_-]{8,64})",
        query.data or "",
    )
    if callback_match is None:
        await query.answer("确认数据无效，未执行任何操作", show_alert=True)
        return
    verb, nonce = callback_match.groups()
    pending = take_pending_confirmation(
        nonce,
        user_id=update.effective_user.id,
        chat_id=update.effective_chat.id,
    )
    if pending is None:
        await query.answer("确认已失效或已使用", show_alert=True)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    action, payload = pending
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    if verb == "cancel":
        await query.edit_message_text("已取消，未执行任何操作。")
        return

    if action == "restart":
        await query.edit_message_text("🔄 正在重启 Hermes Gateway……")
        code, output = await run(
            ["/usr/bin/systemctl", "--user", "restart", GATEWAY_SERVICE],
            timeout=60,
        )
        if code == 0:
            await query.edit_message_text("✅ Hermes Gateway 已重启。Rescue Bot 始终保持在线。")
        else:
            await deliver_result(
                query.message,
                f"❌ Gateway 重启失败（exit={code}）\n{output or '(无输出)'}",
            )
        return

    if action == "project_repair":
        if not PROJECT_REPAIR_ENABLED:
            await query.edit_message_text(
                "🔒 自动维修当前已关闭，未执行任何操作；可继续使用 /project 只读分析。"
            )
            return
        await query.edit_message_text(f"🛠 AGY 正在受控维修 {PROJECT_NAME}……")
        prompt = project_guard_prompt(payload, repair=True)
        with ephemeral_agy_repair_storage() as (repair_state, repair_config):
            await run_agent_job(
                update,
                query.message,
                build_agy_sandbox_args(
                    [
                        AGY,
                        "--sandbox",
                        "--dangerously-skip-permissions",
                        "--add-dir",
                        str(PROJECT_REPO),
                        "--print-timeout",
                        "15m",
                        "-p",
                        prompt,
                    ],
                    mode="project-repair",
                    state_dir_override=repair_state,
                    config_dir_override=repair_config,
                ),
                timeout=930,
                cwd=PROJECT_REPO,
                label=f"{PROJECT_NAME} 受控维修",
                mutation=True,
            )
        return

    await query.edit_message_text("确认动作无法识别，未执行任何操作。")


def cpa_key() -> str:
    cfg = yaml.safe_load(CPA_CONFIG.read_text())
    return cfg["api-keys"][0]


def parse_cpa_model_payload(raw: bytes) -> list[str]:
    """Strictly validate the bounded CPA /v1/models response."""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("CPA models response is not valid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("CPA models response has an invalid shape")
    data = payload["data"]
    if not data or len(data) > 2000:
        raise ValueError("CPA models response has an invalid item count")
    model_ids: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("CPA model entry is not an object")
        model_id = item.get("id")
        if (
            not isinstance(model_id, str)
            or not model_id
            or len(model_id) > 256
            or any(ord(ch) < 32 for ch in model_id)
        ):
            raise ValueError("CPA model id is invalid")
        model_ids.add(model_id)
    return sorted(model_ids)


async def cpa_models(
    *,
    client: httpx.AsyncClient | None = None,
    api_key: str | None = None,
) -> list[str]:
    """Fetch CPA models with a total timeout and a hard response-size bound."""
    key = cpa_key() if api_key is None else api_key
    if not isinstance(key, str) or not key or any(ord(ch) < 32 for ch in key):
        raise ValueError("CPA API key is invalid")

    async def fetch(active_client: httpx.AsyncClient) -> list[str]:
        async with asyncio.timeout(25):
            async with active_client.stream(
                "GET",
                CPA_BASE_URL + "/v1/models",
                headers={"Authorization": "Bearer " + key},
            ) as response:
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        declared_size = int(content_length)
                    except ValueError as exc:
                        raise ValueError("CPA content-length is invalid") from exc
                    if declared_size < 0 or declared_size > MAX_HTTP_RESPONSE:
                        raise ValueError("CPA models response is too large")
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_HTTP_RESPONSE:
                        raise ValueError("CPA models response is too large")
        return parse_cpa_model_payload(bytes(raw))

    if client is not None:
        return await fetch(client)
    timeout = httpx.Timeout(20.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as owned_client:
        return await fetch(owned_client)


async def document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global CREDENTIAL_WRITES_QUARANTINED
    allowlisted = bool(update.effective_user and update.effective_user.id == ALLOWED_USER_ID)
    deleted_early = False
    if allowlisted and update.effective_message:
        try:
            await update.effective_message.delete()
            deleted_early = True
        except Exception:
            pass
    if not allowed(update):
        return await deny(update)
    message = update.effective_message
    doc = message.document
    if not doc:
        return await message.reply_text("没有检测到文件。")
    if CREDENTIAL_WRITES_QUARANTINED:
        return await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="🚨 凭证写入已被安全隔离。请停止使用 CPA 并由操作员人工检查；重启前不会再下载或导入凭证。",
        )
    if doc.file_name and not doc.file_name.lower().endswith(".json"):
        deleted = deleted_early
        try:
            await message.delete()
        except Exception:
            deleted = False
        return await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                "只接受 Codex、CPA 或 Sub2 的 JSON 凭证文件。"
                + ("" if deleted else "\n⚠️ 原文件消息未能自动删除，请手动删除。")
            ),
        )
    if not document_size_allowed(doc.file_size):
        deleted = True
        try:
            await message.delete()
        except Exception:
            deleted = False
        return await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                "文件大小未知、为空或超过 1 MiB，未进行处理。"
                + ("" if deleted else "\n⚠️ 原文件消息未能自动删除，请手动删除。")
            ),
        )
    if MUTATION_LOCK.locked():
        deleted = True
        try:
            await message.delete()
        except Exception:
            deleted = False
        return await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                "⏳ 账号导入或维修正在写入。本文件未处理，请稍后重新上传。"
                + ("" if deleted else "\n⚠️ 原文件消息未能自动删除，请手动删除。")
            ),
        )

    await MUTATION_LOCK.acquire()
    try:
        deleted = deleted_early
        if not deleted:
            try:
                await message.delete()
                deleted = True
            except Exception:
                deleted = False
        warning = "" if deleted else "\n⚠️ 原凭证消息未能自动删除，请立即手动删除。"

        notice = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="🔐 已收到凭证，正在本地校验并安全导入……" + warning,
        )

        try:
            telegram_file = await context.bot.get_file(doc.file_id)
            raw = bytes(await telegram_file.download_as_bytearray())
        except Exception as exc:
            error_id = secrets.token_hex(4)
            log.error(
                "credential download failed error_id=%s type=%s",
                error_id,
                type(exc).__name__,
            )
            return await notice.edit_text(
                f"❌ 凭证下载失败（错误编号 {error_id}），请稍后重新上传。" + warning
            )

        if len(raw) > MAX_DOCUMENT_BYTES:
            return await notice.edit_text("文件超过 1 MiB，未进行导入。" + warning)

        import_task = asyncio.create_task(
            asyncio.to_thread(import_cpa_bundle, raw, CPA_AUTH_DIR)
        )
        CREDENTIAL_IMPORT_TASKS.add(import_task)
        try:
            result = await asyncio.shield(import_task)
        except asyncio.CancelledError as cancelled:
            # Cancelling an await of to_thread() does not stop the underlying
            # filesystem transaction. Keep MUTATION_LOCK held until the worker
            # has committed or rolled back, then propagate cancellation.
            try:
                await _await_task_uninterruptibly(import_task)
            except CredentialRecoveryError:
                CREDENTIAL_WRITES_QUARANTINED = True
                log.critical("credential rollback incomplete during cancellation")
            except Exception as exc:
                log.error(
                    "credential transaction finished with error during cancellation type=%s",
                    type(exc).__name__,
                )
            raise cancelled
        except CredentialRecoveryError:
            CREDENTIAL_WRITES_QUARANTINED = True
            error_id = secrets.token_hex(4)
            log.critical("credential rollback incomplete error_id=%s", error_id)
            return await notice.edit_text(
                f"🚨 导入失败且回滚不完整（错误编号 {error_id}）。\n"
                "请立即停止使用 CPA，并人工检查本地账号池；Bot 不会继续自动写入。"
                + warning
            )
        except CredentialCommitError:
            return await notice.edit_text(
                "❌ 导入提交失败，但原有账号池已完整恢复；未保留本次变更。"
                + warning
            )
        except CredentialError:
            return await notice.edit_text(
                "❌ 未导入：文件格式不受支持或未通过安全校验。\n"
                "支持 Codex、CPA/C2API 和 Sub2 JSON。为避免误写账号池，Bot 不会自动用高权限 AGY 转换未知格式。"
                + warning
            )
        except Exception:
            error_id = secrets.token_hex(4)
            log.error("credential import failed error_id=%s type=unknown", error_id)
            return await notice.edit_text(
                f"❌ 导入失败（错误编号 {error_id}）。账号池没有通过 Bot 继续修改。" + warning
            )

        await asyncio.sleep(2)
        try:
            models = await cpa_models()
        except Exception:
            return await notice.edit_text(
                "⚠️ 凭证已写入账号池，但 CPA 健康验证未通过。\n"
                "请发送 /project_status 查看详情。"
                + warning
            )

        preferred = "gpt-5.6-sol" if "gpt-5.6-sol" in models else (models[0] if models else "无")
        preferred = re.sub(r"[^A-Za-z0-9._:+/-]", "_", preferred)[:128]
        await notice.edit_text(
            "✅ 凭证文件已导入\n"
            f"格式：{result.get('format', 'unknown')}\n"
            f"新增账号：{result.get('created', 0)}\n"
            f"更新账号：{result.get('updated', 0)}\n"
            f"未变化：{result.get('unchanged', 0)}\n"
            f"可用模型：{len(models)}\n"
            f"Hermes 首选：{preferred}\n"
            "CPA 模型接口可访问；这不代表已逐账号验证额度或刷新能力。"
            + warning
        )
    finally:
        if 'import_task' in locals() and import_task.done():
            CREDENTIAL_IMPORT_TASKS.discard(import_task)
        MUTATION_LOCK.release()


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return await deny(update)
    await update.effective_message.reply_text(
        "没有这个命令。发送 /help 查看说明，或在输入框键入 / 打开命令菜单。"
    )


async def unsupported_attachment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return await deny(update)
    await update.effective_message.reply_text(
        "当前附件无法直接处理。导入账号请上传 Codex、CPA/C2API 或 Sub2 的 JSON 文件；"
        "其他任务请直接发送文字说明。"
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    if isinstance(error, NetworkError):
        log.warning("temporary Telegram network error: %s", type(error).__name__)
        return

    error_id = secrets.token_hex(4)
    if error is not None:
        log.error(
            "handler failed error_id=%s type=%s",
            error_id,
            type(error).__name__,
        )
    else:
        log.error("handler failed error_id=%s without exception", error_id)
    if isinstance(update, Update) and allowed(update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                f"⚠️ 操作遇到临时错误（编号 {error_id}）。可重试，或发送 /status 检查状态。"
            )
        except Exception:
            log.warning("failed to deliver error notice error_id=%s", error_id)


async def shutdown_bot(application: Application | None) -> None:
    del application
    processes = list(dict.fromkeys(ACTIVE_PROCS.values()))

    if processes:
        results = await asyncio.gather(
            *(_terminate_process(proc) for proc in processes),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                log.error("shutdown process cleanup failed: %s", type(result).__name__)
    imports = list(CREDENTIAL_IMPORT_TASKS)
    if imports:
        await asyncio.gather(*imports, return_exceptions=True)
    pending_spawns = list(SPAWN_TASKS)
    for spawn_task in pending_spawns:
        if not any(getattr(reaper, "_rescue_spawn_task", None) is spawn_task for reaper in SPAWN_REAPERS):
            reaper = asyncio.create_task(_reap_spawned_process(spawn_task))
            reaper._rescue_spawn_task = spawn_task
            _track_spawn_reaper(reaper)
    reapers = list(SPAWN_REAPERS)
    if reapers:
        try:
            await asyncio.wait_for(asyncio.shield(asyncio.gather(*reapers, return_exceptions=True)), timeout=10)
        except asyncio.TimeoutError:
            log.error("shutdown timed out waiting for spawn reapers")
        # Task done callbacks are scheduled with call_soon; gathering the task
        # may return before _finish_spawn_reaper has removed it from the set.
        await asyncio.sleep(0)
        SPAWN_REAPERS.difference_update(task for task in reapers if task.done())

    CREDENTIAL_IMPORT_TASKS.difference_update(task for task in imports if task.done())
    SPAWN_TASKS.difference_update(task for task in pending_spawns if task.done())

    ACTIVE_PROCS.clear()
    JOB_STARTING.clear()
    CHAT_PRESPAWN.clear()
    JOB_CANCEL_REQUESTED.clear()


def main() -> None:
    token = os.environ.get("RESCUE_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("RESCUE_BOT_TOKEN is not configured")
    app = (
        Application.builder()
        .token(token)
        .concurrent_updates(8)
        .post_init(configure_bot)
        .post_shutdown(shutdown_bot)
        .build()
    )
    app.add_handler(CallbackQueryHandler(confirmation_callback, pattern=r"^(confirm|cancel):"))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("ask", ask))
    app.add_handler(CommandHandler("new", new_chat))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("restart", restart))
    app.add_handler(CommandHandler("agy_login", agy_login))
    app.add_handler(CommandHandler("agy", agy))
    app.add_handler(CommandHandler("project_status", project_status))
    app.add_handler(CommandHandler("project", project))
    app.add_handler(CommandHandler("project_repair", project_repair))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    app.add_handler(MessageHandler(filters.Document.ALL, document))
    app.add_handler(
        MessageHandler(
            filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE,
            unsupported_attachment,
        )
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_chat))
    app.add_error_handler(error_handler)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
