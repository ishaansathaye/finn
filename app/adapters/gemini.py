from __future__ import annotations

import json
from typing import Any

import httpx

from app.config import get_settings


class GeminiAdapter:
    base_url = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self) -> None:
        self.settings = get_settings()

    def configured(self) -> bool:
        return bool(self.settings.gemini_api_key)

    async def generate_json(
        self,
        *,
        model: str,
        system_instruction: str,
        user_payload: dict[str, Any],
        temperature: float = 0.2,
        thinking_budget: int | None = None,
        timeout_seconds: float = 60,
    ) -> dict[str, Any]:
        if not self.configured():
            raise RuntimeError("Gemini API key is not configured")

        url = f"{self.base_url}/models/{model}:generateContent"
        params = {"key": self.settings.gemini_api_key}
        payload = {
            "system_instruction": {
                "parts": [{"text": system_instruction}],
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": json.dumps(user_payload, separators=(',', ':'))}],
                }
            ],
            "generationConfig": {
                "temperature": temperature,
                "responseMimeType": "application/json",
            },
        }
        if thinking_budget is not None and model.startswith("gemini-2.5-flash"):
            payload["generationConfig"]["thinkingConfig"] = {"thinkingBudget": thinking_budget}
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds, connect=10)) as client:
            response = await client.post(url, params=params, json=payload)
            response.raise_for_status()
            data = response.json()
        text = self._extract_text(data)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Gemini returned non-JSON output: {exc}") from exc

    def _extract_text(self, payload: dict[str, Any]) -> str:
        candidates = payload.get("candidates") or []
        if not candidates:
            raise RuntimeError("Gemini returned no candidates")
        content = candidates[0].get("content") or {}
        parts = content.get("parts") or []
        text_parts = [part.get("text") for part in parts if part.get("text")]
        if not text_parts:
            raise RuntimeError("Gemini candidate contained no text")
        return "".join(text_parts)
