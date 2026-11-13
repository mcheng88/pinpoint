"""One call to a larger, hosted model. Used to write the short answer (answer.py).

Which model is one setting, read from the environment:

    PINPOINT_LLM=bedrock:openai.gpt-oss-120b-1:0     provider, then the provider's model id
    PINPOINT_LLM=openai:<model id>                   any service that speaks OpenAI's chat format
    PINPOINT_LLM=                                    empty: off, callers fall back

Bedrock uses the AWS profile in PINPOINT_AWS_PROFILE (default "sandbox").
"openai" reads the key from PINPOINT_OPENAI_KEY and the address from PINPOINT_OPENAI_URL
(default OpenAI's; Gemini's and a model served on our own machine take the same format).
Nothing here knows about cases. Callers decide what text is sent.
"""
import os

TIMEOUT = 60          # seconds; past this the caller falls back
_clients = {}


def setting(use=None):
    provider, _, model = (use or os.environ.get("PINPOINT_LLM", "")).partition(":")
    return (provider, model) if provider and model else (None, None)


def model_id():
    return setting()[1]


def _bedrock():
    if "bedrock" not in _clients:
        import boto3
        from botocore.config import Config
        session = boto3.Session(profile_name=os.environ.get("PINPOINT_AWS_PROFILE", "sandbox") or None,   # empty: the machine's own role
                                region_name=os.environ.get("PINPOINT_AWS_REGION", "us-east-1"))
        _clients["bedrock"] = session.client("bedrock-runtime", config=Config(
            read_timeout=TIMEOUT, connect_timeout=5, retries={"max_attempts": 1}))
    return _clients["bedrock"]


def _openai(model, system, user, max_tokens, effort):
    """(text, tokens read, tokens written) from a service that speaks OpenAI's chat format."""
    import json
    import urllib.request
    body = {"model": model, "max_completion_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if effort:
        body["reasoning_effort"] = effort
    url = os.environ.get("PINPOINT_OPENAI_URL", "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
    req = urllib.request.Request(url, json.dumps(body).encode(), {
        "Content-Type": "application/json", "Authorization": "Bearer " + os.environ.get("PINPOINT_OPENAI_KEY", "")})
    r = json.load(urllib.request.urlopen(req, timeout=TIMEOUT))
    use = r.get("usage") or {}
    return (r["choices"][0]["message"].get("content") or "").strip(), use.get("prompt_tokens", 0), use.get("completion_tokens", 0)


def complete(system, user, max_tokens=4000, effort=None, use=None):
    """The model's text, or None when it is off, slow or fails. `error` holds the last reason.
    `effort` ("low", "medium", "high") is how long the model reasons before it writes; it reasons by default."""
    global error
    provider, model = setting(use)          # `use` names another model for this one call, as "provider:model id"
    global tokens_in, tokens_out
    if provider not in ("bedrock", "openai"):
        error = "off" if not provider else f"unknown provider {provider}"
        return None
    try:
        if provider == "openai":
            text, read, written = _openai(model, system, user, max_tokens, effort)
            tokens_in += read
            tokens_out += written
            error = None if text else "empty reply"
            return text or None
        r = _bedrock().converse(
            modelId=model, system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": user}]}],
            inferenceConfig={"maxTokens": max_tokens, "temperature": 0},
            **({"additionalModelRequestFields": {"reasoning_effort": effort}} if effort else {}))
        tokens_in += r.get("usage", {}).get("inputTokens", 0)
        tokens_out += r.get("usage", {}).get("outputTokens", 0)
        text = "".join(b["text"] for b in r["output"]["message"]["content"] if "text" in b).strip()
        error = None if text else "empty reply"
        return text or None
    except Exception as e:          # no model is a normal state: the caller shows the source's own sentence
        error = f"{type(e).__name__}: {e}"[:300]
        return None


error = None
tokens_in = tokens_out = 0      # counted since the program started, to read the cost of a run
