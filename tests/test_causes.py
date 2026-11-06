"""Real-cause rule book: recorded failure shapes -> plain cause + fix."""

import importlib

causes = importlib.import_module("hermes_rescue.causes")
E = causes.ErrorEvent


def ev(**kw):
    return causes.ErrorEvent.from_hook(**kw)


def test_from_hook_reads_the_api_request_error_payload():
    e = ev(provider="openrouter", model="anthropic/claude-sonnet-4.6", base_url="https://openrouter.ai/api/v1",
           status_code=401, reason="auth", retryable=False,
           error={"type": "AuthenticationError", "message": "invalid_api_key"})
    assert e.status_code == 401 and e.reason == "auth" and "invalid_api_key" in e.message
    assert e.retryable is False


def test_local_model_not_loaded_on_ollama():
    c = causes.classify(E(provider="custom", model="llama3.1:8b", base_url="http://localhost:11434/v1",
                          status_code=404, reason="model_not_found", message="model 'llama3.1:8b' not found"))
    assert c.rule == "local_model_missing"
    assert "localhost:11434" in c.what and "ollama pull llama3.1:8b" in c.fix
    assert not c.transient


def test_local_server_down():
    c = causes.classify(E(provider="ollama", model="qwen3", base_url="http://127.0.0.1:11434",
                          reason="timeout", message="ConnectError: [Errno 111] Connection refused"))
    assert c.rule == "local_server_down" and "ollama serve" in c.fix


def test_local_context_window_too_small():
    c = causes.classify(E(provider="custom", model="gemma-27b", base_url="http://192.168.1.20:8080/v1",
                          status_code=400, reason="format_error",
                          message="the request exceeds the available context size (n_ctx 8192)"))
    assert c.rule == "local_context_too_small" and "/compress" in c.fix


def test_remote_key_rejected_names_provider_and_command():
    c = causes.classify(E(provider="openrouter", model="x/y", base_url="https://openrouter.ai/api/v1",
                          status_code=401, reason="auth"))
    assert c.rule == "key_rejected"
    assert "OpenRouter" in c.what and "hermes auth add openrouter" in c.fix


def test_out_of_credit():
    c = causes.classify(E(provider="openrouter", status_code=402, reason="billing", message="Insufficient credits"))
    assert c.rule == "out_of_credit" and not c.transient


def test_rate_limit_is_transient():
    c = causes.classify(E(provider="zai", status_code=429, reason="rate_limit"))
    assert c.rule == "rate_limited" and c.transient and "Z.ai" in c.what


def test_remote_model_missing():
    c = causes.classify(E(provider="openrouter", model="old/model-v1", status_code=404, reason="model_not_found"))
    assert c.rule == "model_missing" and "model-v1" in c.what


def test_unknown_error_falls_back_to_doctor():
    c = causes.classify(E(provider="mystery", status_code=418, message="I'm a teapot"))
    assert c.rule == "unknown" and "hermes doctor" in c.fix


def test_fallback_chain_401_then_404_is_explained_in_order():
    """#147620 CLI case: 'Your request was not processed' hid a fallback 401 then 404."""
    errors = [
        E(provider="openrouter", model="anthropic/claude-sonnet-4.6", base_url="https://openrouter.ai/api/v1",
          status_code=429, reason="rate_limit"),
        E(provider="openrouter", model="anthropic/claude-sonnet-4.6", base_url="https://openrouter.ai/api/v1",
          status_code=429, reason="rate_limit"),
        E(provider="openai", model="gpt-x", base_url="https://api.openai.com/v1", status_code=401, reason="auth"),
        E(provider="custom", model="llama3", base_url="http://localhost:11434/v1", status_code=404,
          reason="model_not_found", message="model not found"),
    ]
    x = causes.explain(errors, recovered=False)
    assert x.kind == "failed"
    assert [a[1].rule for a in x.attempts] == ["rate_limited", "key_rejected", "local_model_missing"]
    assert x.attempts[0][2] == 2  # repeated retries collapse into one line
    assert x.attempts[1][0].endswith("(backup)")
    assert x.headline.rule == "key_rejected"  # first durable problem, not the transient one
    text = x.render()
    assert "what actually happened" in text and "×2" in text and "Fix:" in text


def test_recovered_after_transient_errors_says_nothing():
    errors = [E(provider="openrouter", status_code=429, reason="rate_limit")]
    assert causes.explain(errors, recovered=True) is None


def test_recovered_but_main_key_dead_gives_a_heads_up():
    errors = [E(provider="anthropic", model="claude-x", status_code=401, reason="auth")]
    x = causes.explain(errors, recovered=True)
    assert x.kind == "degraded"
    assert "came from a backup" in x.render() and "hermes auth add anthropic" in x.render()


def test_no_errors_no_explanation():
    assert causes.explain([], recovered=False) is None


def test_is_local():
    assert causes.is_local("http://localhost:1234/v1")
    assert causes.is_local("http://10.0.0.5:8080")
    assert causes.is_local("http://my-box.local:11434")
    assert not causes.is_local("https://api.openai.com/v1")
    assert not causes.is_local("")


# ---- review regressions: Hermes's own reason beats a status code or a loose word ----------------

def _ev(**kw):
    return causes.ErrorEvent(**{"provider": "openrouter", "model": "m", **kw})


def test_load_balancer_timeout_is_not_out_of_credit():
    c = causes.classify(_ev(status_code=502, reason="server_error", message="502 Bad Gateway: load balancer timeout"))
    assert c.rule == "provider_trouble"


def test_a_403_that_hermes_calls_billing_is_about_credit_not_keys():
    assert causes.classify(_ev(status_code=403, reason="billing")).rule == "out_of_credit"


def test_a_403_model_entitlement_is_about_the_plan():
    assert causes.classify(_ev(status_code=403, reason="model_entitlement")).rule == "model_missing"


def test_a_403_content_policy_is_the_safety_filter():
    assert causes.classify(_ev(status_code=403, reason="content_policy_blocked")).rule == "safety_filter"


def test_privacy_settings_block_is_named():
    c = causes.classify(_ev(status_code=404, reason="provider_policy_blocked"))
    assert c.rule == "provider_policy" and "privacy" in c.fix


def test_a_plain_403_is_still_a_key_problem():
    assert causes.classify(_ev(status_code=403, reason="")).rule == "key_rejected"


def test_credits_resetting_in_a_rate_limit_is_not_out_of_credit():
    c = causes.classify(_ev(status_code=429, reason="", message="Rate limit reached; your credits reset in 1 minute"))
    assert c.rule == "rate_limited"
