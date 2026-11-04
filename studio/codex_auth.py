"""Private, task-scoped Codex login files; public responses never contain credentials."""

import hashlib
import json
import os
from pathlib import Path
import re
import secrets


MAX_AUTH_BYTES = 64 * 1024
AUTH_REFERENCE = re.compile(r"[a-f0-9]{64}\Z")


def validate_auth(value):
    if not isinstance(value, dict):
        raise ValueError("请选择有效的 Codex auth.json 登录文件")

    def present(text):
        return isinstance(text, str) and bool(text.strip()) and not any(c in text for c in "\r\n\x00")

    tokens = value.get("tokens")
    if not (present(value.get("OPENAI_API_KEY")) or isinstance(tokens, dict)
            and all(present(tokens.get(key)) for key in ("access_token", "refresh_token", "id_token"))):
        raise ValueError("auth.json 缺少完整的 Codex 登录凭据")
    try:
        data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        raise ValueError("请选择有效的 Codex auth.json 登录文件") from None
    if len(data) > MAX_AUTH_BYTES:
        raise ValueError("auth.json 文件不能超过 64 KB")
    return data


class CodexAuthStore:
    def __init__(self, state):
        self.root = Path(state) / "codex-auth"

    def save(self, value):
        data = validate_auth(value)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        reference = secrets.token_hex(32)
        # A path printed by a subprocess must not reveal the bearer reference.
        home = self.root / hashlib.sha256(reference.encode("ascii")).hexdigest()
        home.mkdir(mode=0o700)
        for name, content in (("auth.json", data),
                              ("config.toml", b'cli_auth_credentials_store = "file"\n')):
            with os.fdopen(os.open(home / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as file:
                file.write(content)
        return reference

    def home(self, reference):
        if not isinstance(reference, str) or not AUTH_REFERENCE.fullmatch(reference):
            raise ValueError("Codex 登录文件未保存或已失效，请重新添加 auth.json")
        home = self.root / hashlib.sha256(reference.encode("ascii")).hexdigest()
        if not (home / "auth.json").is_file():
            raise ValueError("Codex 登录文件未保存或已失效，请重新添加 auth.json")
        return home

    def reference(self, value):
        if value is None or value == "":
            return None
        self.home(value)
        return value

    def environment(self, reference, base):
        env = dict(base)
        if reference is not None:
            home = self.home(reference)
            for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_BASE_URL",
                         "OPENAI_API_BASE", "CHATGPT_BASE_URL"):
                env.pop(name, None)
            env["CODEX_HOME"] = str(home.resolve())
        return env
