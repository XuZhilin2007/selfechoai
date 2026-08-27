from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import date
from typing import Any

import httpx
from pydantic import ValidationError

from app.config import Settings
from app.schemas import AIExtraction, FailureType, PersonalItemPublic


class AIServiceError(Exception):
    """A classified AI failure with a safe message for the local user."""

    category = FailureType.INTERNAL
    default_user_message = "AI 整理出现内部错误，原文已保存，请稍后重试。"

    def __init__(
        self,
        technical_message: str,
        user_message: str | None = None,
    ) -> None:
        super().__init__(technical_message)
        self.user_message = user_message or self.default_user_message


class AIConfigurationError(AIServiceError):
    category = FailureType.CONFIGURATION
    default_user_message = (
        "AI 未配置：请设置 AI_PROVIDER、AI_API_KEY（或 OPENAI_API_KEY）和 "
        "AI_MODEL，重启服务后重试。"
    )


class AINetworkError(AIServiceError):
    category = FailureType.NETWORK
    default_user_message = (
        "无法连接 AI 服务：请检查网络和 AI_API_URL，或稍后重试。"
    )


class AIAPIError(AIServiceError):
    category = FailureType.API
    default_user_message = "AI 服务拒绝或未完成请求，请检查 API 配置后重试。"


class AIInvalidOutputError(AIServiceError):
    category = FailureType.INVALID_OUTPUT
    default_user_message = (
        "AI 返回内容不符合事项格式；请重试，并确认所选模型支持结构化输出。"
    )


class AIService(ABC):
    """Provider-independent business interface for item extraction."""

    @abstractmethod
    async def extract(
        self,
        original_text: str,
        existing_item: PersonalItemPublic | None,
    ) -> AIExtraction:
        raise NotImplementedError


class DisabledAIService(AIService):
    async def extract(
        self,
        original_text: str,
        existing_item: PersonalItemPublic | None,
    ) -> AIExtraction:
        raise AIConfigurationError(
            "AI_PROVIDER is disabled or was not configured"
        )


class MisconfiguredAIService(AIService):
    def __init__(
        self,
        technical_message: str,
        user_message: str | None = None,
    ) -> None:
        self.technical_message = technical_message
        self.user_message = user_message

    async def extract(
        self,
        original_text: str,
        existing_item: PersonalItemPublic | None,
    ) -> AIExtraction:
        raise AIConfigurationError(self.technical_message, self.user_message)


INSTRUCTIONS = """
You extract a Personal Item from user-confirmed text. Return only the requested
structured result. Never invent importance, urgency, or a deadline. Use
"unknown" for importance/urgency when the user's text does not support a value,
and null for missing optional values. Add importance, urgency, or deadline to
evidence_fields only when the latest user text directly supports that field.

Importance and urgency evidence may be explicit labels or a conservative
inference from facts in the user's own words. Relevant evidence includes:
- explicit consequences of doing or not doing the item;
- dependencies or impact on the user's stated plans;
- external requirements and deadlines;
- stated goals and opportunities whose impact the user describes;
- direct time pressure, blockers, or a risk that changes with delay.

Judge importance and urgency separately. A deadline normally supports urgency,
but does not by itself prove importance. A consequence or impact on a stated
goal may support importance, but does not by itself prove immediate urgency.
Use the user's context instead of stereotypes about the item type. Never infer
that purchases are low importance, study is high importance, or any other
generic category has a fixed priority. If the user's words do not provide enough
evidence for one field, keep only that field "unknown". When a supported
inference is made, include that field in evidence_fields because the evidence
still comes from the user's text.

Calibrate each field from the strength of that evidence:
- high: strong direct consequences or impact for importance; explicit immediate
  time pressure or a near hard deadline for urgency;
- medium: meaningful but limited impact, or a real external requirement/time
  constraint that is not clearly immediate;
- low: the user explicitly frames the impact or time pressure as minor,
  deferrable, or not urgent;
- unknown: the text does not support a reliable level.
Do not combine multiple weak generic assumptions to manufacture evidence.

For a new item, provide a concise title, a short type (use "other" when a more
specific type is not reliable), active status, and all facts supported by the
text. For an existing item, return only fields that the latest text explicitly
adds or corrects; omitted fields stay unchanged. Never return unknown to erase a
known value. Preserve unrelated existing extra_information. Add newly supplied
context, and replace or clear existing context only when the user explicitly
corrects or invalidates it. When changing an existing extra_information key,
return its complete updated value so supported facts are not accidentally lost.

If latest_user_text starts with "Reprocess full Item Input history", every
numbered entry that follows is existing user-confirmed input. Re-extract the
complete set of supported AI-derived context from that history so older prompt
omissions can be repaired. This is still an update: do not invent facts or
change lifecycle state, and keep unsupported optional values null/omitted.

Use extra_information to preserve concise, factual context explicitly supplied
by the user when it would help them understand the item later. This can include
reasons and background, concerns and risks, constraints, options or candidates,
trade-offs, explicitly stated uncertainty, current progress or understanding,
dependencies, related projects or objects, goals, and decision context. Use
short JSON strings, arrays, or objects as appropriate. Do not invent context or
copy the entire original input merely to fill this field.

User-provided reasoning is input data and must be preserved when useful. Never
expose or store model-generated chain-of-thought, hidden reasoning, confidence,
or unsupported recommendations. A Personal Item may be a decision, idea, or
exploration rather than a traditional todo. Set next_action when the user's own
text supports a concrete next step; otherwise use null. Do not manufacture an
action merely to make the item task-like. A status change must be directly
stated by the user.

Every numeric field must be a JSON integer, never a string with units or
qualifiers. estimated_time is always an integer number of minutes:
- Correct: "estimated_time": 60
- Wrong: "estimated_time": "一小时"
- Wrong: "estimated_time": "约1小时"

Use these exact enum values:
- importance and urgency: "high", "medium", "low", or "unknown"
- status: "active", "completed", or "trash"
Use JSON null for an optional value that is not supported by the user's text.
Do not use empty strings, "unknown", "待定", or other placeholder text for
optional date, duration, action, or context fields. Return JSON only.

The request includes current_local_date, calculated by the application at
runtime. Ground all dates against it:
1. Preserve an explicit year supplied by the user.
2. When a future task gives month/day without a year, use the next reasonable
   future occurrence; never silently choose a past year.
3. Interpret relative dates such as 明天, 下周三, and 明年6月 relative to
   current_local_date.
4. Never invent missing precision. If only a month is known, leave deadline
   null and preserve that month-level date fact in extra_information.
5. If an exact interpretation is genuinely ambiguous, leave deadline null and
   preserve the user's date wording in extra_information.
""".strip()


class OpenAIResponsesAIService(AIService):
    """OpenAI Responses API adapter, isolated from application business logic."""

    def __init__(
        self,
        api_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float,
        client: httpx.AsyncClient | None = None,
        today_provider: Callable[[], date] | None = None,
    ) -> None:
        if not api_url or not api_key or not model:
            raise AIConfigurationError(
                "AI_API_URL, AI_API_KEY/OPENAI_API_KEY and AI_MODEL are required"
            )
        self.api_url = api_url
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._client = client
        self._today_provider = today_provider or date.today

    async def extract(
        self,
        original_text: str,
        existing_item: PersonalItemPublic | None,
    ) -> AIExtraction:
        context: dict[str, Any] = {
            "operation": "update" if existing_item is not None else "create",
            "current_local_date": self._today_provider().isoformat(),
            "latest_user_text": original_text,
            "existing_item": (
                existing_item.model_dump(mode="json")
                if existing_item is not None
                else None
            ),
        }
        payload = {
            "model": self.model,
            "instructions": INSTRUCTIONS,
            "input": json.dumps(context, ensure_ascii=False),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "personal_item_extraction",
                    "schema": AIExtraction.model_json_schema(),
                    # Server-side Pydantic validation remains authoritative.
                    "strict": False,
                }
            },
            "store": False,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            if self._client is not None:
                response = await self._client.post(
                    self.api_url, json=payload, headers=headers
                )
            else:
                async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                    response = await client.post(
                        self.api_url, json=payload, headers=headers
                    )
        except httpx.TimeoutException as exc:
            raise AINetworkError("AI provider request timed out") from exc
        except httpx.RequestError as exc:
            raise AINetworkError(
                f"AI provider connection failed: {type(exc).__name__}"
            ) from exc

        if not response.is_success:
            detail = self._provider_error_detail(response)
            raise AIAPIError(
                f"AI provider HTTP {response.status_code}: {detail}",
                self._http_error_message(response.status_code),
            )

        try:
            response_data = response.json()
        except ValueError as exc:
            raise AIInvalidOutputError(
                "AI provider returned a non-JSON success response"
            ) from exc
        if not isinstance(response_data, dict):
            raise AIInvalidOutputError("AI provider response root is not an object")

        response_status = response_data.get("status")
        if response_status == "failed":
            raise AIAPIError(
                f"AI response status=failed: {self._response_error_detail(response_data)}"
            )
        if response_status in {"incomplete", "cancelled"}:
            reason = response_data.get("incomplete_details")
            raise AIAPIError(f"AI response status={response_status}: {reason}")

        output_text = self._extract_output_text(response_data)
        try:
            return AIExtraction.model_validate_json(output_text)
        except (ValueError, ValidationError) as exc:
            raise AIInvalidOutputError(
                f"structured output validation failed: {exc}"
            ) from exc

    @staticmethod
    def _extract_output_text(response_data: dict[str, Any]) -> str:
        # The REST response contains message content items. Supporting output_text
        # as well keeps the adapter easy to test with provider-compatible gateways.
        direct = response_data.get("output_text")
        if isinstance(direct, str) and direct:
            return direct
        for output_item in response_data.get("output", []):
            if not isinstance(output_item, dict):
                continue
            for content_item in output_item.get("content", []):
                if (
                    isinstance(content_item, dict)
                    and content_item.get("type") == "output_text"
                    and isinstance(content_item.get("text"), str)
                ):
                    return content_item["text"]
                if (
                    isinstance(content_item, dict)
                    and content_item.get("type") == "refusal"
                ):
                    raise AIInvalidOutputError("AI provider returned a refusal")
        raise AIInvalidOutputError("AI response did not contain output text")

    @staticmethod
    def _provider_error_detail(response: httpx.Response) -> str:
        try:
            return OpenAIResponsesAIService._response_error_detail(response.json())
        except (ValueError, TypeError):
            return response.reason_phrase or "unknown provider error"

    @staticmethod
    def _response_error_detail(response_data: dict[str, Any]) -> str:
        error = response_data.get("error")
        if isinstance(error, dict):
            code = str(error.get("code") or "unknown")
            message = str(error.get("message") or "unknown error")
            return f"{code}: {message[:500]}"
        return str(error or "unknown provider error")[:500]

    @staticmethod
    def _http_error_message(status_code: int) -> str:
        if status_code in {401, 403}:
            return "AI 服务认证失败：请检查 API Key 和项目权限。"
        if status_code == 429:
            return "AI 服务限流或额度不足：请稍后重试并检查账户额度。"
        if status_code == 400:
            return "AI 服务拒绝请求：请检查 AI_MODEL、AI_API_URL 和结构化输出支持。"
        if status_code >= 500:
            return "AI 服务暂时不可用，请稍后重试。"
        return "AI 服务请求失败，请检查 API 配置后重试。"


def create_ai_service(settings: Settings) -> AIService:
    if settings.ai_provider == "disabled":
        return DisabledAIService()
    if settings.ai_provider == "deepseek":
        from app.services.deepseek import DeepSeekProvider

        missing = []
        if not settings.deepseek_api_url:
            missing.append("DEEPSEEK_API_URL")
        if not settings.deepseek_api_key:
            missing.append("DEEPSEEK_API_KEY")
        if not settings.deepseek_model:
            missing.append("DEEPSEEK_MODEL")
        if missing:
            return MisconfiguredAIService(
                f"missing required DeepSeek settings: {', '.join(missing)}",
                "DeepSeek 未配置：请设置 DEEPSEEK_API_KEY，确认 "
                "DEEPSEEK_MODEL 和 DEEPSEEK_API_URL 后重启服务。",
            )
        if settings.deepseek_model in DeepSeekProvider.RETIRED_MODEL_ALIASES:
            return MisconfiguredAIService(
                f"retired DeepSeek model alias: {settings.deepseek_model}",
                "DeepSeek 模型名已退役：请使用 deepseek-v4-flash，"
                "不要使用 deepseek-chat 或 deepseek-reasoner。",
            )
        return DeepSeekProvider(
            api_url=settings.deepseek_api_url,
            api_key=settings.deepseek_api_key,
            model=settings.deepseek_model,
            timeout_seconds=settings.ai_timeout_seconds,
            debug_output=settings.ai_debug_output,
        )
    if settings.ai_provider == "openai":
        missing = []
        if not settings.ai_api_url:
            missing.append("AI_API_URL")
        if not settings.ai_api_key:
            missing.append("AI_API_KEY/OPENAI_API_KEY")
        if not settings.ai_model:
            missing.append("AI_MODEL")
        if missing:
            return MisconfiguredAIService(
                f"missing required AI settings: {', '.join(missing)}"
            )
        return OpenAIResponsesAIService(
            api_url=settings.ai_api_url,
            api_key=settings.ai_api_key,
            model=settings.ai_model,
            timeout_seconds=settings.ai_timeout_seconds,
        )
    return MisconfiguredAIService(
        f"unsupported AI_PROVIDER: {settings.ai_provider}"
    )
