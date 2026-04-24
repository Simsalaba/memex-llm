"""
OpenAI-compatible client — drop-in alternative to OllamaClient.

Works with any provider that implements the OpenAI /v1/chat/completions API:
OpenAI, Together AI, Fireworks, Cerebras, LM Studio, llama.cpp server, etc.

Usage in config.yaml:
  extra_ollama_urls:
    - "https://api.together.xyz/v1"

Pass your API key via:
  wiki ingest chatgpt --extra-url https://api.together.xyz/v1 --api-key sk-...
  or via environment: OPENAI_API_KEY / LLM_WIKI_API_KEY
"""
from __future__ import annotations

import os
import time
from typing import Any

import httpx

from pipeline.ollama_client import OllamaError


class OpenAICompatibleClient:
    """
    Implements the same interface as OllamaClient for seamless pipeline integration.
    Speaks the OpenAI /v1/chat/completions API.
    """

    def __init__(self, base_url: str, api_key: str, timeout: float = 300.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _client(self) -> httpx.Client:
        return httpx.Client(
            timeout=self.timeout,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )

    def generate(
        self,
        model: str,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.1,
        max_retries: int = 3,
        num_ctx: int = 16384,  # ignored — OpenAI API doesn't expose ctx window
    ) -> str:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": 2048,
        }

        last_err: Exception | None = None
        for attempt in range(max_retries):
            try:
                with self._client() as client:
                    resp = client.post(f"{self.base_url}/chat/completions", json=payload)
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"].strip()
            except (httpx.HTTPError, KeyError, IndexError) as e:
                last_err = e
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)

        raise OllamaError(f"OpenAI request failed after {max_retries} attempts: {last_err}")

    def generate_json(
        self,
        model: str,
        prompt: str,
        *,
        system: str | None = None,
        max_retries: int = 3,
        num_ctx: int = 16384,
    ) -> dict[str, Any]:
        import json
        import re

        for attempt in range(max_retries):
            raw = self.generate(model, prompt, system=system, temperature=0.05, num_ctx=num_ctx)
            try:
                cleaned = re.sub(r"^```(?:json)?\n?", "", raw.strip())
                cleaned = re.sub(r"\n?```$", "", cleaned.strip())
                return json.loads(cleaned)
            except json.JSONDecodeError:
                m = re.search(r"\{[\s\S]+\}", raw)
                if m:
                    try:
                        return json.loads(m.group())
                    except json.JSONDecodeError:
                        pass
                if attempt == max_retries - 1:
                    raise OllamaError(f"Failed to parse JSON from model output: {raw[:500]}")

        raise OllamaError("Exhausted retries")

    def warmup(self, model: str, num_ctx: int = 16384) -> None:
        """No-op — cloud APIs don't need model preloading."""
        pass

    def is_available(self) -> bool:
        try:
            with self._client() as client:
                resp = client.get(f"{self.base_url}/models")
            return resp.status_code == 200
        except Exception:
            return False

    def list_models(self) -> list[str]:
        with self._client() as client:
            resp = client.get(f"{self.base_url}/models")
        resp.raise_for_status()
        return [m["id"] for m in resp.json().get("data", [])]
