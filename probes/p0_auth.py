#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

MARKER = "PINKER_AUTH_OK"
CODEX = "codex-velina"


@dataclass
class Result:
    provider: str
    cli_version: str = "UNKNOWN"
    existing_auth_reused: bool = False
    subscription_login: str = "DISCARDED"
    request_verified: bool = False
    model: str = "UNAVAILABLE"
    quota: str = "UNSUPPORTED"
    quota_detail: list[dict[str, Any]] | None = None
    error: str | None = None


def run(args: list[str], *, env=None, cwd=None, timeout=120):
    return subprocess.run(
        args,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=cwd,
        timeout=timeout,
        check=False,
    )


def version(command: str) -> str:
    try:
        cp = run([command, "--version"], timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return "UNKNOWN"
    lines = (cp.stdout or cp.stderr).strip().splitlines()
    return lines[0] if lines else "UNKNOWN"


def choose(title: str, labels: list[str]) -> int:
    print(title)
    for i, label in enumerate(labels, 1):
        print(f"  {i}. {label}")
    while True:
        raw = input("> ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(labels):
            return int(raw) - 1
        print("Escolha inválida.")


def find_model(value: Any) -> str | None:
    if isinstance(value, dict):
        if isinstance(value.get("model"), str):
            return value["model"]
        usage = value.get("modelUsage")
        if isinstance(usage, dict) and usage:
            return next(iter(usage))
        for child in value.values():
            found = find_model(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_model(child)
            if found:
                return found
    return None


def anthropic_subscription_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
    ):
        env.pop(key, None)
    return env


def verify_codex() -> tuple[bool, str]:
    with tempfile.TemporaryDirectory(prefix="pinker-harness-p0-") as cwd:
        cp = run(
            [
                CODEX,
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--json",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "-C",
                cwd,
                f"Responda somente com {MARKER}. Não use ferramentas.",
            ],
            cwd=cwd,
            timeout=180,
        )
    if cp.returncode != 0 or MARKER not in cp.stdout:
        return False, "UNAVAILABLE"
    for line in cp.stdout.splitlines():
        try:
            model = find_model(json.loads(line))
        except json.JSONDecodeError:
            continue
        if model:
            return True, model
    return True, "UNAVAILABLE"


def verify_claude(env: dict[str, str]) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory(prefix="pinker-harness-p0-") as cwd:
        cp = run(
            [
                "claude",
                "-p",
                f"Responda somente com {MARKER}. Não use ferramentas.",
                "--output-format",
                "json",
            ],
            env=env,
            cwd=cwd,
            timeout=180,
        )
    if cp.returncode != 0 or MARKER not in cp.stdout:
        return False, "UNAVAILABLE"
    try:
        return True, find_model(json.loads(cp.stdout)) or "UNAVAILABLE"
    except json.JSONDecodeError:
        return True, "UNAVAILABLE"


def codex_quota() -> tuple[str, list[dict[str, Any]] | None]:
    messages = [
        {
            "method": "initialize",
            "id": 1,
            "params": {
                "clientInfo": {
                    "name": "pinker-harness-p0",
                    "title": "Pinker Harness P0",
                    "version": "0.1",
                }
            },
        },
        {"method": "initialized", "params": {}},
        {"method": "account/rateLimits/read", "id": 2, "params": {}},
    ]
    wire = "".join(json.dumps(message) + "\n" for message in messages)
    try:
        cp = subprocess.run(
            [CODEX, "app-server", "--stdio"],
            input=wire,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "UNAVAILABLE", None

    result = None
    for line in cp.stdout.splitlines():
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if message.get("id") == 2 and isinstance(message.get("result"), dict):
            result = message["result"]
            break

    limits = result.get("rateLimits") if isinstance(result, dict) else None
    if not isinstance(limits, dict):
        return "UNAVAILABLE", None

    windows = []
    for name in ("primary", "secondary"):
        window = limits.get(name)
        if not isinstance(window, dict) or not isinstance(
            window.get("usedPercent"), (int, float)
        ):
            continue
        windows.append(
            {
                "name": name,
                "used_percent": window["usedPercent"],
                "window_minutes": window.get("windowDurationMins"),
                "resets_at": window.get("resetsAt"),
            }
        )

    if not windows:
        return "UNAVAILABLE", None

    def label(window):
        minutes = window["window_minutes"]
        if minutes == 300:
            period = "5h"
        elif minutes == 10080:
            period = "7d"
        elif isinstance(minutes, int):
            period = f"{minutes}min"
        else:
            period = window["name"]
        return f'{window["used_percent"]}% used ({period})'

    return " | ".join(label(window) for window in windows), windows


def probe_openai() -> Result:
    out = Result(provider="OpenAI")
    if shutil.which(CODEX) is None:
        out.error = f"{CODEX} não encontrado no PATH"
        return out

    out.cli_version = version(CODEX)
    status = run([CODEX, "login", "status"], timeout=30)
    chatgpt = status.returncode == 0 and "chatgpt" in (
        f"{status.stdout}\n{status.stderr}".casefold()
    )

    if chatgpt:
        out.existing_auth_reused = True
    else:
        print("Abrindo login oficial do ChatGPT/Codex...")
        if subprocess.run([CODEX, "login"], check=False).returncode != 0:
            out.error = "login oficial do Codex falhou"
            return out
        status = run([CODEX, "login", "status"], timeout=30)
        chatgpt = status.returncode == 0 and "chatgpt" in (
            f"{status.stdout}\n{status.stderr}".casefold()
        )
        if not chatgpt:
            out.error = "Codex não confirmou autenticação via ChatGPT"
            return out

    ok, out.model = verify_codex()
    if not ok:
        out.error = "login existe, mas a chamada mínima falhou"
        return out

    out.subscription_login = "PASS"
    out.request_verified = True
    out.quota, out.quota_detail = codex_quota()
    return out


def claude_status(env: dict[str, str]) -> bool:
    cp = run(["claude", "auth", "status"], env=env, timeout=30)
    try:
        payload = json.loads(cp.stdout)
    except json.JSONDecodeError:
        return False
    if cp.returncode != 0 or payload.get("loggedIn") is not True:
        return False
    method = str(payload.get("authMethod") or "").casefold()
    return bool(payload.get("subscriptionType")) or method in {
        "claude.ai",
        "oauth_token",
    }


def probe_anthropic() -> Result:
    out = Result(provider="Anthropic")
    if shutil.which("claude") is None:
        out.error = "claude não encontrado no PATH"
        return out

    out.cli_version = version("claude")
    env = anthropic_subscription_env()

    if claude_status(env):
        out.existing_auth_reused = True
    else:
        print("Abrindo login oficial da assinatura Claude...")
        if subprocess.run(
            ["claude", "auth", "login"], env=env, check=False
        ).returncode != 0:
            out.error = "login oficial do Claude Code falhou"
            return out
        if not claude_status(env):
            out.error = "Claude Code não confirmou autenticação de assinatura"
            return out

    ok, out.model = verify_claude(env)
    if not ok:
        out.error = "login existe, mas a chamada mínima falhou"
        return out

    out.subscription_login = "PASS"
    out.request_verified = True
    out.quota = "UNSUPPORTED"
    return out


def save(result: Result) -> Path:
    root = Path.home() / "Downloads"
    if not root.is_dir():
        root = Path.cwd()
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    path = root / f"pinker-harness-p0-auth-{stamp}.json"
    path.write_text(
        json.dumps(asdict(result), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def main() -> int:
    print("P0 — autenticação por assinatura")
    print("API será validada em probe separado; não existe fallback silencioso.")
    provider = choose("Provider:", ["OpenAI", "Anthropic"])

    result = probe_openai() if provider == 0 else probe_anthropic()
    evidence = save(result)

    if result.subscription_login == "PASS":
        print(
            "Conta logada com sucesso! "
            f"Provedor: {result.provider}; "
            f"modelo: {result.model}; quota: {result.quota}"
        )
        print(f"Evidência sanitizada: {evidence}")
        return 0

    print(f"DISCARDED! Provedor: {result.provider}; motivo: {result.error}")
    print(f"Evidência sanitizada: {evidence}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
