"""Real-cause rule book: turn the failed model calls of one turn into plain words and one fix.

Hermes already writes good copy for the LAST failure of a turn. What it does not show is the
whole chain (the main model failed, then the backup failed differently) or the specific cause
behind a generic notice (a local server that has not loaded the model, a backup key that is
dead while answers keep arriving from somewhere else). See NousResearch/hermes-agent#147620.

Everything here is pure and deterministic: no network, no model calls, so it still works when
the model is the thing that is down.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urlparse

# Provider slugs as Hermes reports them -> the name people know.
_PROVIDER_LABELS = {
    "openrouter": "OpenRouter", "anthropic": "Anthropic", "openai": "OpenAI", "openai-codex": "OpenAI Codex",
    "nous": "Nous Portal", "ollama": "Ollama", "gemini": "Google Gemini", "google": "Google Gemini",
    "deepseek": "DeepSeek", "zai": "Z.ai", "glm": "Z.ai", "xai": "xAI", "groq": "Groq", "mistral": "Mistral",
    "lmstudio": "LM Studio", "llamacpp": "llama.cpp", "llama.cpp": "llama.cpp", "vllm": "vLLM",
    "together": "Together AI", "fireworks": "Fireworks", "kimi": "Kimi", "moonshot": "Moonshot",
    "minimax": "MiniMax", "qwen": "Qwen", "copilot": "GitHub Copilot", "huggingface": "Hugging Face",
    "custom": "your custom endpoint",
}

_LOCAL_HOSTS = {"localhost", "host.docker.internal", "0.0.0.0", "::1"}
_LOCAL_DEFAULT_PORTS = {11434: "Ollama", 1234: "LM Studio", 8080: "llama.cpp"}


@dataclass(frozen=True)
class ErrorEvent:
    """One failed provider attempt, as the ``api_request_error`` hook reports it."""

    provider: str = ""
    model: str = ""
    base_url: str = ""
    status_code: Optional[int] = None
    reason: str = ""
    message: str = ""
    retryable: Optional[bool] = None

    @classmethod
    def from_hook(cls, **kw) -> "ErrorEvent":
        err = kw.get("error") if isinstance(kw.get("error"), dict) else {}
        status = kw.get("status_code")
        try:
            status = int(status) if status not in (None, "") else None
        except (TypeError, ValueError):
            status = None
        message = str(err.get("message") or "")
        if err.get("type") and err.get("type") not in message:
            message = f"{err.get('type')}: {message}".strip(": ")
        return cls(provider=str(kw.get("provider") or ""), model=str(kw.get("model") or ""),
                   base_url=str(kw.get("base_url") or ""), status_code=status,
                   reason=str(kw.get("reason") or ""), message=message[:2000],
                   retryable=kw.get("retryable") if isinstance(kw.get("retryable"), bool) else None)

    @property
    def route(self) -> tuple[str, str, str]:
        return (self.provider, self.model, self.base_url)


@dataclass(frozen=True)
class Cause:
    """A classified failure: what happened (short and long) and what to do."""

    rule: str
    short: str            # fits in one list line: "key rejected (401)"
    what: str             # one sentence for a person
    fix: str              # one concrete next step
    transient: bool       # True when waiting / retrying is the fix


@dataclass
class Explanation:
    """What Rescue tells the user about one turn."""

    kind: str                       # "failed" | "degraded"
    attempts: list[tuple[str, Cause, int]] = field(default_factory=list)  # (route label, cause, repeats)
    headline: Cause | None = None   # the most actionable cause in the chain

    def render(self, *, standalone: bool = False) -> str:
        """Footer text for a reply, or (``standalone``) a message of its own."""
        if self.kind == "degraded" and self.headline is not None:
            first = self.attempts[0][0] if self.attempts else "your main model"
            text = ("🛟 Rescue · heads-up: this answer came from a backup because "
                    f"{first} failed — {self.headline.what} Fix: {self.headline.fix}")
            return text if standalone else "\n\n---\n" + text
        lines = [] if standalone else ["", "", "---"]
        lines.append("🛟 Rescue · what actually happened")
        for i, (label, cause, repeats) in enumerate(self.attempts, 1):
            times = f" ×{repeats}" if repeats > 1 else ""
            lines.append(f"{i}. {label} → {cause.short}{times}")
        if self.headline is not None:
            lines.append(f"Fix: {self.headline.fix}")
        lines.append("Send /rescue for details.")
        return "\n".join(lines)


# ---- helpers ---------------------------------------------------------------------------------

def provider_label(provider: str, base_url: str = "") -> str:
    slug = (provider or "").strip().lower()
    port_name = _local_port_name(base_url)
    if port_name and slug in ("", "custom", "openai", "local"):
        return port_name  # a local server behind a generic provider slug: name the server people run
    if slug in _PROVIDER_LABELS:
        return _PROVIDER_LABELS[slug]
    if port_name:
        return port_name
    host = urlparse(base_url).hostname if base_url else ""
    return provider or host or "the model service"


def _local_port_name(base_url: str) -> str:
    try:
        parsed = urlparse(base_url)
        return _LOCAL_DEFAULT_PORTS.get(parsed.port or 0, "") if is_local(base_url) else ""
    except ValueError:
        return ""


def is_local(base_url: str) -> bool:
    """True for a model server on this machine or the local network."""
    if not base_url:
        return False
    try:
        host = (urlparse(base_url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    if host in _LOCAL_HOSTS or host.endswith(".local") or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def host_of(base_url: str) -> str:
    try:
        parsed = urlparse(base_url)
    except ValueError:
        return base_url
    if not parsed.hostname:
        return base_url
    return f"{parsed.hostname}:{parsed.port}" if parsed.port else parsed.hostname


def _short_model(model: str) -> str:
    return model.rsplit("/", 1)[-1] if model else "the model"


def _ollama_hint(e: ErrorEvent, text: str) -> str:
    return text if "ollama" in (e.provider + e.base_url).lower() or _local_port_name(e.base_url) == "Ollama" else ""


def _msg(e: ErrorEvent) -> str:
    return e.message.lower()


def _has(e: ErrorEvent, *needles: str) -> bool:
    m = _msg(e)
    return any(n in m for n in needles)


def _status(e: ErrorEvent, *codes: int) -> bool:
    return e.status_code in codes


def _reason(e: ErrorEvent, *reasons: str) -> bool:
    return e.reason in reasons


_CONNECT_WORDS = ("connection refused", "connecterror", "failed to establish", "connection error",
                  "all connection attempts failed", "name or service not known", "nodename nor servname",
                  "cannot connect", "connection reset", "errno 111", "errno 61")
_CONTEXT_WORDS = ("n_ctx", "context size", "context length", "context window", "maximum context",
                  "exceeds the available context", "too many tokens", "prompt is too long",
                  "input is too long", "num_ctx")
_MODEL_MISSING_WORDS = ("model not found", "not found", "does not exist", "no such model", "not loaded",
                        "unknown model", "invalid model", "model_not_found", "is not a valid model")
_CREDIT_WORDS = ("insufficient credit", "insufficient_quota", "insufficient balance", "out of credit",
                 "payment required", "billing", "insufficient credits", "out of credits", "no credits",
                 "exceeded your current quota", "spend limit",
                 "spending limit", "account balance", "credit balance")


# ---- the rule book -----------------------------------------------------------------------------
# Order matters: specific local-server rules first, then account problems, then transient noise.

@dataclass(frozen=True)
class Rule:
    id: str
    matches: Callable[[ErrorEvent], bool]
    build: Callable[[ErrorEvent, str], Cause]


def _local_model_missing(e: ErrorEvent) -> bool:
    return is_local(e.base_url) and (_status(e, 404) or _reason(e, "model_not_found")
                                     or _has(e, *_MODEL_MISSING_WORDS) and "model" in _msg(e))


def _local_down(e: ErrorEvent) -> bool:
    return is_local(e.base_url) and (_has(e, *_CONNECT_WORDS) or (_reason(e, "timeout") and e.status_code is None))


def _local_context(e: ErrorEvent) -> bool:
    return is_local(e.base_url) and (_reason(e, "context_overflow", "payload_too_large") or _has(e, *_CONTEXT_WORDS))


RULES: tuple[Rule, ...] = (
    Rule("local_model_missing", _local_model_missing, lambda e, who: Cause(
        "local_model_missing", f"model not loaded ({e.status_code or 404})",
        f"The model server at {host_of(e.base_url)} doesn't have “{_short_model(e.model)}” available.",
        "Load it on that server"
        + (_ollama_hint(e, f" (`ollama pull {_short_model(e.model)}`)") or "")
        + ", or pick a model it has with /model.", False)),
    Rule("local_server_down", _local_down, lambda e, who: Cause(
        "local_server_down", "server not reachable",
        f"Hermes couldn't reach the model server at {host_of(e.base_url)}.",
        "Start it" + (_ollama_hint(e, " (`ollama serve`)") or "")
        + ", check the address in /model, then send /retry.", False)),
    Rule("local_context_too_small", _local_context, lambda e, who: Cause(
        "local_context_too_small", "conversation too big for the server",
        f"This conversation is bigger than the context window the server at {host_of(e.base_url)} was started with.",
        "Run /compress or start fresh with /new, or restart the server with a bigger context"
        + (_ollama_hint(e, " (Ollama: raise `num_ctx`)") or " (llama.cpp: `-c`)") + ".", False)),
    Rule("key_rejected",
         lambda e: _reason(e, "auth", "auth_permanent") or (_status(e, 401, 403) and e.reason in ("", "unknown")),
         lambda e, who: Cause(
             "key_rejected", f"key rejected ({e.status_code or 401})",
             f"{who} rejected the key or sign-in Hermes used for it.",
             ("Update the endpoint's key with `hermes model` (Desktop: Settings → Providers)"
              if (e.provider or "custom").lower() in ("custom", "local", "")
              else f"Sign in again with `hermes auth add {e.provider}`")
             + ", or remove it from your backups with `hermes fallback remove`.", False)),
    Rule("out_of_credit",
         lambda e: _reason(e, "billing") or _status(e, 402) or (
             _has(e, *_CREDIT_WORDS) and not _reason(e, "rate_limit") and not _status(e, 429)),
         lambda e, who: Cause(
             "out_of_credit", f"out of credit ({e.status_code or 402})",
             f"{who} says the account is out of credit or over its spending limit.",
             f"Top up on {who}'s site, or switch to another provider with /model.", False)),
    Rule("model_missing",
         lambda e: _reason(e, "model_not_found", "model_entitlement") or (_status(e, 404) and _has(e, "model")),
         lambda e, who: Cause(
             "model_missing", f"model not available ({e.status_code or 404})",
             f"{who} doesn't offer “{_short_model(e.model)}” to this account (it may be retired, misspelled or not on your plan).",
             "Pick another model with /model, and check your backups with `hermes fallback list`.", False)),
    Rule("context_too_long",
         lambda e: _reason(e, "context_overflow", "payload_too_large") or _status(e, 413) or _has(e, *_CONTEXT_WORDS),
         lambda e, who: Cause(
             "context_too_long", "conversation too long",
             f"The conversation got too long for {_short_model(e.model)}.",
             "Run /compress, or start fresh with /new (your history is kept).", False)),
    Rule("safety_filter", lambda e: _reason(e, "content_policy_blocked"), lambda e, who: Cause(
        "safety_filter", "refused by safety filter",
        f"{who}'s safety filter refused the request.",
        "Reword the message or remove the attachment, or try another model with /model.", False)),
    Rule("provider_policy", lambda e: _reason(e, "provider_policy_blocked", "data_policy_denied"),
         lambda e, who: Cause(
             "provider_policy", "blocked by your account's privacy settings",
             f"{who}'s privacy or data settings for this account rule out every endpoint that serves "
             f"{_short_model(e.model)}.",
             f"Change the privacy/data setting on {who}'s site, or pick another model with /model.", False)),
    Rule("firewall_blocked", lambda e: _reason(e, "upstream_blocked"), lambda e, who: Cause(
        "firewall_blocked", "blocked by a firewall",
        f"A firewall or proxy in front of {who} blocked the request; your key is probably fine.",
        "Try another network or provider with /model.", False)),
    Rule("certificate", lambda e: _reason(e, "ssl_cert_verification") or _has(e, "certificate verify failed"),
         lambda e, who: Cause(
             "certificate", "certificate check failed",
             f"Hermes couldn't verify {who}'s security certificate (often a company proxy or an old certificate store).",
             "Try another network, or run `hermes doctor` for the exact fix.", False)),
    Rule("rate_limited", lambda e: _reason(e, "rate_limit", "upstream_rate_limit") or _status(e, 429),
         lambda e, who: Cause(
             "rate_limited", f"rate-limited ({e.status_code or 429})",
             f"{who} is rate-limiting requests right now.",
             "Wait a minute and send /retry, or add a backup with `hermes fallback add`.", True)),
    Rule("provider_trouble",
         lambda e: _reason(e, "overloaded", "server_error") or (e.status_code or 0) >= 500,
         lambda e, who: Cause(
             "provider_trouble", f"service error ({e.status_code or '5xx'})",
             f"{who} is having problems on its side.",
             "Send /retry in a few minutes, or switch models with /model.", True)),
    Rule("timeout", lambda e: _reason(e, "timeout") or _has(e, "timed out", "timeout", *_CONNECT_WORDS),
         lambda e, who: Cause(
             "timeout", "no answer in time",
             f"{who} didn't answer in time.",
             "Check your internet connection, then send /retry.", True)),
)

_FALLBACK_RULE = Rule("unknown", lambda e: True, lambda e, who: Cause(
    "unknown", f"error{f' ({e.status_code})' if e.status_code else ''}",
    f"{who} returned an error Hermes didn't recognise.",
    "Run `hermes doctor`; if it keeps happening, run `hermes debug share` and send the link to support.", True))


# Hermes's own classification of an error (``reason``) is more specific than its status code, so
# a rule keyed on the reason goes first: a 403 that Hermes calls "billing" is about credit, not keys.
_RULE_REASONS: dict[str, tuple[str, ...]] = {
    "local_model_missing": ("model_not_found",),
    "local_server_down": ("timeout",),
    "local_context_too_small": ("context_overflow", "payload_too_large"),
    "key_rejected": ("auth", "auth_permanent"),
    "out_of_credit": ("billing",),
    "model_missing": ("model_not_found", "model_entitlement"),
    "context_too_long": ("context_overflow", "payload_too_large"),
    "safety_filter": ("content_policy_blocked",),
    "provider_policy": ("provider_policy_blocked", "data_policy_denied"),
    "firewall_blocked": ("upstream_blocked",),
    "certificate": ("ssl_cert_verification",),
    "rate_limited": ("rate_limit", "upstream_rate_limit"),
    "provider_trouble": ("overloaded", "server_error"),
    "timeout": ("timeout",),
}


def classify(event: ErrorEvent) -> Cause:
    who = provider_label(event.provider, event.base_url)
    passes = [[r for r in RULES if event.reason in _RULE_REASONS.get(r.id, ())]] if event.reason else []
    passes.append(list(RULES))
    for rules in passes:
        for rule in rules:
            try:
                if rule.matches(event):
                    return rule.build(event, who)
            except Exception:  # a rule bug must never hide the turn's reply
                continue
    return _FALLBACK_RULE.build(event, who)


def _route_label(event: ErrorEvent, first_route: tuple[str, str, str]) -> str:
    who = provider_label(event.provider, event.base_url)
    label = f"{who} · {_short_model(event.model)}" if event.model else who
    return label if event.route == first_route else f"{label} (backup)"


def explain(errors: list[ErrorEvent], *, recovered: bool) -> Optional[Explanation]:
    """Explain one turn's failed attempts, or None when there is nothing worth saying.

    ``recovered`` is True when a later attempt in the same turn succeeded. A recovered turn is
    only worth a note when something durable is broken (a dead key, a missing model, a local
    server that is down) — a rate limit that retried fine is noise.
    """
    if not errors:
        return None
    first = errors[0].route
    attempts: list[tuple[str, Cause, int]] = []
    for event in errors:
        label, cause = _route_label(event, first), classify(event)
        if attempts and attempts[-1][0] == label and attempts[-1][1].rule == cause.rule:
            attempts[-1] = (label, attempts[-1][1], attempts[-1][2] + 1)
        else:
            attempts.append((label, cause, 1))
    durable = [c for _l, c, _n in attempts if not c.transient]
    headline = durable[0] if durable else attempts[-1][1]
    if recovered:
        if not durable:
            return None
        return Explanation("degraded", attempts, headline)
    return Explanation("failed", attempts, headline)
