"""The only Gemini transport and its process-wide model/key availability pool."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from email.utils import parsedate_to_datetime
from enum import Enum

import httpx
from pydantic import ValidationError

from .models import FALLBACK_MODEL, MODELS, PRIMARY_MODEL


class ErrorCategory(str, Enum):
    DAILY_QUOTA_EXHAUSTED = "DAILY_QUOTA_EXHAUSTED"
    RPM_LIMIT = "RPM_LIMIT"
    TPM_LIMIT = "TPM_LIMIT"
    TEMPORARY_PROVIDER_ERROR = "TEMPORARY_PROVIDER_ERROR"
    AUTHENTICATION_ERROR = "AUTHENTICATION_ERROR"
    MODEL_FAILURE = "MODEL_FAILURE"
    INCOMPLETE_GENERATION = "INCOMPLETE_GENERATION"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


class ProviderError(RuntimeError):
    def __init__(
        self,
        message,
        retryable=False,
        retry_after=0,
        daily_quota=False,
        status_code=None,
        category=ErrorCategory.UNKNOWN_ERROR,
    ):
        super().__init__(message)
        self.retryable, self.retry_after, self.status_code = retryable, retry_after, status_code
        self.category = (
            ErrorCategory.DAILY_QUOTA_EXHAUSTED
            if daily_quota and category is ErrorCategory.UNKNOWN_ERROR
            else ErrorCategory(category)
        )
        self.daily_quota = daily_quota or self.category is ErrorCategory.DAILY_QUOTA_EXHAUSTED


def api_keys():
    """Ordered, deduplicated server credentials; never expose values in diagnostics."""
    values = [
        os.getenv(name, "")
        for name in ("GOOGLE_AI_API_KEY", "GOOGLE_AI_API_KEY_BACKUP", "GOOGLE_AI_API_KEY_THIRD")
    ]
    values.extend(os.getenv("GOOGLE_AI_API_KEYS", "").split(","))
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def model_failure_summary(failures):
    """Compatibility summary for old saved checkpoints, which are model-keyed."""
    return "All configured Gemini models failed (tried in order): " + "; ".join(
        f"{model}: {failures[model]}" for model in MODELS
    )


def retry_delay(value):
    try:
        return max(0, float(value))
    except (ValueError, TypeError):
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            return 0


def response_retry_delay(response):
    delay = retry_delay(response.headers.get("Retry-After"))
    try:
        for detail in response.json().get("error", {}).get("details", []):
            if detail.get("@type") == "type.googleapis.com/google.rpc.RetryInfo":
                duration = re.fullmatch(r"(\d+(?:\.\d+)?)s", detail.get("retryDelay", ""))
                if duration:
                    delay = max(delay, float(duration[1]))
    except (ValueError, AttributeError, TypeError):
        pass
    return delay


def response_error_category(response):
    """Classify provider responses without treating every 429 as daily quota."""
    code = response.status_code
    text = " ".join(
        [
            response.headers.get("x-ratelimit-limit", ""),
            response.headers.get("x-ratelimit-resource", ""),
        ]
    ).casefold()
    try:
        error = response.json().get("error", {})
        text += " " + str(error.get("message", "")).casefold()
        text += " " + " ".join(str(detail).casefold() for detail in error.get("details", []))
    except (ValueError, AttributeError, TypeError):
        pass
    normalized = re.sub(r"[^a-z0-9]+", "", text)
    if "perday" in normalized or "requestsperday" in normalized or "rpd" in normalized:
        return ErrorCategory.DAILY_QUOTA_EXHAUSTED
    if "tokensperminute" in normalized or "tpm" in normalized:
        return ErrorCategory.TPM_LIMIT
    if "requestsperminute" in normalized or "rpm" in normalized or "ratelimit" in normalized:
        return ErrorCategory.RPM_LIMIT
    if code == 404:
        return ErrorCategory.MODEL_FAILURE
    if code in (400, 401, 403):
        return ErrorCategory.AUTHENTICATION_ERROR
    if code in (408, 409) or code >= 500:
        return ErrorCategory.TEMPORARY_PROVIDER_ERROR
    return ErrorCategory.UNKNOWN_ERROR


def response_daily_quota(response):
    return response_error_category(response) is ErrorCategory.DAILY_QUOTA_EXHAUSTED


def provider_json_schema(schema):
    """Emit only Gemini's documented JSON Schema subset; validate fully locally."""
    raw = schema.model_json_schema()
    scalar = {"$id", "$ref", "$anchor", "type", "format", "title", "description",
              "enum", "minItems", "maxItems", "minimum", "maximum", "required"}
    maps = {"properties", "$defs"}
    nested = {"items", "additionalProperties"}
    arrays = {"prefixItems", "anyOf", "oneOf"}

    def clean(node):
        if not isinstance(node, dict):
            return node
        result = {}
        for key, value in node.items():
            if key == "const":
                result["enum"] = [value]
            elif key in maps and isinstance(value, dict):
                result[key] = {name: clean(child) for name, child in value.items()}
            elif key in nested:
                result[key] = clean(value)
            elif key in arrays and isinstance(value, list):
                result[key] = [clean(child) for child in value]
            elif key in scalar:
                result[key] = value
        if "$ref" in result:
            return {"$ref": result["$ref"]}
        return result

    return clean(raw)


class Scheduler:
    """Share model/key availability state across all tasks in one API or CLI run."""

    RATE_LIMIT_COOLDOWN = 60

    def __init__(self, concurrency=2, spacing=0.3, timeout=120, transport=None, keys=None):
        if not 1 <= concurrency <= 3:
            raise ValueError("Translation concurrency must be 1–3")
        self.concurrency, self.spacing, self.timeout, self.transport = (
            concurrency,
            spacing,
            timeout,
            transport,
        )
        self.slots, self.gate, self.state_lock = (
            asyncio.Semaphore(concurrency),
            asyncio.Lock(),
            asyncio.Lock(),
        )
        self.next_start = 0.0
        self.keys = list(keys) if keys is not None else (api_keys() if not transport else [None])
        self.keys = self.keys or [None]
        self.pairs = [(model, key_slot) for model in MODELS for key_slot in range(len(self.keys))]
        self.daily_exhausted, self.cooldowns, self.disabled_keys = set(), {}, set()
        self.max_attempts_per_pair = max(1, int(os.getenv("TRANSLATION_RETRIES_PER_PAIR", "2")) + 1)

    @staticmethod
    def _pair_label(pair):
        return f"{pair[0]} [key slot {pair[1] + 1}]"

    async def _next_pair(self, excluded, preferred=None, after=None, ordered_pairs=None):
        """Choose a ready pair; wait only when all otherwise valid pairs are cooling."""
        ordered_pairs = ordered_pairs or self.pairs
        async with self.state_lock:
            now = time.monotonic()
            self.cooldowns = {pair: until for pair, until in self.cooldowns.items() if until > now}
            if (
                preferred
                and preferred not in excluded
                and preferred not in self.daily_exhausted
                and preferred not in self.cooldowns
                and preferred[1] not in self.disabled_keys
            ):
                return preferred, 0
            start = 0 if after is None else (ordered_pairs.index(after) + 1) % len(ordered_pairs)
            for offset in range(len(ordered_pairs)):
                index = (start + offset) % len(ordered_pairs)
                pair = ordered_pairs[index]
                if (
                    pair not in excluded
                    and pair not in self.daily_exhausted
                    and pair not in self.cooldowns
                    and pair[1] not in self.disabled_keys
                ):
                    return pair, 0
            waits = [
                until - now
                for pair, until in self.cooldowns.items()
                if pair in ordered_pairs and pair not in excluded
                and pair not in self.daily_exhausted
                and pair[1] not in self.disabled_keys
            ]
            return None, max(0, min(waits)) if waits else 0

    async def _disable(self, pair, error):
        async with self.state_lock:
            if error.category is ErrorCategory.DAILY_QUOTA_EXHAUSTED:
                self.daily_exhausted.add(pair)
            elif error.category in (ErrorCategory.RPM_LIMIT, ErrorCategory.TPM_LIMIT):
                self.cooldowns[pair] = time.monotonic() + max(self.RATE_LIMIT_COOLDOWN, error.retry_after)
            elif error.category is ErrorCategory.AUTHENTICATION_ERROR:
                self.disabled_keys.add(pair[1])

    async def request(self, operation, payload, schema, record,
                      requested_model=PRIMARY_MODEL, allow_fallback=True):
        if requested_model not in MODELS:
            raise ValueError(f"Unsupported configured model {requested_model!r}")
        model_order = ([PRIMARY_MODEL, FALLBACK_MODEL] if requested_model == PRIMARY_MODEL
                       and allow_fallback else [requested_model])
        ordered_pairs = [(model, slot) for model in model_order for slot in range(len(self.keys))]
        generation_config = {
            "responseMimeType": "application/json",
            "responseJsonSchema": provider_json_schema(schema),
        }
        if os.getenv("TRANSLATION_THINKING_BUDGET"):
            generation_config["thinkingConfig"] = {
                "thinkingBudget": int(os.environ["TRANSLATION_THINKING_BUDGET"])
            }
        body = {
            "systemInstruction": {"parts": [{"text": operation}]},
            "contents": [
                {"role": "user", "parts": [{"text": json.dumps(payload, ensure_ascii=False)}]}
            ],
            "generationConfig": generation_config,
        }
        attempts, pair_attempts, excluded, failures, preferred, after = 0, {}, set(), {}, None, None
        failure_categories = {}
        previous_pair = None
        previous_failure_category = None
        while True:
            pair, wait = await self._next_pair(excluded, preferred, after, ordered_pairs)
            preferred = None
            if pair is None:
                if wait:
                    record(
                        {
                            "status": "waiting_for_rate_limit",
                            "category": ErrorCategory.RPM_LIMIT.value,
                            "retry_after": wait,
                            "message": f"All remaining pairs are rate-limited; retrying after {wait:.0f}s",
                        }
                    )
                    await asyncio.sleep(wait)
                    continue
                summary = "; ".join(
                    f"{self._pair_label(item)}: {reason}" for item, reason in failures.items()
                )
                category = (ErrorCategory.INCOMPLETE_GENERATION
                            if failures and all(value is ErrorCategory.INCOMPLETE_GENERATION
                                                for value in failure_categories.values())
                            else ErrorCategory.UNKNOWN_ERROR)
                raise ProviderError(
                    f"No usable Gemini model/API-key combination remains: {summary}"
                    if summary else "No usable Gemini model/API-key combination is configured",
                    category=category,
                )
            model, key_slot = pair
            attempts += 1
            pair_attempts[pair] = pair_attempts.get(pair, 0) + 1
            metadata = {
                "model": model,
                "key_slot": key_slot + 1,
                "retry_count": attempts - 1,
                "attempt": pair_attempts[pair],
                "key_rotated": previous_pair is not None and previous_pair[1] != key_slot,
                "model_changed": previous_pair is not None and previous_pair[0] != model,
                "requested_model": requested_model,
                "technical_retry": previous_failure_category in (
                    ErrorCategory.RPM_LIMIT, ErrorCategory.TPM_LIMIT,
                    ErrorCategory.TEMPORARY_PROVIDER_ERROR,
                ),
                "model_fallback_started": model != requested_model
                and (previous_pair is None or previous_pair[0] != model),
            }
            try:
                async with self.slots:
                    async with self.gate:
                        await asyncio.sleep(max(0, self.next_start - time.monotonic()))
                        self.next_start = time.monotonic() + self.spacing
                    record({**metadata, "status": "running"})
                    previous_pair = pair
                    result = await asyncio.wait_for(
                        self.transport(model, body)
                        if self.transport
                        else self._send(model, body, self.keys[key_slot]),
                        timeout=self.timeout,
                    )
                    response_meta = {}
                    if isinstance(result, dict) and "__response_meta__" in result:
                        result = dict(result)
                        response_meta = result.pop("__response_meta__")
                    parsed = schema.model_validate(result)
                    object.__setattr__(parsed, "_request_meta", {**metadata, **response_meta,
                        "requested_model": requested_model, "actual_model": model,
                        "generation_config": generation_config})
                record({**metadata, **response_meta, "status": "success"})
                return parsed
            except asyncio.CancelledError:
                record({**metadata, "status": "cancelled"})
                raise
            except ValidationError as exc:
                error = ProviderError(
                    "Invalid provider response schema",
                    True,
                    category=ErrorCategory.TEMPORARY_PROVIDER_ERROR,
                )
                error.__cause__ = exc
            except (httpx.TransportError, asyncio.TimeoutError, TimeoutError) as exc:
                error = ProviderError(
                    "Provider network failure or timeout",
                    True,
                    category=ErrorCategory.TEMPORARY_PROVIDER_ERROR,
                )
                error.__cause__ = exc
            except ProviderError as exc:
                error = exc
            failures[pair] = str(error)
            failure_categories[pair] = error.category
            previous_failure_category = error.category
            record(
                {
                    **metadata,
                    "status": "failed",
                    "error": str(error),
                    "category": error.category.value,
                    "retry_after": error.retry_after,
                    "daily_quota": error.daily_quota,
                }
            )
            if error.category in (
                ErrorCategory.DAILY_QUOTA_EXHAUSTED,
                ErrorCategory.RPM_LIMIT,
                ErrorCategory.TPM_LIMIT,
                ErrorCategory.AUTHENTICATION_ERROR,
            ):
                await self._disable(pair, error)
                if error.category is ErrorCategory.DAILY_QUOTA_EXHAUSTED:
                    status, duration, message = (
                        "daily_quota_disabled",
                        0,
                        f"Disabled {self._pair_label(pair)} for this run",
                    )
                elif error.category in (ErrorCategory.RPM_LIMIT, ErrorCategory.TPM_LIMIT):
                    status, duration, message = (
                        "rate_limit_cooldown",
                        self.RATE_LIMIT_COOLDOWN,
                        f"Suspended {self._pair_label(pair)} for {self.RATE_LIMIT_COOLDOWN}s",
                    )
                else:
                    status, duration, message = (
                        "key_disabled",
                        0,
                        f"Disabled key slot {key_slot + 1} for this run after authentication/configuration failure",
                    )
                record(
                    {
                        **metadata,
                        "status": status,
                        "category": error.category.value,
                        "retry_after": duration,
                        "message": message,
                    }
                )
                after = pair
                continue
            if error.retryable and pair_attempts[pair] < self.max_attempts_per_pair:
                delay = max(
                    error.retry_after + 1 if error.retry_after else 0,
                    1.0 * 2 ** (pair_attempts[pair] - 1),
                )
                record(
                    {
                        **metadata,
                        "status": "retrying",
                        "category": error.category.value,
                        "retry_after": delay,
                        "message": f"Retrying {self._pair_label(pair)} after at least {delay:g}s",
                    }
                )
                async with self.gate:
                    self.next_start = max(self.next_start, time.monotonic() + delay)
                preferred = pair
                continue
            excluded.add(pair)
            after = pair

    async def _send(self, model, body, key=None):
        key = key or next(iter(api_keys()), "")
        if not key:
            raise ProviderError(
                "Set GOOGLE_AI_API_KEY on the server", category=ErrorCategory.AUTHENTICATION_ERROR
            )
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": key},
                json=body,
            )
        if response.status_code != 200:
            category = response_error_category(response)
            raise ProviderError(
                f"Gemini HTTP {response.status_code} for configured model {model} ({category.value})",
                response.status_code == 429
                or category
                in (
                    ErrorCategory.RPM_LIMIT,
                    ErrorCategory.TPM_LIMIT,
                    ErrorCategory.TEMPORARY_PROVIDER_ERROR,
                ),
                response_retry_delay(response),
                category=category,
                status_code=response.status_code,
            )
        try:
            data = response.json()
            candidate = data["candidates"][0]
            reason = candidate.get("finishReason")
            if reason in ("SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "RECITATION"):
                raise ProviderError(f"Gemini blocked response: {reason}", category=ErrorCategory.MODEL_FAILURE)
            if reason == "MAX_TOKENS":
                raise ProviderError("Gemini incomplete response: MAX_TOKENS",
                                    category=ErrorCategory.INCOMPLETE_GENERATION)
            if reason != "STOP":
                raise ProviderError(
                    f"Provider response interrupted: {reason}",
                    True,
                    category=ErrorCategory.TEMPORARY_PROVIDER_ERROR,
                )
            text = "".join(
                part.get("text", "")
                for part in candidate["content"]["parts"]
                if not part.get("thought")
            )
            if not text.strip():
                raise ValueError("Empty response")
            value = json.loads(text)
            if not isinstance(value, dict):
                raise ValueError("Structured response must be an object")
            value["__response_meta__"] = {
                "finish_reason": reason,
                "input_token_count": data.get("usageMetadata", {}).get("promptTokenCount"),
                "output_token_count": data.get("usageMetadata", {}).get("candidatesTokenCount"),
            }
            return value
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise ProviderError(
                "Invalid/empty provider response",
                True,
                category=ErrorCategory.TEMPORARY_PROVIDER_ERROR,
            ) from exc
