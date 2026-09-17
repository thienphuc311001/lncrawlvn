"""The only Gemini transport: one process-wide admission gate for every job/retry."""

from __future__ import annotations

import asyncio
import json
import os
import time
from email.utils import parsedate_to_datetime

import httpx
from pydantic import ValidationError

from .models import MODELS


class ProviderError(RuntimeError):
    def __init__(self, message, retryable=False, retry_after=0):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


def retry_delay(value):
    try:
        return max(0, float(value))
    except (ValueError, TypeError):
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            return 0


class Scheduler:
    def __init__(self, concurrency=2, spacing=0.3, timeout=120, transport=None):
        if not 1 <= concurrency <= 3:
            raise ValueError("Translation concurrency must be 1–3")
        self.concurrency = concurrency
        self.slots = asyncio.Semaphore(concurrency)
        self.gate = asyncio.Lock()
        self.next_start = 0.0
        self.spacing = spacing
        self.timeout = timeout
        self.transport = transport

    async def request(self, operation, payload, schema, record):
        # Serialize local inputs before entering provider retries. Local bugs never fall back.
        prompt = json.dumps(payload, ensure_ascii=False)
        body = {
            "systemInstruction": {"parts": [{"text": operation}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseJsonSchema": schema.model_json_schema(),
                "temperature": 0.15,
            },
        }
        attempts = 0
        for model in MODELS:
            for attempt in range(2):
                attempts += 1
                try:
                    async with self.slots:
                        async with self.gate:
                            await asyncio.sleep(max(0, self.next_start - time.monotonic()))
                            self.next_start = time.monotonic() + self.spacing
                        result = await asyncio.wait_for(
                            self.transport(model, body)
                            if self.transport
                            else self._send(model, body),
                            timeout=self.timeout,
                        )
                        try:
                            parsed = schema.model_validate(result)
                        except ValidationError as exc:
                            raise ProviderError("Invalid provider response schema", True) from exc
                    record({"model": model, "retry_count": attempts - 1, "status": "success"})
                    return parsed
                except (httpx.TransportError, asyncio.TimeoutError, TimeoutError) as exc:
                    error = ProviderError("Provider network failure or timeout", True)
                    error.__cause__ = exc
                except ProviderError as exc:
                    error = exc
                record(
                    {
                        "model": model,
                        "retry_count": attempts - 1,
                        "status": "failed",
                        "error": str(error),
                    }
                )
                if not error.retryable:
                    raise error
                # Shared cooldown also prevents other workers bursting into a quota failure.
                delay = max(error.retry_after, 1.0 * 2**attempt)
                async with self.gate:
                    self.next_start = max(self.next_start, time.monotonic() + delay)
        raise error

    async def _send(self, model, body):
        key = os.environ.get("GOOGLE_AI_API_KEY", "")
        if not key:
            raise ProviderError("Set GOOGLE_AI_API_KEY on the server")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": key},
                json=body,
            )
        if response.status_code != 200:
            code = response.status_code
            raise ProviderError(
                f"Gemini HTTP {code} for configured model {model}",
                code in (408, 429) or code >= 500,
                retry_delay(response.headers.get("Retry-After")),
            )
        try:
            data = response.json()
            candidate = data["candidates"][0]
            reason = candidate.get("finishReason")
            if reason in ("SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "RECITATION"):
                raise ProviderError(f"Gemini blocked response: {reason}")
            if reason != "STOP":
                raise ProviderError("Provider response interrupted", True)
            text = "".join(
                p.get("text", "") for p in candidate["content"]["parts"] if not p.get("thought")
            )
            if not text.strip():
                raise ValueError("Empty response")
            return json.loads(text)
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise ProviderError("Invalid/empty provider response", True) from exc
