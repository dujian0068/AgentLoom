"""Chat Completions adapter shared by DeepSeek/OpenAI compatible connections."""

import asyncio
import math

import httpx

MAX_RETRIES = 2
RETRYABLE_STATUS = {429, 502, 503, 504}


def retry_delay(response, attempt):
    try:
        return min(5.0, max(0.0, float(response.headers.get("Retry-After", ""))))
    except ValueError:
        return min(5.0, 0.5 * (2**attempt))


async def request(model, endpoint, payload, secret):
    # Tool execution happens only after a complete model response reaches the harness.
    # Read timeouts are not retried: the provider may already have billed that request.
    async with httpx.AsyncClient(timeout=90, trust_env=False) as client:
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = await client.post(
                    model["base_url"].rstrip("/") + endpoint,
                    headers={"Authorization": "Bearer " + secret},
                    json=payload,
                )
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(0.5 * (2**attempt))
                    continue
                raise RuntimeError("无法连接模型服务，请检查接口地址与网络") from None
            except httpx.TimeoutException:
                raise RuntimeError("模型请求超时；未自动重复请求，可以恢复任务重试") from None
            except httpx.RequestError:
                raise RuntimeError("模型连接中断；未返回完整响应，可以恢复任务重试") from None
            if response.status_code in RETRYABLE_STATUS and attempt < MAX_RETRIES:
                await asyncio.sleep(retry_delay(response, attempt))
                continue
            if not response.is_success:
                explanations = {
                    400: "请检查模型参数和工具调用支持",
                    401: "请检查 API Key",
                    403: "请检查模型访问权限",
                    404: "请检查基础地址与模型 ID",
                    429: "限流或额度不足，短暂重试仍未成功",
                }
                hint = explanations.get(response.status_code, "服务异常，请稍后恢复任务重试")
                # Do not expose provider response bodies, URLs or credentials in errors.
                raise RuntimeError(f"模型请求失败 HTTP {response.status_code}：{hint}")
            try:
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError()
                return data
            except ValueError:
                raise RuntimeError("模型服务返回了无效的 JSON 响应") from None
    raise RuntimeError("模型请求未完成")


async def chat(model, messages, tools, secret):
    payload = {"model": model["model_id"], "messages": messages}
    for name in ("temperature", "top_p"):
        if model.get(name) is not None:
            payload[name] = model[name]
    if model.get("max_output_tokens"):
        limit_key = "max_completion_tokens" if model.get("provider") == "openai" else "max_tokens"
        payload[limit_key] = model["max_output_tokens"]
    if tools:
        payload["tools"] = tools
    data = await request(model, "/chat/completions", payload, secret)
    try:
        message = data["choices"][0]["message"]
        if not isinstance(message, dict):
            raise ValueError()
        content = message.get("content")
        calls = message.get("tool_calls")
        if content is not None and not isinstance(content, str):
            raise ValueError()
        if not calls and not (content or "").strip():
            raise ValueError()
        if calls:
            if not isinstance(calls, list):
                raise ValueError()
            ids = []
            for call in calls:
                if not isinstance(call["id"], str) or not call["id"]:
                    raise ValueError()
                if not isinstance(call["function"]["name"], str):
                    raise ValueError()
                if not isinstance(call["function"]["arguments"], str):
                    raise ValueError()
                ids.append(call["id"])
            if len(set(ids)) != len(ids):
                raise ValueError()
        return {
            **message,
            **({"usage": data["usage"]} if "usage" in data else {}),
            **({"provider_request_id": data["id"]} if "id" in data else {}),
            "finish_reason": data["choices"][0].get("finish_reason"),
        }
    except (KeyError, IndexError, TypeError, ValueError):
        raise RuntimeError("模型响应缺少有效回答或工具调用，请检查模型协议兼容性") from None


async def embeddings(model, texts, secret):
    data = await request(model, "/embeddings", {"model": model["model_id"], "input": texts}, secret)
    try:
        records = sorted(data["data"], key=lambda item: item["index"])
        if [item["index"] for item in records] != list(range(len(texts))):
            raise ValueError()
        vectors = [item["embedding"] for item in records]
        dimension = len(vectors[0]) if vectors else 0
        if not dimension:
            raise ValueError()
        for vector in vectors:
            if not isinstance(vector, list) or len(vector) != dimension:
                raise ValueError()
            if any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                for value in vector
            ):
                raise ValueError()
        return vectors
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("向量响应的数量、索引或维度不合法") from None
