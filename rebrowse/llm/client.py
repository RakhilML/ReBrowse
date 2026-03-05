"""Multi-provider LLM client.

Supports: local (LM Studio/vLLM), OpenAI, Gemini, Claude, Ollama.
All providers are accessed via their chat completions API.
Uses DSPy signatures for structured I/O when available.
"""

from __future__ import annotations
import json
import httpx
from rebrowse import config

_http = httpx.AsyncClient(timeout=120.0)

# DSPy modules (lazy-initialized)
_dspy_configured = False
_intent_module = None
_picker_module = None
_describer_module = None

# Provider-specific base URLs (used when LLM_BASE_URL is not customized)
_PROVIDER_DEFAULTS = {
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "claude": "https://api.anthropic.com/v1",
    "ollama": "http://localhost:11434/v1",
    "local": "http://localhost:1234/v1",
}


def _get_base_url() -> str:
    """Resolve the base URL for the current provider."""
    provider = config.LLM_PROVIDER.lower()
    base = config.LLM_BASE_URL

    # If the user set a custom URL, use it as-is
    if base != "http://localhost:1234/v1":
        return base

    # Otherwise use the provider default
    return _PROVIDER_DEFAULTS.get(provider, base)


def _get_headers() -> dict[str, str]:
    """Build auth headers for the current provider."""
    provider = config.LLM_PROVIDER.lower()
    key = config.LLM_API_KEY
    headers: dict[str, str] = {"Content-Type": "application/json"}

    if provider == "claude":
        if key:
            headers["x-api-key"] = key
            headers["anthropic-version"] = "2023-06-01"
    elif provider in ("openai", "gemini"):
        if key:
            headers["Authorization"] = f"Bearer {key}"
    elif provider == "ollama":
        pass  # no auth needed
    else:
        # local (LM Studio, vLLM, etc.) — send key if provided
        if key:
            headers["Authorization"] = f"Bearer {key}"

    return headers


def _ensure_dspy():
    """Configure DSPy with the LLM on first use."""
    global _dspy_configured, _intent_module, _picker_module, _describer_module
    if _dspy_configured:
        return

    try:
        import dspy
        from rebrowse.llm.signatures import ParseIntent, PickEndpoint, DescribeEndpoints

        provider = config.LLM_PROVIDER.lower()
        base_url = _get_base_url()
        key = config.LLM_API_KEY or "local"

        if provider == "claude":
            model_id = f"anthropic/{config.LLM_MODEL_INTENT}"
        elif provider == "gemini":
            model_id = f"google/{config.LLM_MODEL_INTENT}"
        else:
            model_id = f"openai/{config.LLM_MODEL_INTENT}"

        lm = dspy.LM(
            model=model_id,
            api_base=base_url,
            api_key=key,
            temperature=0.1,
            max_tokens=4096,
        )
        dspy.configure(lm=lm)

        _intent_module = dspy.Predict(ParseIntent)
        _picker_module = dspy.Predict(PickEndpoint)
        _describer_module = dspy.Predict(DescribeEndpoints)
        _dspy_configured = True
    except Exception:
        _dspy_configured = False


async def _chat_openai_compat(
    messages: list[dict],
    model: str,
    temperature: float,
    max_tokens: int,
) -> str:
    """OpenAI-compatible chat completions (works for local, OpenAI, Gemini, Ollama)."""
    base_url = _get_base_url()
    headers = _get_headers()

    resp = await _http.post(
        f"{base_url}/chat/completions",
        headers=headers,
        json={
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        },
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


async def _chat_claude(
    messages: list[dict],
    model: str,
    temperature: float,
    max_tokens: int,
) -> str:
    """Anthropic Messages API."""
    base_url = _get_base_url()
    headers = _get_headers()

    # Anthropic expects system as a top-level param, not in messages
    system = ""
    filtered = []
    for m in messages:
        if m["role"] == "system":
            system = m["content"]
        else:
            filtered.append(m)

    body = {
        "model": model,
        "messages": filtered,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if system:
        body["system"] = system

    resp = await _http.post(
        f"{base_url}/messages",
        headers=headers,
        json=body,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["content"][0]["text"]


async def chat(
    prompt: str,
    system: str = "",
    model: str | None = None,
    temperature: float = 0.3,
    max_tokens: int = 4096,
) -> str:
    """Send a chat completion to the configured LLM provider."""
    model = model or config.LLM_MODEL_INTENT
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    provider = config.LLM_PROVIDER.lower()

    if provider == "claude":
        content = await _chat_claude(messages, model, temperature, max_tokens)
    else:
        # OpenAI-compatible: local, openai, gemini, ollama
        content = await _chat_openai_compat(messages, model, temperature, max_tokens)

    # Strip <think>...</think> tags (common with qwen/deepseek models)
    if "<think>" in content:
        parts = content.split("</think>")
        content = parts[-1].strip() if len(parts) > 1 else content
    return content


def _extract_json(raw: str) -> str:
    """Strip markdown fences and whitespace from LLM output."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        raw = raw.rsplit("```", 1)[0]
    return raw.strip()


async def describe_endpoints(endpoints_json: str) -> str:
    """Use the code model to generate descriptions for extracted endpoints.

    DSPy Signature: DescribeEndpoints
        Input:  endpoints_json (str) - JSON array of {url_template, method}
        Output: descriptions_json (str) - JSON array of {url_template, method, description}
    """
    raw = await chat(
        prompt=(
            "Given these API endpoints extracted from a website's network traffic, "
            "write a short one-line description for each endpoint explaining what it does. "
            "Return ONLY a JSON array of objects with keys: url_template, method, description.\n\n"
            f"{endpoints_json}"
        ),
        system="You are an API analyst. Be concise and accurate. Return ONLY valid JSON, no markdown fences, no explanation.",
        model=config.LLM_MODEL_CODE,
        temperature=0.1,
    )
    return _extract_json(raw)


async def parse_intent(user_query: str) -> dict:
    """Parse user intent into structured form: domain, action, params.

    DSPy Signature: ParseIntent
        Input:  user_query (str)
        Output: domain (str), action (str), params (dict)
    """
    _ensure_dspy()
    if _dspy_configured and _intent_module:
        try:
            result = _intent_module(user_query=user_query)
            return {
                "domain": result.domain or None,
                "action": result.action,
                "params": result.params if isinstance(result.params, dict) else {},
            }
        except Exception:
            pass

    raw = await chat(
        prompt=(
            f"User wants: {user_query}\n\n"
            "Extract the intent as JSON with these exact fields:\n"
            '- "domain": the website domain (e.g. "github.com") or null if unclear\n'
            '- "action": what the user wants to do in 5-10 words\n'
            '- "params": dict of any specific parameters mentioned (empty dict if none)\n\n'
            "Return ONLY valid JSON, no markdown fences, no explanation."
        ),
        system="You extract structured intent from natural language. Return ONLY JSON.",
        temperature=0.1,
    )
    return json.loads(_extract_json(raw))


async def pick_endpoint(intent: str, endpoints: list[dict]) -> dict:
    """Ask the LLM to pick the best endpoint for the user's intent.

    DSPy Signature: PickEndpoint
        Input:  intent (str), endpoints (list[dict])
        Output: endpoint_id (str), params (dict), query (dict), reason (str)
    """
    _ensure_dspy()
    if _dspy_configured and _picker_module:
        try:
            result = _picker_module(intent=intent, endpoints=endpoints)
            return {
                "endpoint_id": result.endpoint_id,
                "params": result.params if isinstance(result.params, dict) else {},
                "query": result.query if isinstance(result.query, dict) else {},
                "reason": result.reason,
            }
        except Exception:
            pass

    raw = await chat(
        prompt=(
            f"User intent: {intent}\n\n"
            f"Available API endpoints:\n{json.dumps(endpoints, indent=2)}\n\n"
            "Pick the single best endpoint that matches the user's intent.\n"
            "Return ONLY a JSON object with:\n"
            '- "endpoint_id": the id of the chosen endpoint\n'
            '- "params": dict of path parameter values to substitute (if any)\n'
            '- "query": dict of query parameter values (if any)\n'
            '- "reason": one-line explanation of why you picked this endpoint\n'
        ),
        system="You are an API routing expert. Pick the best endpoint. Return ONLY JSON.",
        temperature=0.1,
    )
    return json.loads(_extract_json(raw))
