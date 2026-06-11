import os
import httpx

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterClient:
    def __init__(self):
        self.api_key = os.getenv("OPENROUTER_API_KEY", "")

    async def chat(self, model: str, messages: list, temperature: float = 0.4, max_tokens: int = 900):
        from app.core.context import openrouter_key_var
        api_key = openrouter_key_var.get() or self.api_key
        if not api_key:
            raise RuntimeError("Missing OPENROUTER_API_KEY in environment or request header.")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": os.getenv("OPENROUTER_REFERER", "http://localhost:5000"),
            "X-Title": os.getenv("OPENROUTER_TITLE", "STEM AI Teacher"),
        }
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        timeout = httpx.Timeout(60.0, connect=15.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(OPENROUTER_URL, headers=headers, json=payload)
            r.raise_for_status()
            data = r.json()

        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"OpenRouter returned no choices for model: {model}")

        message = choices[0].get("message") or {}
        content = message.get("content")

        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(item.get("text", ""))
            content = "".join(parts)

        content = (content or "").strip()
        if not content:
            raise RuntimeError(f"OpenRouter returned empty content for model: {model}")

        return content
