"""tnb: a fallback to a custom endpoint keeps that endpoint's TLS settings.

try_activate_fallback rebuilt agent._client_kwargs from the fallback client's
key/base_url/headers only, dropping custom_providers ssl_verify/ssl_ca_cert, so
the streaming request client verified an internal endpoint against the default
CA store and every fallback call failed with CERTIFICATE_VERIFY_FAILED.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tests.run_agent.test_fallback_credential_isolation import _make_agent

FB_URL = "https://fallback-llm.internal.example/v1"


def _activate(custom_providers):
    from agent.chat_completion_helpers import try_activate_fallback

    agent = _make_agent(provider="custom", model="primary-model",
                        base_url="https://primary-llm.internal.example/v1",
                        api_mode="chat_completions")
    agent._fallback_chain = [{"provider": "custom", "model": "fb-model",
                              "base_url": FB_URL, "api_key": "no-key"}]
    agent._credential_pool = None
    agent._buffer_status = MagicMock()
    agent._is_azure_openai_url.return_value = False
    agent._is_direct_openai_url.return_value = False
    agent._provider_model_requires_responses_api.return_value = False
    agent._anthropic_prompt_cache_policy.return_value = (False, False)
    agent._ensure_lmstudio_runtime_loaded = MagicMock()
    agent._replace_primary_openai_client = MagicMock()
    agent.context_compressor = None
    fb_client = SimpleNamespace(api_key="no-key", base_url=FB_URL, _custom_headers={})
    with patch("agent.auxiliary_client.resolve_provider_client", return_value=(fb_client, "fb-model")), \
         patch("agent.credential_pool.load_pool", return_value=None), \
         patch("hermes_cli.config.load_config_readonly",
               return_value={"custom_providers": custom_providers}):
        assert try_activate_fallback(agent) is True
    return agent


def test_fallback_keeps_custom_provider_ssl_verify():
    agent = _activate([{"name": "fb", "base_url": FB_URL, "ssl_verify": False}])
    assert agent.base_url == FB_URL
    assert agent._client_kwargs["base_url"] == FB_URL
    assert agent._client_kwargs.get("ssl_verify") is False


def test_fallback_without_tls_settings_adds_none():
    agent = _activate([{"name": "other", "base_url": "https://elsewhere.example/v1", "ssl_verify": False}])
    assert "ssl_verify" not in agent._client_kwargs
