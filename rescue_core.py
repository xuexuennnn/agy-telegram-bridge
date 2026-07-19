"""Security-sensitive core helpers for the independent Hermes rescue bot."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


API_KEY_PROVIDERS = frozenset({
    "alibaba", "alibaba-coding-plan", "anthropic", "arcee", "azure-foundry",
    "bedrock", "deepseek", "gemini", "gmi", "huggingface", "kilocode",
    "kimi-coding", "minimax", "nvidia", "novita", "openai-api", "opencode-zen",
    "openrouter", "qwen", "stepfun", "xai", "zai",
})
MAX_ACCOUNT_BUNDLE_BYTES = 1024 * 1024
MAX_ACCOUNTS = 20
MAX_JSON_DEPTH = 16
MAX_JSON_NODES = 4096


class CredentialError(ValueError):
    """Raised when an uploaded credential document is invalid."""


class CredentialCommitError(CredentialError):
    """A commit failed but the previous on-disk state was restored."""


class CredentialRecoveryError(CredentialError):
    """A commit failed and rollback could not restore a known-good state."""


@dataclass(repr=False, frozen=True)
class ApiCredential:
    provider: str
    api_key: str = field(repr=False)
    label: str

    def __repr__(self) -> str:
        return (
            f"ApiCredential(provider={self.provider!r}, "
            f"api_key='[REDACTED]', label={self.label!r})"
        )


@dataclass(repr=False, frozen=True)
class CodexCredential:
    tokens: dict[str, str] = field(repr=False)
    last_refresh: str | None
    label: str
    refreshable: bool
    expires_at: str | None

    def __repr__(self) -> str:
        return (
            f"CodexCredential(tokens='[REDACTED]', "
            f"last_refresh={self.last_refresh!r}, label={self.label!r}, "
            f"refreshable={self.refreshable!r}, expires_at={self.expires_at!r})"
        )


@dataclass(frozen=True)
class CpaInstallResult:
    path: Path
    refreshable: bool
    expires_at: str | None


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CredentialError(f"JSON 包含重复字段：{key[:80]}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise CredentialError(f"JSON 不允许非常量数值：{value}")


def _validate_json_resources(value: Any) -> None:
    nodes = 0

    def visit(item: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise CredentialError("JSON 结构过大")
        if depth > MAX_JSON_DEPTH:
            raise CredentialError("JSON 嵌套层级过深")
        if isinstance(item, str):
            if len(item) > 128 * 1024:
                raise CredentialError("JSON 字符串长度异常")
        elif isinstance(item, dict):
            if len(item) > 128:
                raise CredentialError("JSON 对象字段过多")
            for key, child in item.items():
                if len(key) > 128:
                    raise CredentialError("JSON 字段名过长")
                visit(child, depth + 1)
        elif isinstance(item, list):
            if len(item) > 256:
                raise CredentialError("JSON 数组条目过多")
            for child in item:
                visit(child, depth + 1)
        elif item is not None and not isinstance(item, (bool, int, float)):
            raise CredentialError("JSON 包含不支持的数据类型")

    visit(value, 0)


def _load_json_object(raw: bytes) -> dict[str, Any]:
    if not raw:
        raise CredentialError("凭证文件为空")
    if len(raw) > MAX_ACCOUNT_BUNDLE_BYTES:
        raise CredentialError("凭证文件超过 1 MiB 限制")
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except CredentialError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CredentialError("凭证文件必须是有效的严格 UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise CredentialError("凭证 JSON 顶层必须是对象")
    _validate_json_resources(payload)
    return payload


def parse_api_credential(raw: bytes) -> ApiCredential:
    payload = _load_json_object(raw)
    provider = str(payload.get("provider", "")).strip().lower()
    api_key = str(payload.get("api_key", "")).strip()
    label = str(payload.get("label", "telegram-rescue")).strip() or "telegram-rescue"
    if provider not in API_KEY_PROVIDERS:
        raise CredentialError(f"不支持的 API provider：{provider or '(空)'}")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in api_key):
        raise CredentialError("API key 不得包含换行或控制字符")
    if not api_key or len(api_key) > 8192:
        raise CredentialError("API key 长度无效")
    if len(label) > 64 or any(ord(ch) < 32 for ch in label):
        raise CredentialError("凭证标签无效")
    return ApiCredential(provider=provider, api_key=api_key, label=label)


def parse_codex_credential(raw: bytes) -> CodexCredential:
    payload = _load_json_object(raw)
    candidate = payload.get("tokens", payload)
    if not isinstance(candidate, dict):
        raise CredentialError("Codex tokens 必须是 JSON 对象")

    tokens: dict[str, str] = {}
    for key in ("access_token", "refresh_token", "id_token", "account_id"):
        value = candidate.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            raise CredentialError(f"Codex {key} 格式无效")
        value = value.strip()
        # CPA treats these as optional metadata. Exporters commonly emit
        # empty strings for unavailable refresh/id/account fields; omitting
        # them is valid and safer than rejecting an otherwise usable token.
        if not value:
            continue
        if len(value) > 32768 or any(ord(ch) < 32 for ch in value):
            raise CredentialError(f"Codex {key} 格式无效")
        tokens[key] = value

    if "access_token" not in tokens:
        raise CredentialError("Codex 凭证缺少 access_token")

    last_refresh_value = payload.get("last_refresh")
    last_refresh = None
    if last_refresh_value is not None:
        if not isinstance(last_refresh_value, str) or len(last_refresh_value) > 80:
            raise CredentialError("Codex last_refresh 格式无效")
        last_refresh = last_refresh_value.strip() or None
    label = str(payload.get("label", "telegram-codex")).strip() or "telegram-codex"
    if len(label) > 64 or any(ord(ch) < 32 for ch in label):
        raise CredentialError("Codex 凭证标签无效")
    expires_value = payload.get("expired")
    expires_at = None
    if expires_value is not None:
        if not isinstance(expires_value, str) or len(expires_value) > 80:
            raise CredentialError("Codex expired 格式无效")
        expires_at = expires_value.strip() or None
    return CodexCredential(
        tokens=tokens,
        last_refresh=last_refresh,
        label=label,
        refreshable=bool(tokens.get("refresh_token")),
        expires_at=expires_at,
    )


def _stored_codex_payload(credential: CodexCredential) -> bytes:
    stored: dict[str, str] = {"type": "codex", **credential.tokens}
    if credential.last_refresh:
        stored["last_refresh"] = credential.last_refresh
    if credential.expires_at:
        stored["expired"] = credential.expires_at
    return (json.dumps(stored, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def _credential_destination(credential: CodexCredential, auth_dir: Path) -> Path:
    digest = hashlib.sha256(credential.tokens["access_token"].encode()).hexdigest()[:16]
    return auth_dir / f"codex-{digest}.json"


def _secure_auth_dir(path: Path) -> None:
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise CredentialError(f"凭据路径不允许符号链接：{candidate}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise CredentialError("凭据路径不是目录")
    os.chmod(path, 0o700)
    if stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise CredentialError("无法为凭据目录设置 0700 权限")


def _validate_destination(path: Path) -> None:
    if path.is_symlink():
        raise CredentialError("凭据目标不允许符号链接")
    if path.exists():
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise CredentialError("凭据目标不是普通文件")
        if info.st_nlink != 1:
            raise CredentialError("凭据目标不允许硬链接")
        if info.st_size > MAX_ACCOUNT_BUNDLE_BYTES:
            raise CredentialError("现有凭据文件大小异常")


def _atomic_bytes_write(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    _validate_destination(path)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        os.chmod(path, mode)
    except Exception:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def install_cpa_credential(raw: bytes, auth_dir: Path) -> CpaInstallResult:
    """Validate and atomically install one minimal CPA Codex auth file."""
    credential = parse_codex_credential(raw)
    auth_dir = Path(auth_dir)
    _secure_auth_dir(auth_dir)
    destination = _credential_destination(credential, auth_dir)
    _atomic_bytes_write(destination, _stored_codex_payload(credential))
    return CpaInstallResult(
        path=destination,
        refreshable=credential.refreshable,
        expires_at=credential.expires_at,
    )


def _sub2_expiry(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CredentialError("Sub2 expires_at 必须是 Unix 时间戳")
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError) as exc:
        raise CredentialError("Sub2 expires_at 超出有效范围") from exc


def _sub2_credential(account: Any, exported_at: Any) -> CodexCredential:
    if not isinstance(account, dict):
        raise CredentialError("Sub2 accounts 中存在无效条目")
    if account.get("platform") != "openai" or account.get("type") != "oauth":
        raise CredentialError("Sub2 文件包含当前不支持的账号类型")
    credentials = account.get("credentials")
    if not isinstance(credentials, dict):
        raise CredentialError("Sub2 账号缺少 credentials 对象")
    extra = account.get("extra", {})
    if not isinstance(extra, dict):
        raise CredentialError("Sub2 extra 必须是对象")
    password = credentials.get("password", "")
    if password not in ("", None, "Takeover_NoPassword"):
        raise CredentialError("账号 JSON 不允许导入真实密码")

    id_token = credentials.get("id_token")
    if not isinstance(id_token, str) or not id_token.strip():
        raise CredentialError("Sub2 账号缺少 id_token")
    last_refresh = extra.get("last_refresh") or exported_at or None
    if last_refresh is not None and not isinstance(last_refresh, str):
        raise CredentialError("Sub2 last_refresh 格式无效")
    expiry = _sub2_expiry(credentials.get("expires_at")) or _sub2_expiry(
        account.get("expires_at")
    )
    normalized = {
        "type": "codex",
        "access_token": credentials.get("access_token"),
        "refresh_token": credentials.get("refresh_token"),
        "id_token": id_token,
        "account_id": credentials.get("account_id") or account.get("account_id"),
        "last_refresh": last_refresh,
        "expired": expiry,
    }
    encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode()
    return parse_codex_credential(encoded)


def _parse_cpa_bundle(raw: bytes) -> tuple[str, list[CodexCredential]]:
    payload = _load_json_object(raw)
    accounts = payload.get("accounts")
    if accounts is not None:
        if not isinstance(accounts, list) or not accounts:
            raise CredentialError("Sub2 文件不包含有效账号")
        if len(accounts) > MAX_ACCOUNTS:
            raise CredentialError(f"一次最多导入 {MAX_ACCOUNTS} 个账号")
        exported_at = payload.get("exported_at")
        if exported_at is not None and not isinstance(exported_at, str):
            raise CredentialError("Sub2 exported_at 格式无效")
        return "sub2", [_sub2_credential(account, exported_at) for account in accounts]
    if payload.get("type") == "codex":
        return "cpa", [parse_codex_credential(raw)]
    if "tokens" in payload:
        return "codex", [parse_codex_credential(raw)]
    raise CredentialError("无法识别该 JSON；仅支持 Codex、CPA/C2API 或 Sub2 导出")


def import_cpa_bundle(raw: bytes, auth_dir: Path) -> dict[str, Any]:
    """Validate a CPA/Sub2 bundle, then commit it as a recoverable local batch."""
    bundle_format, credentials = _parse_cpa_bundle(raw)
    auth_dir = Path(auth_dir)
    _secure_auth_dir(auth_dir)
    prepared = [
        (
            _credential_destination(credential, auth_dir),
            _stored_codex_payload(credential),
        )
        for credential in credentials
    ]
    paths = [path for path, _ in prepared]
    if len(paths) != len(set(paths)):
        raise CredentialError("账号文件包含重复逻辑凭据")

    backups: dict[Path, tuple[bytes, int] | None] = {}
    for path in paths:
        _validate_destination(path)
        if path.exists():
            backups[path] = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        else:
            backups[path] = None

    created = sum(1 for path, _ in prepared if backups[path] is None)
    unchanged = sum(
        1
        for path, payload in prepared
        if backups[path] is not None and backups[path][0] == payload
    )
    updated = len(prepared) - created - unchanged
    changed = [
        (path, payload)
        for path, payload in prepared
        if backups[path] is None or backups[path][0] != payload
    ]

    written: list[Path] = []
    try:
        for path, payload in changed:
            written.append(path)
            _atomic_bytes_write(path, payload)
    except Exception as exc:
        recovery_errors = []
        for path in reversed(written):
            try:
                backup = backups[path]
                if backup is None:
                    if path.exists() or path.is_symlink():
                        _validate_destination(path)
                        path.unlink()
                else:
                    old_payload, old_mode = backup
                    _atomic_bytes_write(path, old_payload, mode=old_mode)
            except Exception as recovery_exc:
                recovery_errors.append(type(recovery_exc).__name__)
        if recovery_errors:
            raise CredentialRecoveryError(
                "账号导入失败且恢复不完整；请停止使用并检查本地数据"
            ) from exc
        raise CredentialCommitError("账号导入失败；原有本地数据已恢复") from exc

    return {
        "format": bundle_format,
        "imported": created + updated,
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
    }
