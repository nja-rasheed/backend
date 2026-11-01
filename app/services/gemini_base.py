"""
Production-oriented Gemini client with model discovery diagnostics, structured
JSON output, schema-less JSON fallback, bounded retries and key rotation.

Environment variables:
    GEMINI_API_KEYS                  Comma-separated API keys
    GEMINI_API_KEY                   Single API key
    GEMINI_API_KEY_1 ... _9          Numbered fallback keys
    GEMINI_PRIORITY_MODELS           Comma-separated model IDs
                                     (default starts with gemini-3.8-flash)
    GEMINI_REQUEST_TIMEOUT_SECONDS   Per-request timeout (default 20)
    GEMINI_TOTAL_TIMEOUT_SECONDS     Per-task deadline (default 90)
    GEMINI_TIMEOUT_RETRIES           Same-model timeout retries (default 1)
    GEMINI_SERVER_ERROR_RETRIES      Same-model 5xx retries (default 1)
    GEMINI_RETRY_BASE_DELAY_SECONDS  Retry delay (default 0.75)
    GEMINI_DIAGNOSTICS               Set to 1 to run model/key diagnostics
"""

import asyncio
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Type, Tuple

from google import genai
from google.genai import types
from google.genai.errors import APIError
from pydantic import BaseModel, ValidationError

from app.config import settings


logger = logging.getLogger("agent.pipeline")


def _env_float(name: str, default: float) -> float:
    try:
        return max(0.1, float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


GEMINI_REQUEST_TIMEOUT_SECONDS = _env_float(
    "GEMINI_REQUEST_TIMEOUT_SECONDS", 20
)
GEMINI_TOTAL_TIMEOUT_SECONDS = _env_float(
    "GEMINI_TOTAL_TIMEOUT_SECONDS", 90
)
GEMINI_TIMEOUT_RETRIES = _env_int("GEMINI_TIMEOUT_RETRIES", 1)
GEMINI_SERVER_ERROR_RETRIES = _env_int(
    "GEMINI_SERVER_ERROR_RETRIES", 1
)
GEMINI_RETRY_BASE_DELAY_SECONDS = _env_float(
    "GEMINI_RETRY_BASE_DELAY_SECONDS", 0.75
)
GEMINI_DIAGNOSTICS = os.getenv("GEMINI_DIAGNOSTICS", "0").lower() in {
    "1", "true", "yes", "on"
}

# Client-side pacing reduces bursts. These values should be tuned to the
# actual RPM/TPM limits shown for the Google AI Studio project.
GEMINI_MIN_REQUEST_INTERVAL_SECONDS = _env_float(
    "GEMINI_MIN_REQUEST_INTERVAL_SECONDS", 1.5
)
GEMINI_MAX_CONCURRENT_REQUESTS = max(
    1, _env_int("GEMINI_MAX_CONCURRENT_REQUESTS", 2)
)
GEMINI_RATE_LIMIT_COOLDOWN_SECONDS = _env_float(
    "GEMINI_RATE_LIMIT_COOLDOWN_SECONDS", 60
)

# Current stable Gemini Flash model IDs. The first model is the primary
# model; the remaining models are fallbacks if the primary is unavailable.
DEFAULT_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.6-flash",
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.1-flash-lite",
]

PRIORITY_MODELS = [
    item.strip()
    for item in os.getenv(
        "GEMINI_PRIORITY_MODELS", ",".join(DEFAULT_MODELS)
    ).split(",")
    if item.strip()
]


class GeminiFastClient:
    def __init__(self) -> None:
        raw_keys = (
            getattr(settings, "GEMINI_API_KEYS", "")
            or getattr(settings, "GEMINI_API_KEY", "")
            or ""
        )

        # Support Pydantic SecretStr settings, if used by app.config.
        if hasattr(raw_keys, "get_secret_value"):
            raw_keys = raw_keys.get_secret_value()

        self.api_keys: List[str] = [
            item.strip()
            for item in str(raw_keys).split(",")
            if item and item.strip()
        ]

        # Numbered environment keys are used when the settings value is empty.
        if not self.api_keys:
            for index in range(1, 10):
                key = os.getenv(f"GEMINI_API_KEY_{index}", "").strip()
                if key:
                    self.api_keys.append(key)

        # De-duplicate while preserving configured order.
        self.api_keys = list(dict.fromkeys(self.api_keys))
        self.total_keys = len(self.api_keys)
        self.current_key_idx = 0
        self._rotation_lock = threading.Lock()

        # Model availability is cached per key for this process.
        self._model_check_cache: Dict[Tuple[int, str], Tuple[bool, str]] = {}
        self._model_cache_lock = threading.Lock()

        # Rate limiting is model/project-aware, not key-aware. Multiple API
        # keys from the same Google Cloud project generally share project
        # quotas, so rotating keys after HTTP 429 only creates more traffic.
        self._model_cooldowns: Dict[str, float] = {}
        self._cooldown_lock = threading.Lock()
        self._pacing_lock = asyncio.Lock()
        self._last_request_started = 0.0
        self._request_semaphore = asyncio.Semaphore(
            GEMINI_MAX_CONCURRENT_REQUESTS
        )

        self.client = None
        if self.api_keys:
            self._init_client()
            logger.info(
                "[GeminiClient] Loaded %s API key(s). Active key: %s",
                self.total_keys,
                self._mask_key(0),
            )
            print(
                f"[GeminiClient] Loaded {self.total_keys} API key(s). "
                f"Active Key #0 ({self._mask_key(0)})"
            )
        else:
            logger.error(
                "[GeminiClient] No API keys found. Check GEMINI_API_KEYS "
                "or GEMINI_API_KEY in your environment/configuration."
            )
            print("[GeminiClient] ERROR: No Gemini API keys found.")

    def _mask_key(self, index: int) -> str:
        if not 0 <= index < self.total_keys:
            return "N/A"
        key = self.api_keys[index]
        return f"{key[:5]}...{key[-4:]}" if len(key) > 10 else "******"

    def _init_client(self) -> None:
        """Maintain the old self.client attribute for compatibility."""
        if self.api_keys:
            self.client = genai.Client(api_key=self.api_keys[self.current_key_idx])
        else:
            self.client = None

    def _get_request_client(self, key_idx: int):
        """Each request uses a client pinned to its own API key."""
        return genai.Client(api_key=self.api_keys[key_idx])

    def _get_active_key_idx(self) -> int:
        with self._rotation_lock:
            return self.current_key_idx

    def _rotate_key(self, failed_key_idx: Optional[int] = None) -> int:
        if self.total_keys <= 1:
            return 0

        with self._rotation_lock:
            previous = (
                self.current_key_idx
                if failed_key_idx is None
                else failed_key_idx
            )
            next_index = (previous + 1) % self.total_keys
            self.current_key_idx = next_index

        logger.warning(
            "[GeminiClient] Rotated active key #%s -> #%s (%s)",
            previous,
            next_index,
            self._mask_key(next_index),
        )
        print(
            f"[GeminiClient] Rotated Active Key: Key #{previous} -> "
            f"Key #{next_index} ({self._mask_key(next_index)})"
        )
        self._init_client()
        return next_index

    @staticmethod
    def _error_code(error: APIError) -> Optional[int]:
        value = getattr(error, "code", None)
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _schema_for_gemini(schema: Type[BaseModel]) -> dict:
        """
        Convert Pydantic JSON Schema into a conservative Gemini schema.

        Do not reject a model merely because Pydantic omitted
        `additionalProperties`. We validate the final JSON locally with
        Pydantic regardless of whether Gemini's schema constraint is used.
        """
        raw_schema = schema.model_json_schema()

        unsupported_keys = {
            "additionalProperties",
            "unevaluatedProperties",
        }

        def clean(value: Any) -> Any:
            if isinstance(value, dict):
                return {
                    key: clean(item)
                    for key, item in value.items()
                    if key not in unsupported_keys
                }
            if isinstance(value, list):
                return [clean(item) for item in value]
            return value

        return clean(raw_schema)

    @staticmethod
    def _clean_json_text(value: str) -> str:
        text = (value or "").strip()
        if text.startswith("```json"):
            text = text[len("```json"):]
        elif text.startswith("```"):
            text = text[len("```"):]
        if text.endswith("```"):
            text = text[:-3]
        return text.strip()

    @staticmethod
    def _extract_response_text(response: Any) -> str:
        """
        Read response.text safely. The SDK can raise when candidates are
        blocked or no text part exists, so callers must not assume text exists.
        """
        try:
            text = getattr(response, "text", None)
            if text:
                return str(text).strip()
        except Exception:
            pass

        # Fallback to candidate content parts.
        try:
            candidates = getattr(response, "candidates", None) or []
            for candidate in candidates:
                content = getattr(candidate, "content", None)
                parts = getattr(content, "parts", None) or []
                fragments = []
                for part in parts:
                    part_text = getattr(part, "text", None)
                    if part_text:
                        fragments.append(str(part_text))
                if fragments:
                    return "\n".join(fragments).strip()
        except Exception:
            pass

        return ""

    @staticmethod
    def _response_diagnostics(response: Any) -> str:
        """Return useful finish/block metadata without logging prompt content."""
        details = []

        try:
            prompt_feedback = getattr(response, "prompt_feedback", None)
            if prompt_feedback:
                block_reason = getattr(prompt_feedback, "block_reason", None)
                if block_reason:
                    details.append(f"prompt_block_reason={block_reason}")
                ratings = getattr(prompt_feedback, "safety_ratings", None)
                if ratings:
                    details.append(f"prompt_safety_ratings={ratings}")
        except Exception:
            pass

        try:
            candidates = getattr(response, "candidates", None) or []
            if not candidates:
                details.append("candidate_count=0")
            for index, candidate in enumerate(candidates):
                finish_reason = getattr(candidate, "finish_reason", None)
                if finish_reason:
                    details.append(
                        f"candidate[{index}].finish_reason={finish_reason}"
                    )
                finish_message = getattr(candidate, "finish_message", None)
                if finish_message:
                    details.append(
                        f"candidate[{index}].finish_message={finish_message}"
                    )
                ratings = getattr(candidate, "safety_ratings", None)
                if ratings:
                    details.append(
                        f"candidate[{index}].safety_ratings={ratings}"
                    )
        except Exception as error:
            details.append(f"diagnostic_read_error={type(error).__name__}")

        try:
            usage = getattr(response, "usage_metadata", None)
            if usage:
                details.append(f"usage={usage}")
        except Exception:
            pass

        return "; ".join(details) if details else "No finish/block metadata supplied"

    @staticmethod
    def _is_schema_error(message: str) -> bool:
        lowered = (message or "").lower()
        markers = (
            "response_schema",
            "response schema",
            "schema",
            "additionalproperties",
            "additional_properties",
            "json schema",
            "invalid argument",
            "unknown name",
            "not supported for",
        )
        return any(marker in lowered for marker in markers)

    async def _generate(
        self,
        request_client: Any,
        model_name: str,
        prompt: str,
        config: types.GenerateContentConfig,
        timeout: float,
    ) -> Any:
        return await asyncio.wait_for(
            request_client.aio.models.generate_content(
                model=model_name,
                contents=prompt,
                config=config,
            ),
            timeout=timeout,
        )

    def _set_model_cooldown(
        self, model_name: str, seconds: Optional[float] = None
    ) -> None:
        cooldown = seconds or GEMINI_RATE_LIMIT_COOLDOWN_SECONDS
        until = time.monotonic() + max(1.0, cooldown)
        with self._cooldown_lock:
            self._model_cooldowns[model_name] = max(
                until, self._model_cooldowns.get(model_name, 0.0)
            )
        logger.warning(
            "[GeminiClient] Model %s paused for %.1fs after rate limiting.",
            model_name,
            cooldown,
        )

    def _model_cooldown_remaining(self, model_name: str) -> float:
        with self._cooldown_lock:
            until = self._model_cooldowns.get(model_name, 0.0)
        return max(0.0, until - time.monotonic())

    async def _wait_for_request_slot(
        self, model_name: str, deadline: float
    ) -> bool:
        """
        Enforce a process-wide minimum request interval and model cooldown.
        Returns False if the caller's total deadline would be exceeded.
        """
        cooldown = self._model_cooldown_remaining(model_name)
        if cooldown:
            if time.monotonic() + cooldown >= deadline:
                logger.info(
                    "Skipping %s: rate-limit cooldown (%.1fs) exceeds "
                    "remaining task deadline.",
                    model_name,
                    cooldown,
                )
                return False
            logger.info(
                "Waiting %.1fs before retrying rate-limited model %s.",
                cooldown,
                model_name,
            )
            await asyncio.sleep(cooldown)

        async with self._pacing_lock:
            now = time.monotonic()
            wait_for = max(
                0.0,
                self._last_request_started
                + GEMINI_MIN_REQUEST_INTERVAL_SECONDS
                - now,
            )
            if now + wait_for >= deadline:
                return False
            if wait_for:
                await asyncio.sleep(wait_for)
            if time.monotonic() >= deadline:
                return False
            self._last_request_started = time.monotonic()
        return True

    async def _throttled_generate(
        self,
        request_client: Any,
        model_name: str,
        prompt: str,
        config: types.GenerateContentConfig,
        timeout: float,
        deadline: float,
    ) -> Any:
        if not await self._wait_for_request_slot(model_name, deadline):
            raise asyncio.TimeoutError("No time left after Gemini rate-limit pacing.")

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError("Gemini task deadline exhausted.")

        # Bound concurrent requests across all tasks using this client.
        await asyncio.wait_for(
            self._request_semaphore.acquire(),
            timeout=remaining,
        )
        try:
            return await self._generate(
                request_client=request_client,
                model_name=model_name,
                prompt=prompt,
                config=config,
                timeout=min(timeout, max(0.1, deadline - time.monotonic())),
            )
        finally:
            self._request_semaphore.release()

    async def diagnose_models(self, prompt: str = "Reply with the word OK.") -> dict:
        """
        Explicit diagnostic: checks configured keys/models with a tiny request.
        Call this from a temporary admin/debug endpoint or a one-off script.
        It does not expose API keys or user prompts.
        """
        report = {
            "keys_configured": self.total_keys,
            "models_configured": list(PRIORITY_MODELS),
            "results": [],
        }

        if not self.api_keys:
            report["error"] = "No Gemini API keys configured."
            return report

        for model_name in PRIORITY_MODELS:
            model_result = {
                "model": model_name,
                "key_results": [],
                "working": False,
            }

            for key_idx in range(self.total_keys):
                try:
                    client = self._get_request_client(key_idx)
                    start = time.perf_counter()
                    response = await asyncio.wait_for(
                        client.aio.models.generate_content(
                            model=model_name,
                            contents=prompt,
                            config=types.GenerateContentConfig(
                                max_output_tokens=16,
                            ),
                        ),
                        timeout=min(12, GEMINI_REQUEST_TIMEOUT_SECONDS),
                    )
                    elapsed = time.perf_counter() - start
                    answer = self._extract_response_text(response)
                    result = {
                        "key": self._mask_key(key_idx),
                        "ok": bool(answer),
                        "latency_seconds": round(elapsed, 3),
                        "response_received": bool(answer),
                    }
                    if not answer:
                        result["diagnostic"] = self._response_diagnostics(response)
                    model_result["key_results"].append(result)
                    if answer:
                        model_result["working"] = True
                        break

                except asyncio.TimeoutError:
                    model_result["key_results"].append({
                        "key": self._mask_key(key_idx),
                        "ok": False,
                        "error": "Request timed out",
                    })
                except APIError as error:
                    error_code = self._error_code(error)
                    model_result["key_results"].append({
                        "key": self._mask_key(key_idx),
                        "ok": False,
                        "http_code": error_code,
                        "error": str(getattr(error, "message", error))[:500],
                    })
                    if error_code == 404:
                        # The model is unavailable, regardless of which key is used.
                        break
                except Exception as error:
                    model_result["key_results"].append({
                        "key": self._mask_key(key_idx),
                        "ok": False,
                        "error_type": type(error).__name__,
                        "error": str(error)[:500],
                    })

            report["results"].append(model_result)

        return report

    async def execute_structured_query(
        self,
        task_name: str,
        prompt: str,
        system_instruction: str,
        schema: Type[BaseModel],
        use_search_grounding: bool = False,
    ) -> Optional[Any]:
        if not self.api_keys or not self.client:
            logger.error(
                "[%s] Gemini unavailable: no API keys configured. "
                "Check GEMINI_API_KEYS/GEMINI_API_KEY.",
                task_name,
            )
            return None

        if not PRIORITY_MODELS:
            logger.error("[%s] Gemini model list is empty.", task_name)
            return None

        clean_prompt = (prompt or "").strip()
        if not clean_prompt:
            logger.error("[%s] Gemini prompt is empty.", task_name)
            return None

        try:
            response_schema = self._schema_for_gemini(schema)
        except Exception as error:
            logger.exception(
                "[%s] Could not build Pydantic JSON schema for %s: %s",
                task_name,
                getattr(schema, "__name__", str(schema)),
                error,
            )
            return None

        # Gemini 3.x does not support legacy sampling parameters such as
        # temperature/top_p/top_k. Do not send them in the request config.
        base_config: Dict[str, Any] = {
            "system_instruction": system_instruction or None,
            "max_output_tokens": 2048,
            "response_mime_type": "application/json",
        }
        if use_search_grounding:
            base_config["tools"] = [
                types.Tool(google_search=types.GoogleSearch())
            ]

        # First try schema-constrained JSON. If the API rejects the schema,
        # retry the SAME model/key with JSON mode only, then validate locally.
        structured_config = dict(base_config)
        structured_config["response_schema"] = response_schema
        json_only_config = dict(base_config)

        start_time = time.perf_counter()
        deadline = start_time + GEMINI_TOTAL_TIMEOUT_SECONDS

        # Rate limits can be model-specific, so track model/key pairs rather
        # than disabling a key for every model after one 429.
        rate_limited_pairs = set()
        unavailable_models = set()
        attempts = 0

        logger.info(
            "[%s] Gemini started | keys=%s | models=%s | timeout=%.1fs",
            task_name,
            self.total_keys,
            PRIORITY_MODELS,
            GEMINI_TOTAL_TIMEOUT_SECONDS,
        )
        print(
            f"[{task_name}] Gemini started | "
            f"{self.total_keys} key(s) | {len(PRIORITY_MODELS)} model(s)"
        )

        for model_name in PRIORITY_MODELS:
            if time.perf_counter() >= deadline:
                break

            if model_name in unavailable_models:
                continue

            cooldown_remaining = self._model_cooldown_remaining(model_name)
            if cooldown_remaining > 0:
                logger.info(
                    "[%s] Skipping %s: model is cooling down for %.1fs.",
                    task_name,
                    model_name,
                    cooldown_remaining,
                )
                continue

            # Begin with the active key, then visit the remaining configured keys.
            active_idx = self._get_active_key_idx()
            key_order = [
                (active_idx + offset) % self.total_keys
                for offset in range(self.total_keys)
            ]

            for key_idx in key_order:
                if (model_name, key_idx) in rate_limited_pairs:
                    continue
                if time.perf_counter() >= deadline:
                    break

                request_client = self._get_request_client(key_idx)
                timeout_retries = 0
                server_retries = 0
                schema_fallback_used = False
                current_config = types.GenerateContentConfig(**structured_config)

                while time.perf_counter() < deadline:
                    remaining = deadline - time.perf_counter()
                    request_timeout = min(
                        GEMINI_REQUEST_TIMEOUT_SECONDS,
                        remaining,
                    )
                    attempts += 1
                    request_start = time.perf_counter()

                    logger.info(
                        "[%s] Attempt %s | model=%s | key=%s | "
                        "schema_mode=%s | timeout=%.1fs",
                        task_name,
                        attempts,
                        model_name,
                        self._mask_key(key_idx),
                        "json_only" if schema_fallback_used else "structured",
                        request_timeout,
                    )
                    print(
                        f"[{task_name}] Attempt {attempts} | {model_name} | "
                        f"Key #{key_idx} | "
                        f"{'JSON fallback' if schema_fallback_used else 'structured JSON'}"
                    )

                    try:
                        response = await self._throttled_generate(
                            request_client=request_client,
                            model_name=model_name,
                            prompt=clean_prompt,
                            config=current_config,
                            timeout=request_timeout,
                            deadline=deadline,
                        )
                        elapsed = time.perf_counter() - request_start
                        response_text = self._extract_response_text(response)

                        if not response_text:
                            diagnostic = self._response_diagnostics(response)
                            logger.warning(
                                "[%s] Gemini returned no text | model=%s | "
                                "key=%s | %.2fs | %s",
                                task_name,
                                model_name,
                                self._mask_key(key_idx),
                                elapsed,
                                diagnostic,
                            )
                            print(
                                f"[{task_name}] NO TEXT from {model_name}: "
                                f"{diagnostic}"
                            )

                            # If structured output yielded no text, retry once
                            # without response_schema before abandoning the model.
                            if not schema_fallback_used:
                                schema_fallback_used = True
                                current_config = types.GenerateContentConfig(
                                    **json_only_config
                                )
                                continue

                            break

                        cleaned = self._clean_json_text(response_text)
                        try:
                            result = schema.model_validate_json(cleaned)
                        except ValidationError as validation_error:
                            logger.warning(
                                "[%s] Invalid JSON/schema output from %s: %s",
                                task_name,
                                model_name,
                                str(validation_error)[:700],
                            )

                            # One schema-less retry can help when constrained
                            # decoding produced an incompatible shape.
                            if not schema_fallback_used:
                                schema_fallback_used = True
                                current_config = types.GenerateContentConfig(
                                    **json_only_config
                                )
                                continue
                            break

                        total_elapsed = time.perf_counter() - start_time
                        logger.info(
                            "[%s] SUCCESS | model=%s | key=%s | "
                            "request=%.2fs | total=%.2fs",
                            task_name,
                            model_name,
                            self._mask_key(key_idx),
                            elapsed,
                            total_elapsed,
                        )
                        print(
                            f"[{task_name}] SUCCESS via {model_name} "
                            f"| {elapsed:.2f}s request | {total_elapsed:.2f}s total"
                        )
                        return result

                    except asyncio.TimeoutError:
                        elapsed = time.perf_counter() - request_start
                        timeout_retries += 1
                        logger.warning(
                            "[%s] TIMEOUT | model=%s | key=%s | "
                            "elapsed=%.2fs | retry=%s/%s",
                            task_name,
                            model_name,
                            self._mask_key(key_idx),
                            elapsed,
                            timeout_retries,
                            GEMINI_TIMEOUT_RETRIES,
                        )
                        print(
                            f"[{task_name}] TIMEOUT: {model_name} "
                            f"after {elapsed:.2f}s"
                        )

                        if timeout_retries <= GEMINI_TIMEOUT_RETRIES:
                            delay = GEMINI_RETRY_BASE_DELAY_SECONDS
                            if time.perf_counter() + delay < deadline:
                                await asyncio.sleep(delay)
                                continue
                        # Do not rotate keys on timeout. Try the next model.
                        break

                    except APIError as api_error:
                        code = self._error_code(api_error)
                        message = str(
                            getattr(api_error, "message", api_error)
                        )
                        elapsed = time.perf_counter() - request_start

                        logger.error(
                            "[%s] GEMINI API ERROR | model=%s | key=%s | "
                            "HTTP=%s | %.2fs | %s",
                            task_name,
                            model_name,
                            self._mask_key(key_idx),
                            code,
                            elapsed,
                            message[:1000],
                        )
                        print(
                            f"[{task_name}] GEMINI API ERROR {code} "
                            f"| {model_name} | {message[:350]}"
                        )

                        if code == 400:
                            # A schema/config 400 should not kill the whole
                            # pipeline: retry this same model/key without schema.
                            if (
                                not schema_fallback_used
                                and self._is_schema_error(message)
                            ):
                                logger.warning(
                                    "[%s] Structured schema rejected. "
                                    "Retrying same model/key in JSON-only mode.",
                                    task_name,
                                )
                                schema_fallback_used = True
                                current_config = types.GenerateContentConfig(
                                    **json_only_config
                                )
                                continue

                            # A non-schema 400 is usually a bad request/prompt/config.
                            logger.error(
                                "[%s] Non-retryable HTTP 400. Check request "
                                "configuration, tools, prompt and model support.",
                                task_name,
                            )
                            break

                        if code == 404:
                            # A model-level 404 is not key-specific. Trying the
                            # same model with every key only wastes requests.
                            unavailable_models.add(model_name)
                            logger.error(
                                "[%s] Model %s returned HTTP 404 and is "
                                "unavailable to this API project. Skipping "
                                "this model for all configured keys.",
                                task_name,
                                model_name,
                            )
                            print(
                                f"[{task_name}] MODEL UNAVAILABLE: {model_name}; "
                                "moving to the next model."
                            )
                            break

                        if code in (401, 403):
                            # 403 can mean invalid/restricted key OR project/API
                            # permission. Rotate only if error text indicates key issue.
                            lower_message = message.lower()
                            key_problem = (
                                code == 401
                                or "api key" in lower_message
                                or "api_key" in lower_message
                                or "invalid key" in lower_message
                                or "key not valid" in lower_message
                            )
                            if key_problem and self.total_keys > 1:
                                self._rotate_key(key_idx)
                                break

                            logger.error(
                                "[%s] Permission/authentication failure. "
                                "Check that Generative Language API is enabled, "
                                "the key is valid, and key restrictions allow it. "
                                "Details: %s",
                                task_name,
                                message[:700],
                            )
                            # Try other configured keys for auth failures.
                            if self.total_keys > 1:
                                self._rotate_key(key_idx)
                                break
                            return None

                        if code == 429:
                            rate_limited_pairs.add((model_name, key_idx))

                            # Do not rotate through keys from the same project:
                            # they usually share the same project/model quota.
                            # Pause this model for all concurrent requests and
                            # immediately move to a different configured model.
                            retry_after = GEMINI_RATE_LIMIT_COOLDOWN_SECONDS
                            self._set_model_cooldown(model_name, retry_after)

                            logger.warning(
                                "[%s] HTTP 429 for model=%s. Pausing this "
                                "model for %.1fs; not rotating API keys because "
                                "keys may share the same project quota.",
                                task_name,
                                model_name,
                                retry_after,
                            )
                            break

                        if code is not None and 500 <= code <= 599:
                            if server_retries < GEMINI_SERVER_ERROR_RETRIES:
                                delay = GEMINI_RETRY_BASE_DELAY_SECONDS * (
                                    2 ** server_retries
                                )
                                server_retries += 1
                                if time.perf_counter() + delay < deadline:
                                    await asyncio.sleep(delay)
                                    continue
                            break

                        # Unknown API error: log it and move to fallback model.
                        break

                    except asyncio.CancelledError:
                        logger.warning(
                            "[%s] Gemini request cancelled by caller.",
                            task_name,
                        )
                        raise

                    except Exception as error:
                        logger.exception(
                            "[%s] UNEXPECTED GEMINI CLIENT ERROR | "
                            "model=%s | key=%s | type=%s | error=%s",
                            task_name,
                            model_name,
                            self._mask_key(key_idx),
                            type(error).__name__,
                            str(error)[:1000],
                        )
                        # Do not rotate keys for local programming/network errors.
                        break

                # A 404 means the model itself is unavailable, not that this
                # particular API key failed. Do not retry it with other keys.
                if model_name in unavailable_models:
                    break

        total_elapsed = time.perf_counter() - start_time
        logger.error(
            "[%s] ALL GEMINI ATTEMPTS FAILED | attempts=%s | "
            "elapsed=%.2fs | models=%s | rate_limited_pairs=%s",
            task_name,
            attempts,
            total_elapsed,
            PRIORITY_MODELS,
            len(rate_limited_pairs),
        )
        print(
            f"[{task_name}] FAILED: no valid Gemini response after "
            f"{attempts} attempts ({total_elapsed:.2f}s). "
            "Read the preceding GEMINI API ERROR / TIMEOUT / NO TEXT logs."
        )
        return None


# Preserve the import name used by the rest of the existing backend.
gemini_fast_client = GeminiFastClient()
