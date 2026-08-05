from __future__ import annotations

import hashlib
import hmac
import json


def validate_sidecar_auth_key(auth_key: str) -> bytes:
    if not isinstance(auth_key, str) or len(auth_key) != 64:
        raise ValueError("Origin 进程身份记录认证密钥无效")
    try:
        return bytes.fromhex(auth_key)
    except ValueError as exc:
        raise ValueError("Origin 进程身份记录认证密钥无效") from exc


def sidecar_content_hmac(
    payload: dict[str, object],
    auth_key: str,
) -> str:
    document = dict(payload)
    document.pop("content_hmac", None)
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(
        validate_sidecar_auth_key(auth_key),
        encoded,
        hashlib.sha256,
    ).hexdigest()
