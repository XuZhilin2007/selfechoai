from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from pydantic import ValidationError

from app.schemas import AIExtraction, PersonalItemPublic
from app.services.ai import (
    AIAPIError,
    AIConfigurationError,
    AIInvalidOutputError,
    AINetworkError,
    AIService,
    INSTRUCTIONS,
)


logger = logging.getLogger(__name__)

_DURATION_NUMBER = r"(?:\d+(?:\.\d+)?|[零一二两三四五六七八九十]+)"
_APPROXIMATE_PREFIX = re.compile(r"^(?:约|大约|大概|预计|差不多)")
_UNKNOWN_DURATION_VALUES = frozenset(
    {"", "未知", "不确定", "无法判断", "待确认", "暂无", "unknown", "null", "none"}
)
_CHINESE_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


DEEPSEEK_JSON_INSTRUCTIONS = f"""
{INSTRUCTIONS}

Return exactly one valid json object and no markdown. The json must match one of
these shapes. Omit unchanged fields during an update.

Example for creating an item:
{{
  "fields": {{
    "title": "复习高数",
    "type": "study",
    "importance": "unknown",
    "urgency": "unknown",
    "deadline": null,
    "estimated_time": null,
    "status": "active",
    "next_action": null,
    "extra_information": null
  }},
  "evidence_fields": [],
  "reminder": {{
    "intent": false,
    "temporal_expression": null
  }}
}}

Context-preserving decision example for latest_user_text
"我想买一个设备，不算特别急，但是担心之后涨价，不知道现在买还是等，"
"预算充足，购买时间不会影响正常生活。":
{{
  "fields": {{
    "title": "购买设备",
    "type": "purchase_decision",
    "importance": "unknown",
    "urgency": "low",
    "deadline": null,
    "estimated_time": null,
    "status": "active",
    "next_action": null,
    "extra_information": {{
      "decision_context": "不确定现在购买还是等待",
      "concerns": ["担心之后涨价"],
      "constraints": ["预算充足", "购买时间不会影响正常生活"]
    }}
  }},
  "evidence_fields": ["urgency"]
}}

Evidence-based priority example for latest_user_text
"补办校园通行证会影响已经确认的住宿安排，必须尽快处理。":
{{
  "fields": {{
    "title": "补办校园通行证",
    "type": "administrative",
    "importance": "high",
    "urgency": "high",
    "deadline": null,
    "estimated_time": null,
    "status": "active",
    "next_action": "尽快办理校园通行证",
    "extra_information": {{
      "dependency": "会影响已经确认的住宿安排"
    }}
  }},
  "evidence_fields": ["importance", "urgency"]
}}

Deadline-without-importance example for latest_user_text
"旅行用品最好在出发五天前买好。":
{{
  "fields": {{
    "title": "购买旅行用品",
    "type": "purchase_decision",
    "importance": "unknown",
    "urgency": "medium",
    "deadline": null,
    "estimated_time": null,
    "status": "active",
    "next_action": null,
    "extra_information": {{"timing_constraint": "出发五天前买好"}}
  }},
  "evidence_fields": ["urgency"]
}}

Generic-category example for latest_user_text
"有空了解一门新课程。":
{{
  "fields": {{
    "title": "了解一门新课程",
    "type": "study",
    "importance": "unknown",
    "urgency": "unknown",
    "deadline": null,
    "estimated_time": null,
    "status": "active",
    "next_action": null,
    "extra_information": null
  }},
  "evidence_fields": []
}}

The dynamic extra_information keys above are illustrative, not mandatory. A
different concise factual representation is valid. For any choice, preserve
the stated options, candidate progress, intended use, and comparison step. For
a project decision, preserve its purpose, requirements, preferred candidate,
possible modifications, uncertainty, and submission goal.

Example for updating only a next action:
{{
  "fields": {{"next_action": "复习第四章"}},
  "evidence_fields": []
}}

Allowed evidence_fields values are "importance", "urgency", and "deadline".
Only include one when facts in latest_user_text directly support that field,
including a conservative inference grounded in consequences, deadlines,
dependencies, external requirements, goals, or impact on stated plans.

Valid optional values use their real JSON types:
{{"deadline": null, "estimated_time": 30, "next_action": null}}
Invalid examples:
{{"deadline": "待定", "estimated_time": "半小时", "next_action": ""}}
""".strip()


class _RepairableOutputError(AIInvalidOutputError):
    """Invalid structured output that can receive one bounded repair attempt."""

    def __init__(self, technical_message: str, raw_output: str) -> None:
        super().__init__(technical_message)
        self.raw_output = raw_output


class DeepSeekProvider(AIService):
    """DeepSeek Chat Completions adapter using the official JSON Output mode."""

    DEFAULT_API_URL = "https://api.deepseek.com"
    DEFAULT_MODEL = "deepseek-v4-flash"
    RETIRED_MODEL_ALIASES = frozenset({"deepseek-chat", "deepseek-reasoner"})
    MAX_TOKENS = 2_048

    def __init__(
        self,
        api_url: str,
        api_key: str,
        model: str = DEFAULT_MODEL,
        timeout_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
        debug_output: bool = False,
        today_provider: Callable[[], date] | None = None,
    ) -> None:
        if not api_url or not api_key or not model:
            raise AIConfigurationError(
                "DEEPSEEK_API_URL, DEEPSEEK_API_KEY and DEEPSEEK_MODEL are required",
                "DeepSeek 未配置：请设置 DEEPSEEK_API_KEY，确认 "
                "DEEPSEEK_MODEL 和 DEEPSEEK_API_URL 后重启服务。",
            )
        if model in self.RETIRED_MODEL_ALIASES:
            raise AIConfigurationError(
                f"retired DeepSeek model alias: {model}",
                "DeepSeek 模型名已退役：请使用 deepseek-v4-flash，"
                "不要使用 deepseek-chat 或 deepseek-reasoner。",
            )
        self.api_url = self._chat_completions_url(api_url)
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._client = client
        self.debug_output = debug_output
        self._today_provider = today_provider or date.today

    async def extract(
        self,
        original_text: str,
        existing_item: PersonalItemPublic | None,
    ) -> AIExtraction:
        current_local_date = self._today_provider()
        context: dict[str, Any] = {
            "operation": "update" if existing_item is not None else "create",
            "current_local_date": current_local_date.isoformat(),
            "latest_user_text": original_text,
            "existing_item": (
                existing_item.model_dump(mode="json")
                if existing_item is not None
                else None
            ),
        }
        base_messages = [
            {"role": "system", "content": DEEPSEEK_JSON_INSTRUCTIONS},
            {
                "role": "user",
                "content": json.dumps(context, ensure_ascii=False),
            },
        ]
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": base_messages,
            "response_format": {"type": "json_object"},
            "max_tokens": self.MAX_TOKENS,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        for attempt in range(2):
            response = await self._post(payload, headers)
            try:
                return self._parse_extraction(
                    response,
                    original_text=original_text,
                    current_local_date=current_local_date,
                    is_update=existing_item is not None,
                )
            except _RepairableOutputError as exc:
                if attempt == 1:
                    raise AIInvalidOutputError(str(exc)) from exc
                payload = {
                    **payload,
                    "messages": [
                        *base_messages,
                        {
                            "role": "assistant",
                            "content": exc.raw_output[:12_000],
                        },
                        {
                            "role": "user",
                            "content": (
                                "The previous structured output was invalid. "
                                f"Validation error: {str(exc)[:2_000]}. "
                                "Correct only the format/types while preserving "
                                "facts supported by latest_user_text. Return one "
                                "corrected JSON object only."
                            ),
                        },
                    ],
                }

        raise AIInvalidOutputError("DeepSeek structured output repair failed")

    async def _post(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> httpx.Response:
        try:
            if self._client is not None:
                return await self._client.post(
                    self.api_url, json=payload, headers=headers
                )
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                return await client.post(
                    self.api_url, json=payload, headers=headers
                )
        except httpx.TimeoutException as exc:
            raise AINetworkError(
                "DeepSeek request timed out",
                "无法连接 DeepSeek：请求超时，请检查网络和 DEEPSEEK_API_URL。",
            ) from exc
        except httpx.RequestError as exc:
            raise AINetworkError(
                f"DeepSeek connection failed: {type(exc).__name__}",
                "无法连接 DeepSeek：请检查网络和 DEEPSEEK_API_URL，或稍后重试。",
            ) from exc

    def _parse_extraction(
        self,
        response: httpx.Response,
        *,
        original_text: str,
        current_local_date: date,
        is_update: bool,
    ) -> AIExtraction:
        if not response.is_success:
            raise AIAPIError(
                f"DeepSeek HTTP {response.status_code}: "
                f"{self._provider_error_detail(response)}",
                self._http_error_message(response.status_code),
            )

        try:
            response_data = response.json()
        except ValueError as exc:
            self._log_invalid_output(
                failure_kind="response_json_syntax",
                raw_output=response.text,
                errors=[
                    {
                        "path": "$response",
                        "type": "json_invalid",
                        "message": str(exc),
                    }
                ],
            )
            raise _RepairableOutputError(
                "DeepSeek returned a non-JSON success response",
                response.text,
            ) from exc
        if not isinstance(response_data, dict):
            self._log_invalid_output(
                failure_kind="response_type",
                raw_output=json.dumps(response_data, ensure_ascii=False),
                errors=[
                    {
                        "path": "$response",
                        "type": "object_required",
                        "message": "response root must be an object",
                    }
                ],
            )
            raise _RepairableOutputError(
                "DeepSeek response root is not an object",
                json.dumps(response_data, ensure_ascii=False),
            )

        try:
            output_text = self._extract_message_content(response_data)
        except AIInvalidOutputError as exc:
            raw_response = json.dumps(response_data, ensure_ascii=False)
            self._log_invalid_output(
                failure_kind="chat_completion_shape",
                raw_output=raw_response,
                errors=[
                    {
                        "path": "$response",
                        "type": "chat_completion_shape",
                        "message": str(exc),
                    }
                ],
            )
            raise _RepairableOutputError(str(exc), raw_response) from exc

        try:
            decoded_output = json.loads(output_text)
        except json.JSONDecodeError as exc:
            self._log_invalid_output(
                failure_kind="json_syntax",
                raw_output=output_text,
                errors=[
                    {
                        "path": f"line {exc.lineno}, column {exc.colno}",
                        "type": "json_invalid",
                        "message": exc.msg,
                    }
                ],
            )
            raise _RepairableOutputError(
                "DeepSeek output JSON syntax failed at "
                f"line {exc.lineno}, column {exc.colno}: {exc.msg}",
                output_text,
            ) from exc

        self._normalize_optional_empty_values(
            decoded_output,
            is_update=is_update,
        )
        self._normalize_estimated_time(
            decoded_output,
            is_update=is_update,
        )
        self._normalize_deadline(
            decoded_output,
            original_text=original_text,
            current_local_date=current_local_date,
            is_update=is_update,
        )
        self._normalize_reminder_candidate(decoded_output)

        try:
            extraction = AIExtraction.model_validate(decoded_output)
        except ValidationError as exc:
            errors = self._validation_errors(exc)
            self._log_invalid_output(
                failure_kind="schema_validation",
                raw_output=output_text,
                errors=errors,
            )
            raise _RepairableOutputError(
                "DeepSeek structured output schema validation failed: "
                f"{json.dumps(errors, ensure_ascii=False)}",
                output_text,
            ) from exc

        if not is_update and extraction.fields.title is None:
            errors = [
                {
                    "path": "fields.title",
                    "type": "missing",
                    "message": "title is required when creating a new item",
                }
            ]
            self._log_invalid_output(
                failure_kind="create_requirement",
                raw_output=output_text,
                errors=errors,
            )
            raise _RepairableOutputError(
                "DeepSeek create output validation failed: fields.title is required",
                output_text,
            )
        return extraction

    @staticmethod
    def _normalize_reminder_candidate(decoded_output: Any) -> None:
        if not isinstance(decoded_output, dict):
            return
        if "reminder" not in decoded_output:
            decoded_output["reminder"] = {
                "intent": False,
                "temporal_expression": None,
            }
            return
        reminder = decoded_output.get("reminder")
        if not isinstance(reminder, dict):
            return
        expression = reminder.get("temporal_expression")
        if isinstance(expression, str):
            reminder["temporal_expression"] = expression.strip() or None
        if reminder.get("intent") is False:
            reminder["temporal_expression"] = None

    @staticmethod
    def _normalize_optional_empty_values(
        decoded_output: Any,
        *,
        is_update: bool,
    ) -> None:
        if not isinstance(decoded_output, dict):
            return
        fields = decoded_output.get("fields")
        if not isinstance(fields, dict):
            return
        for name in ("deadline", "estimated_time", "next_action", "extra_information"):
            value = fields.get(name)
            if not isinstance(value, str) or value.strip():
                continue
            if is_update:
                fields.pop(name, None)
            else:
                fields[name] = None

    @classmethod
    def _normalize_deadline(
        cls,
        decoded_output: Any,
        *,
        original_text: str,
        current_local_date: date,
        is_update: bool,
    ) -> None:
        if not isinstance(decoded_output, dict):
            return
        fields = decoded_output.get("fields")
        if not isinstance(fields, dict):
            return
        evidence = decoded_output.get("evidence_fields")
        deadline_is_claimed = "deadline" in fields or (
            isinstance(evidence, list) and "deadline" in evidence
        )

        exact_date: date | None = None
        explicit = re.search(
            r"(?<!\d)(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*(?:日|号)?",
            original_text,
        )
        if explicit:
            exact_date = cls._safe_date(*map(int, explicit.groups()))
        else:
            next_year_exact = re.search(
                r"明年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*(?:日|号)?",
                original_text,
            )
            if next_year_exact:
                month, day_value = map(int, next_year_exact.groups())
                exact_date = cls._safe_date(
                    current_local_date.year + 1,
                    month,
                    day_value,
                )
            elif "明天" in original_text:
                exact_date = current_local_date + timedelta(days=1)
            else:
                next_week = re.search(r"下周([一二三四五六日天])", original_text)
                if next_week:
                    weekday = {
                        "一": 0,
                        "二": 1,
                        "三": 2,
                        "四": 3,
                        "五": 4,
                        "六": 5,
                        "日": 6,
                        "天": 6,
                    }[next_week.group(1)]
                    next_monday = current_local_date + timedelta(
                        days=7 - current_local_date.weekday()
                    )
                    exact_date = next_monday + timedelta(days=weekday)
                else:
                    text_without_explicit_year = re.sub(
                        r"(?:\d{4}\s*年|明年)\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*(?:日|号)?)?",
                        "",
                        original_text,
                    )
                    yearless = re.search(
                        r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*(?:日|号)?",
                        text_without_explicit_year,
                    )
                    if yearless and cls._has_future_intent(original_text):
                        month, day_value = map(int, yearless.groups())
                        candidate = cls._safe_date(
                            current_local_date.year,
                            month,
                            day_value,
                        )
                        if candidate is not None and candidate < current_local_date:
                            candidate = cls._safe_date(
                                current_local_date.year + 1,
                                month,
                                day_value,
                            )
                        exact_date = candidate

        if exact_date is not None and deadline_is_claimed:
            fields["deadline"] = exact_date.isoformat()
            return

        partial = cls._partial_date_text(original_text)
        if partial is None:
            return
        cls._preserve_date_context(fields, partial)
        if is_update:
            fields.pop("deadline", None)
        else:
            fields["deadline"] = None

    @staticmethod
    def _safe_date(year: int, month: int, day_value: int) -> date | None:
        try:
            return date(year, month, day_value)
        except ValueError:
            return None

    @staticmethod
    def _has_future_intent(original_text: str) -> bool:
        if re.search(r"去年|前年|过去|当时|曾经", original_text):
            return False
        return bool(
            re.search(
                r"截止|之前|以前|\d\s*(?:日|号)?\s*前|"
                r"前(?:提交|完成|购买|发送|交付)|ddl|deadline|"
                r"计划|准备|需要|要|将|未来|到期",
                original_text,
                re.IGNORECASE,
            )
        )

    @staticmethod
    def _partial_date_text(original_text: str) -> str | None:
        without_exact_dates = re.sub(
            r"(?:\d{4}\s*年|明年)?\s*\d{1,2}\s*月\s*\d{1,2}\s*(?:日|号)?",
            "",
            original_text,
        )
        match = re.search(
            r"(?:(?:\d{4}\s*年|明年)\s*)?\d{1,2}\s*月(?:份)?",
            without_exact_dates,
        )
        return re.sub(r"\s+", "", match.group(0)) if match else None

    @staticmethod
    def _preserve_date_context(fields: dict[str, Any], date_text: str) -> None:
        extra = fields.get("extra_information")
        if extra is None:
            extra = {}
            fields["extra_information"] = extra
        if isinstance(extra, dict):
            extra.setdefault("date_context", date_text)

    @classmethod
    def _normalize_estimated_time(
        cls,
        decoded_output: Any,
        *,
        is_update: bool,
    ) -> None:
        if not isinstance(decoded_output, dict):
            return
        fields = decoded_output.get("fields")
        if not isinstance(fields, dict) or "estimated_time" not in fields:
            return

        value = fields["estimated_time"]
        if isinstance(value, int) and not isinstance(value, bool):
            return
        if value is None or not isinstance(value, str):
            return

        minutes = cls._parse_duration_minutes(value)
        if minutes is not None:
            fields["estimated_time"] = minutes
            return

        # Unknown must not erase an existing estimate during a progressive update.
        if is_update:
            fields.pop("estimated_time", None)
        else:
            fields["estimated_time"] = None

    @classmethod
    def _parse_duration_minutes(cls, value: str) -> int | None:
        text = re.sub(r"\s+", "", value.strip().lower())
        text = _APPROXIMATE_PREFIX.sub("", text)
        if text in _UNKNOWN_DURATION_VALUES:
            return None
        if text in {"半小时", "半个小时"}:
            return 30

        plain_minutes = re.fullmatch(rf"({_DURATION_NUMBER})(?:分钟|分|min|mins)", text)
        if plain_minutes:
            return cls._safe_minutes(cls._parse_number(plain_minutes.group(1)))

        # A unitless string is safe here because estimated_time is defined in minutes.
        unitless_minutes = re.fullmatch(r"\d+", text)
        if unitless_minutes:
            return cls._safe_minutes(Decimal(unitless_minutes.group(0)))

        hours_and_minutes = re.fullmatch(
            rf"({_DURATION_NUMBER})(?:个)?小时(?:({_DURATION_NUMBER})分钟)?",
            text,
        )
        if hours_and_minutes:
            hours = cls._parse_number(hours_and_minutes.group(1))
            minutes = cls._parse_number(hours_and_minutes.group(2) or "0")
            if hours is None or minutes is None:
                return None
            return cls._safe_minutes(hours * 60 + minutes)

        hours_and_half = re.fullmatch(
            rf"({_DURATION_NUMBER})(?:个)?小时半",
            text,
        )
        if hours_and_half:
            hours = cls._parse_number(hours_and_half.group(1))
            if hours is None:
                return None
            return cls._safe_minutes(hours * 60 + 30)
        return None

    @staticmethod
    def _parse_number(value: str) -> Decimal | None:
        try:
            return Decimal(value)
        except InvalidOperation:
            pass

        if value == "十":
            return Decimal(10)
        if "十" in value:
            tens_text, units_text = value.split("十", 1)
            tens = _CHINESE_DIGITS.get(tens_text) if tens_text else 1
            units = _CHINESE_DIGITS.get(units_text) if units_text else 0
            if tens is None or units is None:
                return None
            return Decimal(tens * 10 + units)
        if len(value) == 1 and value in _CHINESE_DIGITS:
            return Decimal(_CHINESE_DIGITS[value])
        return None

    @staticmethod
    def _safe_minutes(value: Decimal | None) -> int | None:
        if value is None or value != value.to_integral_value():
            return None
        minutes = int(value)
        if not 1 <= minutes <= 100_800:
            return None
        return minutes

    @staticmethod
    def _validation_errors(exc: ValidationError) -> list[dict[str, str]]:
        errors: list[dict[str, str]] = []
        for error in exc.errors(include_input=False, include_url=False):
            location = error.get("loc", ())
            path = ".".join(str(part) for part in location) or "$"
            errors.append(
                {
                    "path": path,
                    "type": str(error.get("type", "unknown")),
                    "message": str(error.get("msg", "validation failed")),
                }
            )
        return errors

    def _log_invalid_output(
        self,
        *,
        failure_kind: str,
        raw_output: str,
        errors: list[dict[str, str]],
    ) -> None:
        if not self.debug_output:
            return
        # Development-only diagnostic. It is intentionally never returned by the
        # API or persisted in Personal Item / Item Input records.
        logger.error(
            "AI_DEBUG_OUTPUT provider=deepseek failure_kind=%s "
            "validation_errors=%s raw_model_output=%s",
            failure_kind,
            json.dumps(errors, ensure_ascii=False),
            raw_output,
        )

    @staticmethod
    def _chat_completions_url(api_url: str) -> str:
        normalized = api_url.rstrip("/")
        if normalized.endswith("/chat/completions"):
            return normalized
        return f"{normalized}/chat/completions"

    @staticmethod
    def _extract_message_content(response_data: dict[str, Any]) -> str:
        choices = response_data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise AIInvalidOutputError("DeepSeek response did not contain choices")
        choice = choices[0]
        if not isinstance(choice, dict):
            raise AIInvalidOutputError("DeepSeek first choice is not an object")

        finish_reason = choice.get("finish_reason")
        if finish_reason == "length":
            raise AIInvalidOutputError(
                "DeepSeek JSON output was truncated at max_tokens"
            )
        if finish_reason in {
            "content_filter",
            "tool_calls",
            "insufficient_system_resource",
        }:
            raise AIAPIError(
                f"DeepSeek finish_reason={finish_reason}",
                "DeepSeek 未返回可用的事项结果，请稍后重试。",
            )

        message = choice.get("message")
        if not isinstance(message, dict):
            raise AIInvalidOutputError("DeepSeek choice did not contain a message")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise AIInvalidOutputError(
                "DeepSeek message content was empty or not text"
            )
        return content

    @staticmethod
    def _provider_error_detail(response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return response.reason_phrase or "unknown provider error"
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            error = body["error"]
            code = str(error.get("code") or error.get("type") or "unknown")
            message = str(error.get("message") or "unknown error")
            return f"{code}: {message[:500]}"
        return "unknown provider error"

    @staticmethod
    def _http_error_message(status_code: int) -> str:
        if status_code in {401, 403}:
            return "DeepSeek 认证失败：请检查 DEEPSEEK_API_KEY。"
        if status_code == 402:
            return "DeepSeek 账户余额不足：请检查账户余额后重试。"
        if status_code == 429:
            return "DeepSeek 服务限流：请稍后重试。"
        if status_code in {400, 404, 422}:
            return (
                "DeepSeek 拒绝请求：请检查 DEEPSEEK_MODEL、"
                "DEEPSEEK_API_URL 和 JSON Output 支持。"
            )
        if status_code >= 500:
            return "DeepSeek 服务暂时不可用，请稍后重试。"
        return "DeepSeek API 请求失败，请检查配置后重试。"
