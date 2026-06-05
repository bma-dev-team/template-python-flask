"""Reusable encrypted named-secret store (shared, byte-identical across builds).

A build declares the buyer-supplied credentials it needs (an LLM API key, a SaaS
token) as ``SecretSpec`` entries; values are persisted in a single Fernet-encrypted
JSON file (``instance/secrets.enc``, mode 0600) and resolved ``env/config > stored >
none`` per name. The encryption key is HKDF-derived from the app's ``SECRET_KEY`` (no
new key to provision); a dump of the file is useless wherever the key lives apart from
the data (a future hosted DB backend), and on a single-user desktop the OS file
permissions + full-disk encryption are the real boundary (the Fernet layer future-proofs).

Secret values are never logged, never rendered, and never returned by ``status`` (only a
masked last-4). See docs/superpowers/specs/2026-06-04-reusable-encrypted-secret-store-design.md
and developer-guides/architecture/bma-security-features.md.
"""
from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_STORE_FILENAME = "secrets.enc"
_LEGACY_AI_KEY_FILENAME = "anthropic_api_key"  # pre-migration single-key plaintext store
_HKDF_INFO = b"bma-secret-store-v1"

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SecretSpec:
    """A buyer-supplied secret a build declares it needs (drives the P2 Settings UI)."""

    name: str          # config/env name, e.g. "ANTHROPIC_API_KEY"
    label: str         # UI label, e.g. "Anthropic API key"
    kind: str = "secret"   # "secret" (masked, last-4) | "text" (shown, e.g. a store domain)
    help: str = ""     # one-line UI hint


class SecretStore(Protocol):
    """The seam the deferred backends (hosted Postgres, multi-user) slot behind."""

    def get(self, name: str) -> str | None: ...
    def set(self, name: str, value: str) -> None: ...
    def clear(self, name: str) -> None: ...
    def set_at(self, name: str) -> str | None: ...


def _derive_fernet_key(secret_key) -> bytes:
    """A urlsafe-b64 Fernet key HKDF-derived from the app SECRET_KEY (str or bytes).

    Fails loudly on a missing/empty key: deriving from an empty input would yield a
    single well-known key shared across every misconfigured install, defeating the
    encryption. In production ``create_app`` always resolves SECRET_KEY before the
    store is used, so this only guards a misconfigured caller / bare test stub.
    """
    if not secret_key:
        raise ValueError(
            "EncryptedFileStore requires a non-empty SECRET_KEY; set one before "
            "instantiating the store."
        )
    if isinstance(secret_key, str):
        secret_key = secret_key.encode("utf-8")
    raw = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO
    ).derive(secret_key)
    return base64.urlsafe_b64encode(raw)


class EncryptedFileStore:
    """Default backend: one Fernet-encrypted 0600 JSON file in the instance folder."""

    def __init__(self, instance_path: str, secret_key) -> None:
        self._instance_path = instance_path
        self._path = os.path.join(instance_path, _STORE_FILENAME)
        self._fernet = Fernet(_derive_fernet_key(secret_key))

    def _read_all(self) -> dict:
        try:
            with open(self._path, "rb") as fh:
                blob = fh.read()
        except (FileNotFoundError, OSError):
            return {}
        if not blob:
            return {}
        try:
            plaintext = self._fernet.decrypt(blob)
        except InvalidToken:
            _log.warning(
                "secret_store: could not decrypt %s (wrong SECRET_KEY or corrupted "
                "file); treating as empty. Stored secrets are inaccessible until the "
                "original SECRET_KEY is restored.",
                self._path,
            )
            return {}
        try:
            data = json.loads(plaintext.decode("utf-8"))
        except ValueError:
            _log.warning("secret_store: corrupt JSON in %s; treating as empty.", self._path)
            return {}
        return data if isinstance(data, dict) else {}

    def _write_all(self, data: dict) -> None:
        os.makedirs(self._instance_path, exist_ok=True)
        blob = self._fernet.encrypt(json.dumps(data).encode("utf-8"))
        tmp = self._path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)  # force 0600 regardless of the process umask
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(blob)
            os.replace(tmp, self._path)
            os.chmod(self._path, 0o600)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def get(self, name: str) -> str | None:
        entry = self._read_all().get(name)
        return entry.get("value") if isinstance(entry, dict) else None

    def set(self, name: str, value: str) -> None:
        v = (value or "").strip()
        if not v:
            raise ValueError("secret value must not be empty")
        data = self._read_all()
        data[name] = {
            "value": v,
            "set_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self._write_all(data)

    def clear(self, name: str) -> None:
        data = self._read_all()
        if name in data:
            del data[name]
            self._write_all(data)

    def set_at(self, name: str) -> str | None:
        entry = self._read_all().get(name)
        return entry.get("set_at") if isinstance(entry, dict) else None


def get_store(app) -> EncryptedFileStore:
    """The store bound to this app's instance folder + SECRET_KEY."""
    return EncryptedFileStore(app.instance_path, app.config.get("SECRET_KEY"))


def _configured(app, name: str) -> str | None:
    """The env/config value, normalized: whitespace-only counts as unset."""
    return (app.config.get(name) or "").strip() or None


def _last4(value) -> str | None:
    """Last 4 characters of value, or None if value is None or shorter than 4 chars."""
    return value[-4:] if value and len(value) >= 4 else None


def resolve(app, name: str) -> str | None:
    """Effective value: env/config first, else the stored value, else None."""
    configured = _configured(app, name)
    if configured:
        return configured
    return get_store(app).get(name)


def status(app, name: str, kind: str = "secret") -> dict:
    """UI status; carries NO usable value (only a masked last-4 for kind='secret')."""
    configured = _configured(app, name)
    if configured:
        return {
            "configured": True, "source": "env", "set_at": None,
            "last4": _last4(configured) if kind == "secret" else None,
        }
    store = get_store(app)
    value = store.get(name)
    if value:
        return {
            "configured": True, "source": "settings", "set_at": store.set_at(name),
            "last4": _last4(value) if kind == "secret" else None,
        }
    return {"configured": False, "source": None, "set_at": None, "last4": None}


def migrate_legacy(app) -> None:
    """One-time: fold the legacy plaintext ``anthropic_api_key`` file into the encrypted
    store under ANTHROPIC_API_KEY (without clobbering an existing value), then delete it."""
    legacy = os.path.join(app.instance_path, _LEGACY_AI_KEY_FILENAME)
    try:
        with open(legacy, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, OSError, ValueError):
        return
    key = (data.get("key") or "").strip() if isinstance(data, dict) else ""
    if key:
        store = get_store(app)
        if store.get("ANTHROPIC_API_KEY") is None:
            store.set("ANTHROPIC_API_KEY", key)
    try:
        os.unlink(legacy)
    except OSError:
        pass
