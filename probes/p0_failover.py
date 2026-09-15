#!/usr/bin/env python3
"""P0: preserve subscription auth while failing over a quota-blocked request."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Sequence

import p0_auth


class AuthStatus(str, Enum):
    PASS = "PASS"
    NOT_AUTHENTICATED = "NOT_AUTHENTICATED"
    UNSUPPORTED = "UNSUPPORTED"


class Availability(str, Enum):
    AVAILABLE = "AVAILABLE"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    TEMPORARILY_UNAVAILABLE = "TEMPORARILY_UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


class RequestStatus(str, Enum):
    PASS = "PASS"
    BLOCKED_BY_QUOTA = "BLOCKED_BY_QUOTA"
    FAILED = "FAILED"
    NOT_ATTEMPTED = "NOT_ATTEMPTED"


ROUTES = (
    ("Anthropic", "velina"),
    ("Anthropic", "amara"),
    ("OpenAI", "velina"),
    ("OpenAI", "amara"),
)
SECRET = re.compile(
    r"(?i)(sk-[A-Za-z0-9_-]+|bearer\s+\S+|api[_ -]?key\s*[:=]\s*\S+|"
    r"token\s*[:=]\s*\S+|cookie\s*[:=]\s*\S+)"
)
QUOTA_MESSAGE = re.compile(
    r"(?i)(?:quota|usage limit|weekly limit|monthly limit|daily limit|"
    r"you've hit .* limit)"
)


@dataclass
class Attempt:
    provider: str
    account: str
    marker: str
    auth: AuthStatus = AuthStatus.UNSUPPORTED
    availability: Availability = Availability.UNKNOWN
    request: RequestStatus = RequestStatus.NOT_ATTEMPTED
    wrapper: str = "UNRESOLVED"
    exit_code: int | None = None
    category: str = "NOT_ATTEMPTED"
    message: str | None = None
    model: str = "UNAVAILABLE"


@dataclass
class FailoverResult:
    probe_id: str
    marker: str
    source: Attempt
    target: Attempt | None = None
    trigger: str = "NOT_TRIGGERED"
    status: str = "P0_FAILOVER_PARTIAL"


def sanitize_message(stdout: str, stderr: str) -> str:
    """Keep only provider error facts needed to audit classification."""
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        parts = [
            f"api_error_status={payload['api_error_status']}"
            if payload.get("api_error_status") is not None
            else "",
            f"terminal_reason={payload['terminal_reason']}"
            if payload.get("terminal_reason")
            else "",
            str(payload.get("result") or ""),
        ]
        text = "; ".join(part for part in parts if part)
    else:
        text = " ".join(part.strip() for part in (stdout, stderr) if part.strip())
    return SECRET.sub("[REDACTED]", " ".join(text.split()))[:500]


def classify_failure(exit_code: int | None, message: str, *, timed_out: bool = False) -> tuple[Availability, RequestStatus, str]:
    if QUOTA_MESSAGE.search(message):
        return Availability.QUOTA_EXHAUSTED, RequestStatus.BLOCKED_BY_QUOTA, "QUOTA_EVIDENCE"
    if timed_out:
        return Availability.TEMPORARILY_UNAVAILABLE, RequestStatus.FAILED, "TIMEOUT"
    return Availability.UNKNOWN, RequestStatus.FAILED, f"EXIT_{exit_code if exit_code is not None else 'UNKNOWN'}"


def choose_same_provider(
    provider: str, source_account: str, routes: Sequence[tuple[str, str]] = ROUTES
) -> tuple[str, str] | None:
    return next(
        ((candidate_provider, account) for candidate_provider, account in routes
         if candidate_provider == provider and account != source_account),
        None,
    )


def request_claude(claude: str, marker: str, env: dict[str, str]) -> tuple[int | None, str, str, bool]:
    with tempfile.TemporaryDirectory(prefix="pinker-harness-p0-") as cwd:
        try:
            cp = subprocess.run(
                [claude, "-p", f"Respond exactly with {marker}. Do not use tools.", "--output-format", "json"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=cwd,
                timeout=180,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            return None, error.stdout or "", error.stderr or "", True
        except OSError as error:
            return None, "", str(error), False
    return cp.returncode, cp.stdout, cp.stderr, False


def attempt_anthropic(account: str, marker: str) -> Attempt:
    attempt = Attempt(provider="Anthropic", account=account, marker=marker)
    try:
        route = p0_auth.provider_route(account)
    except RuntimeError as error:
        attempt.category = "UNAUTHORIZED_ROUTE"
        attempt.message = str(error)
        return attempt

    claude = route["user_scoped_claude"]
    attempt.wrapper = claude
    if shutil.which(claude) is None:
        attempt.category = "USER_SCOPED_WRAPPER_MISSING"
        attempt.message = f"{claude} not found on PATH"
        return attempt

    env = p0_auth.anthropic_subscription_env()
    if not p0_auth.claude_status(claude, env):
        attempt.auth = AuthStatus.NOT_AUTHENTICATED
        attempt.category = "SUBSCRIPTION_LOGIN_NOT_CONFIRMED"
        return attempt

    attempt.auth = AuthStatus.PASS
    exit_code, stdout, stderr, timed_out = request_claude(claude, marker, env)
    attempt.exit_code = exit_code
    attempt.message = sanitize_message(stdout, stderr)
    if exit_code == 0 and marker in stdout:
        attempt.availability = Availability.AVAILABLE
        attempt.request = RequestStatus.PASS
        attempt.category = "REQUEST_VERIFIED"
        try:
            attempt.model = p0_auth.find_model(json.loads(stdout)) or "UNAVAILABLE"
        except json.JSONDecodeError:
            pass
        return attempt

    attempt.availability, attempt.request, attempt.category = classify_failure(
        exit_code, attempt.message or "", timed_out=timed_out
    )
    return attempt


def run_probe() -> FailoverResult:
    probe_id = uuid.uuid4().hex[:12]
    marker = f"PINKER_FAILOVER_OK_{probe_id}"
    source = attempt_anthropic("velina", marker)
    result = FailoverResult(probe_id=probe_id, marker=marker, source=source)
    if source.auth != AuthStatus.PASS or source.availability != Availability.QUOTA_EXHAUSTED:
        result.trigger = "LIVE_QUOTA_EXHAUSTION_NOT_REPRODUCIBLE"
        return result

    target_route = choose_same_provider(source.provider, source.account)
    if target_route is None:
        result.trigger = "NO_SAME_PROVIDER_TARGET"
        return result
    result.trigger = "QUOTA_EXHAUSTED"
    _, target_account = target_route
    result.target = attempt_anthropic(target_account, marker)
    if result.target.request == RequestStatus.PASS:
        result.status = "P0_FAILOVER_PASS"
    return result


def evidence(result: FailoverResult) -> dict[str, object]:
    def serialize(attempt: Attempt | None) -> dict[str, object] | None:
        if attempt is None:
            return None
        return {key: value.value if isinstance(value, Enum) else value for key, value in asdict(attempt).items()}

    return {
        "probe_id": result.probe_id,
        "marker": result.marker,
        "source": serialize(result.source),
        "target": serialize(result.target),
        "failover_trigger": result.trigger,
        "status": result.status,
    }


def save(result: FailoverResult) -> Path:
    path = p0_auth.artifact_root() / f"pinker-harness-p0-failover-{result.probe_id}.json"
    path.write_text(json.dumps(evidence(result), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def show(label: str, attempt: Attempt) -> None:
    print(f"{label}: {attempt.account}/{attempt.provider}")
    print(f"  Auth: {attempt.auth.value}")
    print(f"  Availability: {attempt.availability.value}")
    print(f"  Request: {attempt.request.value}")
    print(f"  Exit: {attempt.exit_code}; category: {attempt.category}")


def main() -> int:
    result = run_probe()
    artifact = save(result)
    print("P0 — failover por quota")
    print(f"Probe: {result.probe_id}")
    show("Tentativa 1", result.source)
    if result.target:
        print(f"Failover: {result.source.account}/{result.source.provider} -> {result.target.account}/{result.target.provider}")
        show("Tentativa 2", result.target)
    print(f"FAILOVER = {result.status}")
    print(f"Evidência sanitizada: {artifact}")
    return 0 if result.status == "P0_FAILOVER_PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
