"""Provider discovery with explicit metadata provenance and conservative fallbacks."""

import json
import re
from datetime import date

import httpx

MANIFEST_DATE = "2026-10-07"
OPENAI_DOCS = "https://developers.openai.com/api/docs/models/"
# Exact aliases/snapshots only: do not infer limits from arbitrary model-name prefixes.
OPENAI_MODELS = {
    "gpt-4.1": (1047576, 32768, "gpt-4.1", True),
    "gpt-4.1-2025-04-14": (1047576, 32768, "gpt-4.1", True),
    "gpt-4.1-mini": (1047576, 32768, "gpt-4.1-mini", True),
    "gpt-4.1-mini-2025-04-14": (1047576, 32768, "gpt-4.1-mini", True),
    "gpt-4.1-nano": (1047576, 32768, "gpt-4.1-nano", True),
    "gpt-4.1-nano-2025-04-14": (1047576, 32768, "gpt-4.1-nano", True),
    "gpt-4o": (128000, 16384, "gpt-4o", True),
    "gpt-4o-2024-08-06": (128000, 16384, "gpt-4o", True),
    "gpt-4o-2024-11-20": (128000, 16384, "gpt-4o", True),
    "gpt-4o-mini": (128000, 16384, "gpt-4o-mini", True),
    "gpt-4o-mini-2024-07-18": (128000, 16384, "gpt-4o-mini", True),
    "gpt-5": (400000, 128000, "gpt-5", True),
    "gpt-5-2025-08-07": (400000, 128000, "gpt-5", True),
    "gpt-5.4": (1050000, 128000, "gpt-5.4", True),
    "gpt-5.4-2026-03-05": (1050000, 128000, "gpt-5.4", True),
    "gpt-5.4-pro": (1050000, 128000, "gpt-5.4-pro", False),
}


def positive_int(value):
    return value if type(value) is int and value > 0 else None


def describe_model(value, provider, base_url, secret):
    model_id = value.get("id")
    if (
        not isinstance(model_id, str)
        or not model_id
        or len(model_id) > 200
        or secret in model_id
        or any(ord(char) < 32 for char in model_id)
    ):
        return None
    metadata = {
        "source": "platform_default",
        "source_url": "",
        "verified_at": "",
        "provider_max_output_tokens": None,
        "chat_compatible": None,
    }
    window = 32768
    limit = positive_int(value.get("max_output_tokens"))
    native_window = positive_int(value.get("context_window"))
    if native_window is not None and native_window > 2:
        window = native_window
        metadata.update(
            source="provider",
            source_url=base_url + "/models",
            verified_at=date.today().isoformat(),
            provider_max_output_tokens=limit,
        )
    elif provider == "openai" and base_url == "https://api.openai.com/v1":
        known = OPENAI_MODELS.get(model_id)
        if known:
            window, limit, slug, compatible = known
            metadata.update(
                source="official_manifest",
                source_url=OPENAI_DOCS + slug,
                verified_at=MANIFEST_DATE,
                provider_max_output_tokens=limit,
                chat_compatible=compatible,
            )
    if metadata["provider_max_output_tokens"] is None and limit:
        metadata["provider_max_output_tokens"] = limit
    # Exclude known non-text protocols; an unknown ID stays explicitly unverified.
    if re.search(
        r"embedding|tts|whisper|image|sora|audio|realtime|moderation|transcribe", model_id
    ):
        metadata["chat_compatible"] = False
    if value.get("output_modalities") == ["text"] and provider == "deepseek":
        metadata["chat_compatible"] = True
    margin = min(1024, window // 8)
    output = min(4096, limit or 4096, window - margin - 1)
    name = value.get("name")
    name = name.replace(secret, "[redacted]")[:100] if isinstance(name, str) else model_id
    return {
        "id": model_id,
        "name": name,
        "context_window": window,
        "max_output_tokens": output,
        "safety_margin_tokens": margin,
        "metadata": metadata,
        "purpose": "embedding" if "embedding" in model_id else "chat",
    }


async def discover(provider, base_url, secret):
    """Never expose provider response/error bodies, which may reflect Authorization."""
    if not secret:
        raise ValueError("请输入 API Key")
    try:
        async with httpx.AsyncClient(timeout=20, trust_env=False, follow_redirects=False) as client:
            async with client.stream(
                "GET", base_url + "/models", headers={"Authorization": "Bearer " + secret}
            ) as response:
                if response.status_code in (401, 403):
                    raise ValueError("模型列表鉴权失败，请检查 API Key 与访问权限")
                if response.status_code == 429:
                    raise ValueError("模型列表请求受限，请稍后重试")
                if not response.is_success:
                    raise ValueError("获取模型列表失败，请检查服务地址与接口支持情况")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 1024 * 1024:
                        raise ValueError("模型列表响应过大")
        data = json.loads(body)
    except (httpx.HTTPError, UnicodeError, json.JSONDecodeError):
        raise ValueError("获取模型列表失败，请检查网络、服务地址和响应格式") from None
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list) or len(rows) > 5000:
        raise ValueError("模型列表格式不正确")
    models = {}
    for row in rows:
        if isinstance(row, dict) and (entry := describe_model(row, provider, base_url, secret)):
            models[entry["id"]] = entry
    if not models:
        raise ValueError("该 Key 未返回可选模型，可检查权限或在高级设置中手动填写")
    return {"models": sorted(models.values(), key=lambda value: value["id"])}
