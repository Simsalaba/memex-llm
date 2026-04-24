"""
Minimal async Ollama client using httpx.
Wraps /api/generate for structured JSON output.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx


class OllamaError(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, base_url: str, timeout: float = 300.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.timeout)

    def generate(
        self,
        model: str,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.1,
        max_retries: int = 3,
        num_ctx: int = 16384,
    ) -> str:
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "keep_alive": -1,  # never unload — model stays hot between requests
            "options": {
                "temperature": temperature,
                "num_predict": 2048,
                # Always set explicitly — Ollama's default is only 2048 tokens
                "num_ctx": num_ctx,
                # Larger batch = faster prompt processing (prefill phase)
                # 512 is Ollama default; 2048 is 4x faster prefill with same output quality
                "num_batch": 2048,
            },
        }
        if system:
            payload["system"] = system

        last_err: Exception | None = None
        for attempt in range(max_retries):
            try:
                with self._client() as client:
                    resp = client.post(f"{self.base_url}/api/generate", json=payload)
                resp.raise_for_status()
                return resp.json()["response"].strip()
            except (httpx.HTTPError, KeyError) as e:
                last_err = e
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)

        raise OllamaError(f"Ollama request failed after {max_retries} attempts: {last_err}")

    def generate_json(
        self,
        model: str,
        prompt: str,
        *,
        system: str | None = None,
        max_retries: int = 3,
        num_ctx: int = 16384,
    ) -> dict[str, Any]:
        """Generate and parse JSON output. Retries on parse failure."""
        for attempt in range(max_retries):
            raw = self.generate(model, prompt, system=system, temperature=0.05, num_ctx=num_ctx)
            try:
                # Strip markdown code fences if present
                cleaned = re.sub(r"^```(?:json)?\n?", "", raw.strip())
                cleaned = re.sub(r"\n?```$", "", cleaned.strip())
                return json.loads(cleaned)
            except json.JSONDecodeError:
                # Try extracting JSON object from response
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
        """
        Preload model into VRAM with a dummy request.
        Call once before starting a batch so the first real request doesn't pay
        the model-load penalty (~10-30s for 14B).
        """
        self.generate(model, ".", num_ctx=num_ctx, max_retries=1)

    def is_available(self) -> bool:
        try:
            with self._client() as client:
                resp = client.get(f"{self.base_url}/api/tags")
            return resp.status_code == 200
        except Exception:
            return False

    def list_models(self) -> list[str]:
        with self._client() as client:
            resp = client.get(f"{self.base_url}/api/tags")
        resp.raise_for_status()
        return [m["name"] for m in resp.json().get("models", [])]
