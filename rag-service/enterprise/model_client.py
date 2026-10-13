"""One configured HTTPS provider. No user-selected endpoints or implicit SDK retries."""
import asyncio
import json
import time
import httpx

from .errors import ProviderUnavailable
from .store import vector_literal


class ModelClient:
    def __init__(self, settings):
        self.settings = settings

    def close(self):
        pass

    def _post(self, path, payload, deadline=None):
        remaining = self.settings.model_timeout if deadline is None else min(self.settings.model_timeout, deadline-time.monotonic())
        if remaining <= 0:
            raise ProviderUnavailable("Generation deadline exceeded")
        async def post():
            async with httpx.AsyncClient(base_url=self.settings.model_base_url + "/", timeout=httpx.Timeout(remaining, connect=min(5, remaining)),
                                         follow_redirects=False, trust_env=False,
                                         headers={"Authorization": "Bearer " + self.settings.model_api_key}) as client:
                async with client.stream("POST", path, json=payload) as response:
                    response.raise_for_status()
                    body = bytearray()
                    async for part in response.aiter_bytes():
                        body.extend(part)
                        if len(body) > 8 * 1024 * 1024:
                            raise ValueError("Provider response exceeds limit")
                    return json.loads(body)
        async def bounded_post():
            return await asyncio.wait_for(post(), timeout=remaining)
        try:
            return asyncio.run(bounded_post())
        except (httpx.HTTPError, ValueError, asyncio.TimeoutError):
            raise ProviderUnavailable("Model provider unavailable") from None

    def embed(self, texts, deadline=None):
        data = self._post("embeddings", {"model": self.settings.embedding_model, "input": texts,
                                         "dimensions": self.settings.embedding_dimension, "encoding_format": "float"}, deadline)
        try:
            rows = sorted(data["data"], key=lambda r: r["index"])
            if [r["index"] for r in rows] != list(range(len(texts))):
                raise ValueError()
            result = [r["embedding"] for r in rows]
            if any(len(e) != self.settings.embedding_dimension for e in result):
                raise ValueError()
            for e in result:
                vector_literal(e)
            return result
        except (KeyError, TypeError, ValueError):
            raise ProviderUnavailable("Invalid embedding response") from None

    def chat(self, messages, deadline=None):
        result = self._post("chat/completions", {"model": self.settings.chat_model, "messages": messages,
                                                 "temperature": 0, "max_tokens": self.settings.output_tokens, "stream": False}, deadline)
        try:
            choice = result["choices"][0]
            answer = choice["message"]["content"]
            if choice.get("finish_reason") != "stop" or not isinstance(answer, str) or not answer.strip() or len(answer) > 20000:
                raise ValueError()
            return answer
        except (KeyError, IndexError, TypeError, ValueError):
            raise ProviderUnavailable("Invalid or incomplete model response") from None
