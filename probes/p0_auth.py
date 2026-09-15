#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

MARKER = "PINKER_AUTH_OK"
DEVELOPMENT_USERS = frozenset({"amara", "velina"})


@dataclass
class Result:
    provider: str
    task_os_user: str = "UNRESOLVED"
    user_scoped_gh: str = "UNRESOLVED"
    user_scoped_codex: str = "UNRESOLVED"
    user_scoped_claude: str = "UNRESOLVED"
    codex_login_status: str = "NOT_CHECKED"
    cli_version: str = "UNKNOWN"
    existing_auth_reused: bool = False
    subscription_login: str = "NOT_AUTHENTICATED"
    availability: str = "UNKNOWN"
    request_status: str = "NOT_ATTEMPTED"
    request_verified: bool = False
    model: str = "UNAVAILABLE"
    quota: str = "UNSUPPORTED"
    quota_detail: list[dict[str, Any]] | None = None
    error: str | None = None


def provider_route(user: str | None = None) -> dict[str, str]:
    task_os_user = user or pwd.getpwuid(os.getuid()).pw_name
    if task_os_user not in DEVELOPMENT_USERS:
        raise RuntimeError(
            "BLOCK: usuário "
            f"'{task_os_user}' não é participante autorizado do desenvolvimento Pinker"
        )
    return {
        "task_os_user": task_os_user,
        "user_scoped_gh": f"gh-{task_os_user}",
        "user_scoped_codex": f"codex-{task_os_user}",
        "user_scoped_claude": f"claude-{task_os_user}",
    }


def artifact_root(
    *, environment: dict[str, str] | None = None, checkout: Path | None = None
) -> Path:
    """Return an external runtime-evidence root; never fall back into Git."""
    environment = os.environ if environment is None else environment
    checkout = (checkout or Path(__file__).resolve().parents[1]).resolve()
    root = Path(
        environment.get(
            "PINKER_HARNESS_ARTIFACT_ROOT",
            str(Path(tempfile.gettempdir()) / "pinker-harness-artifacts"),
        )
    ).expanduser().resolve()
    try:
        root.relative_to(checkout)
    except ValueError:
        root.mkdir(parents=True, exist_ok=True)
        return root
    raise RuntimeError("RUNTIME_EVIDENCE_MUST_NOT_FALL_BACK_TO_REPOSITORY")


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


def verify_codex(codex: str) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory(prefix="pinker-harness-p0-") as cwd:
        cp = run(
            [
                codex,
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


def verify_claude(claude: str, env: dict[str, str]) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory(prefix="pinker-harness-p0-") as cwd:
        cp = run(
            [
                claude,
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


def codex_quota(codex: str) -> tuple[str, list[dict[str, Any]] | None]:
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
            [codex, "app-server", "--stdio"],
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


def codex_login_status(codex: str) -> str:
    if shutil.which(codex) is None:
        return "USER_SCOPED_WRAPPER_MISSING"
    status = run([codex, "login", "status"], timeout=30)
    chatgpt = status.returncode == 0 and "chatgpt" in (
        f"{status.stdout}\n{status.stderr}".casefold()
    )
    return "PASS" if chatgpt else "USER_SCOPED_AUTH_FAILURE"


def routed_result(provider: str) -> Result:
    out = Result(provider=provider)
    try:
        route = provider_route()
    except RuntimeError as error:
        out.error = str(error)
        return out
    for field, value in route.items():
        setattr(out, field, value)
    out.codex_login_status = codex_login_status(out.user_scoped_codex)
    return out


def probe_openai() -> Result:
    out = routed_result("OpenAI")
    if out.error:
        return out
    codex = out.user_scoped_codex
    if out.codex_login_status == "USER_SCOPED_WRAPPER_MISSING":
        out.error = f"USER_SCOPED_WRAPPER_MISSING: {codex} não encontrado no PATH"
        return out

    out.cli_version = version(codex)
    if out.codex_login_status == "PASS":
        out.existing_auth_reused = True
    else:
        print("Abrindo login oficial do ChatGPT/Codex...")
        if subprocess.run([codex, "login"], check=False).returncode != 0:
            out.error = "USER_SCOPED_AUTH_FAILURE: login oficial do Codex falhou"
            return out
        out.codex_login_status = codex_login_status(codex)
        if out.codex_login_status != "PASS":
            out.error = "USER_SCOPED_AUTH_FAILURE: Codex não confirmou autenticação via ChatGPT"
            return out

    out.subscription_login = "PASS"
    ok, out.model = verify_codex(codex)
    if not ok:
        out.request_status = "FAILED"
        out.error = "login existe, mas a chamada mínima falhou"
        return out

    out.availability = "AVAILABLE"
    out.request_status = "PASS"
    out.request_verified = True
    out.quota, out.quota_detail = codex_quota(codex)
    return out


def claude_status(claude: str, env: dict[str, str]) -> bool:
    cp = run([claude, "auth", "status"], env=env, timeout=30)
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
    out = routed_result("Anthropic")
    if out.error:
        return out
    claude = out.user_scoped_claude
    if shutil.which(claude) is None:
        out.error = f"USER_SCOPED_WRAPPER_MISSING: {claude} não encontrado no PATH"
        return out

    out.cli_version = version(claude)
    env = anthropic_subscription_env()

    if claude_status(claude, env):
        out.existing_auth_reused = True
    else:
        print("Abrindo login oficial da assinatura Claude...")
        if subprocess.run(
            [claude, "auth", "login"], env=env, check=False
        ).returncode != 0:
            out.error = "USER_SCOPED_AUTH_FAILURE: login oficial do Claude Code falhou"
            return out
        if not claude_status(claude, env):
            out.error = "USER_SCOPED_AUTH_FAILURE: Claude Code não confirmou autenticação de assinatura"
            return out

    out.subscription_login = "PASS"
    ok, out.model = verify_claude(claude, env)
    if not ok:
        out.request_status = "FAILED"
        out.error = "login existe, mas a chamada mínima falhou"
        return out

    out.availability = "AVAILABLE"
    out.request_status = "PASS"
    out.request_verified = True
    out.quota = "UNSUPPORTED"
    return out


def save(result: Result) -> Path:
    root = artifact_root()
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

    if result.subscription_login == "PASS" and result.request_verified:
        print(
            "Conta logada com sucesso! "
            f"Provedor: {result.provider}; "
            f"modelo: {result.model}; quota: {result.quota}"
        )
        print(f"Evidência sanitizada: {evidence}")
        return 0

    if result.subscription_login == "PASS":
        print(
            "Login preservado; chamada não verificada. "
            f"Provedor: {result.provider}; "
            f"availability: {result.availability}; "
            f"request: {result.request_status}; motivo: {result.error}"
        )
        print(f"Evidência sanitizada: {evidence}")
        return 1

    print(f"Autenticação não confirmada. Provedor: {result.provider}; motivo: {result.error}")
    print(f"Evidência sanitizada: {evidence}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
