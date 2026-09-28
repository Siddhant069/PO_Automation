"""Encrypted-at-rest storage for Playwright storage_state (cookies + JWT)."""
import json
import logging
from cryptography.fernet import Fernet, InvalidToken
import config

log = logging.getLogger(__name__)


def _fernet() -> Fernet:
    if not config.SESSION_ENC_KEY:
        raise RuntimeError("SESSION_ENC_KEY not set — generate one (see .env.example)")
    return Fernet(config.SESSION_ENC_KEY.encode())


def save(storage_state: dict) -> None:
    payload = json.dumps(storage_state).encode()
    token = _fernet().encrypt(payload)
    config.SESSION_STATE_PATH.write_bytes(token)
    config.SESSION_STATE_PATH.chmod(0o600)
    log.info("Session state saved (%s)", config.SESSION_STATE_PATH)


def load() -> dict | None:
    if not config.SESSION_STATE_PATH.exists():
        return None
    try:
        token = config.SESSION_STATE_PATH.read_bytes()
        return json.loads(_fernet().decrypt(token))
    except (InvalidToken, ValueError, json.JSONDecodeError) as e:
        log.warning("Stored session unreadable (%s) — discarding", e)
        config.SESSION_STATE_PATH.unlink(missing_ok=True)
        return None


def clear() -> None:
    config.SESSION_STATE_PATH.unlink(missing_ok=True)
