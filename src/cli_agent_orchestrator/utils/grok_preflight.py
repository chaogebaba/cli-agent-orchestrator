"""F295 Half 2 — Grok relay preflight probe.

Proves the resolved model's relay route is reachable BEFORE any terminal
resources are allocated.  Runs only when the canonical config declares a
``base_url`` for the resolved model (D2); the official direct route makes zero
network calls.

The probe shape is keyed on ``api_backend`` (D3):
  - ``responses`` → POST {base_url}/responses
  - ``chat``      → POST {base_url}/chat/completions
  - unknown/absent → transport-only GET (any HTTP response passes)

Failure is fail-closed and structured (D4), mirroring
``NativeHomeIsolationUnavailable``.
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from typing import Any

import requests

from cli_agent_orchestrator.services.settings_service import (
    get_provider_defaults,
    get_provider_profile_defaults,
    resolve_provider_string_option,
)
from cli_agent_orchestrator.utils.provider_plane import provider_home

logger = logging.getLogger(__name__)

# D4: Structured failure, same shape as NativeHomeIsolationUnavailable.
_BODY_TAIL_LIMIT = 200


class RelayPreflightFailed(RuntimeError):
    """The grok relay failed the creation-time reachability probe (D4)."""

    code = "grok_relay_preflight_failed"

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"{self.code}: {detail}")


def _canonical_config_path() -> Path:
    """Return the canonical grok config path (same as grok_config_watcher)."""
    return provider_home("grok_cli").home / "config.toml"


def _resolve_model(agent_profile: str | None, model: str | None) -> str | None:
    """Resolve the model with the same precedence as _build_grok_command."""
    provider_defaults = get_provider_defaults("grok_cli")
    profile_name = agent_profile or ""
    profile_defaults = get_provider_profile_defaults(provider_defaults, profile_name)

    # Explicit model override takes precedence
    if model and isinstance(model, str):
        return model

    # TOML precedence: profile_defaults → provider_defaults → CAO profile attr
    resolved = resolve_provider_string_option(
        profile_defaults, provider_defaults, None, "model", "model"
    )
    return resolved


def _read_model_table(config_path: Path, model_name: str) -> dict[str, Any] | None:
    """Read [model.<name>] table from the canonical config."""
    if not config_path.exists():
        return None
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    models = data.get("model")
    if not isinstance(models, dict):
        return None
    table = models.get(model_name)
    return dict(table) if isinstance(table, dict) else None


def _redact_key(text: str, api_key: str | None) -> str:
    """Strip the api key from text (D6)."""
    if not api_key or not text:
        return text
    return text.replace(api_key, "[REDACTED]")


def _failure_detail(
    endpoint: str,
    model: str,
    kind: str,
    body_tail: str | None,
    api_key: str | None,
) -> str:
    """Compose the detail string for RelayPreflightFailed (D4)."""
    parts = [f"endpoint={endpoint}", f"model={model}", f"failure={kind}"]
    if body_tail:
        safe_tail = _redact_key(body_tail[:_BODY_TAIL_LIMIT], api_key)
        parts.append(f"body_tail={safe_tail}")
    return "; ".join(parts)


def _probe_responses(base_url: str, model: str, api_key: str | None, timeout: float) -> None:
    """POST /responses probe (api_backend=responses)."""
    url = f"{base_url.rstrip('/')}/responses"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {"model": model, "input": "ping", "max_output_tokens": 16, "stream": False}
    resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    if resp.status_code >= 500:
        raise RelayPreflightFailed(
            _failure_detail(url, model, f"http_{resp.status_code}", resp.text, api_key)
        )
    # 2xx-4xx: relay is alive and routing (4xx means bad auth/shape, not dead)


def _probe_chat(base_url: str, model: str, api_key: str | None, timeout: float) -> None:
    """POST /chat/completions probe (api_backend=chat)."""
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 16,
        "stream": False,
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    if resp.status_code >= 500:
        raise RelayPreflightFailed(
            _failure_detail(url, model, f"http_{resp.status_code}", resp.text, api_key)
        )


def _probe_transport(base_url: str, model: str, api_key: str | None, timeout: float) -> None:
    """Transport-only GET probe (unknown api_backend). Any HTTP response passes (D3)."""
    url = base_url.rstrip("/")
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    # Any complete HTTP response (of any status) passes — only connection failures fail.
    requests.get(url, headers=headers, timeout=timeout)


def run_preflight(
    *,
    agent_profile: str | None,
    model: str | None,
    timeout: float = 20.0,
) -> None:
    """Run the relay preflight probe (D1-D6).

    Raises ``RelayPreflightFailed`` on a provably-dead relay.
    Returns ``None`` silently when the route is healthy or when no probe is needed.
    """
    from cli_agent_orchestrator.utils.sandbox_guard import is_sandbox

    # AC5(b): skip in sandbox
    if is_sandbox():
        return

    # AC5(a): check the escape knob
    provider_defaults = get_provider_defaults("grok_cli")
    if not provider_defaults.get("relay_preflight", True):
        return

    timeout_s = float(provider_defaults.get("relay_preflight_timeout_s", timeout))
    if timeout_s <= 0:
        timeout_s = timeout

    resolved_model = _resolve_model(agent_profile, model)
    if not resolved_model:
        return  # cannot probe without a model name

    config_path = _canonical_config_path()
    model_table = _read_model_table(config_path, resolved_model)
    if model_table is None:
        return  # no model table → no base_url → official route (D2)

    base_url = model_table.get("base_url")
    if not isinstance(base_url, str) or not base_url:
        return  # no relay → official direct route, zero network calls (D2/AC2)

    api_key = model_table.get("api_key")
    if not isinstance(api_key, str):
        api_key = None

    api_backend = model_table.get("api_backend")

    try:
        if api_backend == "responses":
            _probe_responses(base_url, resolved_model, api_key, timeout_s)
        elif api_backend in ("chat", "openai"):
            _probe_chat(base_url, resolved_model, api_key, timeout_s)
        else:
            _probe_transport(base_url, resolved_model, api_key, timeout_s)
    except RelayPreflightFailed:
        raise
    except requests.exceptions.SSLError as exc:
        raise RelayPreflightFailed(
            _failure_detail(base_url, resolved_model, "tls", str(exc)[:_BODY_TAIL_LIMIT], api_key)
        ) from exc
    except requests.ConnectionError as exc:
        # DNS failures also arrive as ConnectionError in requests
        exc_str = str(exc).lower()
        kind = "dns" if "name or service not known" in exc_str or "nodename" in exc_str else "connect_refused"
        raise RelayPreflightFailed(
            _failure_detail(base_url, resolved_model, kind, str(exc)[:_BODY_TAIL_LIMIT], api_key)
        ) from exc
    except requests.Timeout as exc:
        raise RelayPreflightFailed(
            _failure_detail(base_url, resolved_model, "timeout", None, api_key)
        ) from exc
    except requests.RequestException as exc:
        raise RelayPreflightFailed(
            _failure_detail(base_url, resolved_model, "connect_refused", str(exc)[:_BODY_TAIL_LIMIT], api_key)
        ) from exc
