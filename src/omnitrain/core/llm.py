"""Unified LLM Client supporting both OpenAI-compatible (LM Studio, vLLM, llama.cpp, Local Mobile) and Ollama APIs."""
import json
import os
import re
from typing import Any, Dict, Optional, Protocol
import httpx

# Configuration via environment variables
DEFAULT_LLM_PROVIDER = os.environ.get("OMNITRAIN_LLM_PROVIDER", "openai_compatible")  # or "ollama"
DEFAULT_OPENAI_URL = os.environ.get("OMNITRAIN_LLM_URL", "http://100.82.144.89:1234/v1")
DEFAULT_OPENAI_MODEL = os.environ.get("OMNITRAIN_LLM_MODEL", "google/gemma-4-e4b")

DEFAULT_OLLAMA_URL = os.environ.get("OMNITRAIN_OLLAMA_URL", "http://127.0.0.1:11434")
DEFAULT_OLLAMA_MODEL = os.environ.get("OMNITRAIN_MODEL", "qwen2.5:3b")


class UnifiedLLMClient:
    """
    Client that connects to an OpenAI-compatible endpoint (LM Studio on Windows PC,
    or later local on mobile via Termux/MLC/llama.cpp) or falls back to Ollama.
    """

    def __init__(
        self,
        provider: str = DEFAULT_LLM_PROVIDER,
        base_url: str = DEFAULT_OPENAI_URL,
        model: str = DEFAULT_OPENAI_MODEL,
        timeout: float = 25.0
    ):
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def is_available(self) -> bool:
        """Quick connectivity check."""
        try:
            if self.provider == "ollama":
                r = httpx.get(f"{self.base_url}/api/tags", timeout=2.0)
                return r.status_code == 200
            else:
                # OpenAI-compatible /models endpoint
                r = httpx.get(f"{self.base_url}/models", timeout=2.0)
                return r.status_code == 200
        except Exception:
            return False

    def get_info(self) -> Dict[str, Any]:
        available = self.is_available()
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "available": available
        }

    def chat_completion_json(self, system_prompt: str, user_prompt: str, max_tokens: int = 1500) -> Optional[str]:
        """
        Sends chat completion request and extracts the JSON object from content.
        Gracefully returns None if unreachable or if decoding fails.
        """
        if not self.is_available():
            return None

        try:
            if self.provider == "ollama":
                res = httpx.post(
                    f"{self.base_url}/api/generate",
                    json={
                        "model": self.model,
                        "prompt": f"{system_prompt}\n\n{user_prompt}",
                        "format": "json",
                        "stream": False
                    },
                    timeout=self.timeout
                )
                if res.status_code == 200:
                    return res.json().get("response")
                return None
            else:
                # OpenAI-compatible chat completions
                payload = {
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ],
                    "temperature": 0.1,
                    "max_tokens": max_tokens
                }
                res = httpx.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    timeout=self.timeout
                )
                if res.status_code != 200:
                    return None

                data = res.json()
                choices = data.get("choices", [])
                if not choices:
                    return None

                msg = choices[0].get("message", {})
                content = msg.get("content") or ""
                return self._clean_json_string(content)
        except Exception:
            return None

    @staticmethod
    def _clean_json_string(raw: str) -> Optional[str]:
        if not raw:
            return None
        # Try finding ```json ... ``` code fence first
        code_fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw)
        candidate = code_fence.group(1).strip() if code_fence else raw.strip()
        # Find outer braces { ... }
        brace_match = re.search(r"\{[\s\S]*\}", candidate)
        if brace_match:
            candidate = brace_match.group(0)
        
        # Validate that it parses as JSON
        try:
            json.loads(candidate)
            return candidate
        except Exception:
            return None
