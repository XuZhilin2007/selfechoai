from __future__ import annotations

import asyncio
import base64
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.voice_contracts import ALIBABA_ASR_MODEL


class AlibabaASRError(RuntimeError):
    failure_code = "internal"
    user_message = "语音转写出现内部错误，原始录音已保留。"


class AlibabaASRConfigurationError(AlibabaASRError):
    failure_code = "configuration"
    user_message = "语音转写尚未正确配置，原始录音已保留。"


class AlibabaASRNetworkError(AlibabaASRError):
    failure_code = "network"
    user_message = "暂时无法连接语音转写服务，原始录音已保留。"


class AlibabaASRTimeoutError(AlibabaASRError):
    failure_code = "timeout"
    user_message = "语音转写超时，原始录音已保留。"


class AlibabaASRAuthenticationError(AlibabaASRError):
    failure_code = "authentication"
    user_message = "语音转写服务认证失败，原始录音已保留。"


class AlibabaASRQuotaError(AlibabaASRError):
    failure_code = "quota_rate_limit"
    user_message = "语音转写服务额度不足或请求过多，原始录音已保留。"


class AlibabaASRRejectedError(AlibabaASRError):
    failure_code = "provider_rejected"
    user_message = "语音转写服务拒绝了本次请求，原始录音已保留。"


class AlibabaASRUnavailableError(AlibabaASRError):
    failure_code = "provider_unavailable"
    user_message = "语音转写服务暂时不可用，原始录音已保留。"


class AlibabaASRInvalidResponseError(AlibabaASRError):
    failure_code = "invalid_response"
    user_message = "语音转写结果无效，原始录音已保留。"


@dataclass(frozen=True, slots=True)
class AlibabaASRResult:
    transcript: str
    request_id: str


class AlibabaASRClient:
    """Non-streaming DashScope adapter with exactly one HTTP attempt."""

    def __init__(
        self,
        *,
        api_url: str,
        api_key: str,
        timeout_seconds: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_url or not api_key:
            raise AlibabaASRConfigurationError(
                "ALIBABA_ASR_API_URL and ALIBABA_API_KEY are required"
            )
        try:
            endpoint = urlsplit(api_url)
        except ValueError as exc:
            raise AlibabaASRConfigurationError(
                "ALIBABA_ASR_API_URL must be an HTTPS endpoint without credentials"
            ) from exc
        if (
            endpoint.scheme.lower() != "https"
            or not endpoint.hostname
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.fragment
        ):
            raise AlibabaASRConfigurationError(
                "ALIBABA_ASR_API_URL must be an HTTPS endpoint without credentials"
            )
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 120
        ):
            raise AlibabaASRConfigurationError(
                "VOICE_ASR_TIMEOUT_SECONDS must be greater than 0 and at most 120"
            )
        self.api_url = api_url
        self._api_key = api_key
        self.timeout_seconds = timeout_seconds
        self._client = client

    async def transcribe(
        self,
        audio_path: Path,
        *,
        format: str,
        sample_rate_hz: int | None,
    ) -> AlibabaASRResult:
        media_type = {"webm": "audio/webm", "wav": "audio/wav"}.get(format)
        if media_type is None:
            raise AlibabaASRConfigurationError(
                "unsupported internal Alibaba ASR input format"
            )
        data_url = await asyncio.to_thread(
            _encode_audio_data_url,
            audio_path,
            media_type,
        )
        parameters: dict[str, str] = {"format": format}
        if sample_rate_hz is not None:
            parameters["sample_rate"] = str(sample_rate_hz)
        payload: dict[str, Any] = {
            "model": ALIBABA_ASR_MODEL,
            "input": {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {"data": data_url},
                            }
                        ],
                    }
                ]
            },
            "parameters": parameters,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "X-DashScope-SSE": "disable",
        }

        try:
            if self._client is not None:
                response = await self._client.post(
                    self.api_url,
                    json=payload,
                    headers=headers,
                    timeout=self.timeout_seconds,
                )
            else:
                async with httpx.AsyncClient(
                    timeout=self.timeout_seconds,
                    follow_redirects=False,
                ) as client:
                    response = await client.post(
                        self.api_url,
                        json=payload,
                        headers=headers,
                    )
        except httpx.TimeoutException as exc:
            raise AlibabaASRTimeoutError("Alibaba ASR request timed out") from exc
        except httpx.RequestError as exc:
            raise AlibabaASRNetworkError(
                f"Alibaba ASR connection failed: {type(exc).__name__}"
            ) from exc

        if not response.is_success:
            self._raise_http_error(response.status_code)
        try:
            body = response.json()
            output = body["output"]
            transcript = output["text"]
            request_id = body["request_id"]
        except (KeyError, TypeError, ValueError) as exc:
            raise AlibabaASRInvalidResponseError(
                "Alibaba ASR success response had an invalid structure"
            ) from exc
        if not isinstance(transcript, str) or not transcript.strip():
            raise AlibabaASRInvalidResponseError(
                "Alibaba ASR success response had an empty transcript"
            )
        if not isinstance(request_id, str) or not request_id.strip():
            raise AlibabaASRInvalidResponseError(
                "Alibaba ASR success response had an empty request_id"
            )
        return AlibabaASRResult(transcript=transcript, request_id=request_id)

    @staticmethod
    def _raise_http_error(status_code: int) -> None:
        if status_code in {401, 403}:
            raise AlibabaASRAuthenticationError(f"Alibaba ASR HTTP {status_code}")
        if status_code == 429:
            raise AlibabaASRQuotaError("Alibaba ASR HTTP 429")
        if status_code >= 500:
            raise AlibabaASRUnavailableError(f"Alibaba ASR HTTP {status_code}")
        raise AlibabaASRRejectedError(f"Alibaba ASR HTTP {status_code}")


def _encode_audio_data_url(audio_path: Path, media_type: str) -> str:
    encoded = base64.b64encode(audio_path.read_bytes()).decode("ascii")
    return f"data:{media_type};base64,{encoded}"
