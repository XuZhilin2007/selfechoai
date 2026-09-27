"""One Alibaba duplex recognition task; never owns Original Audio or Draft text."""

from __future__ import annotations

import json
import uuid
from urllib.parse import urlsplit, urlunsplit

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from app.services.alibaba_asr import (
    AlibabaASRConfigurationError,
    AlibabaASRInvalidResponseError,
    AlibabaASRNetworkError,
    AlibabaASRRejectedError,
    AlibabaASRTimeoutError,
)
from app.voice_contracts import ALIBABA_STREAMING_ASR_MODEL


class StreamingTranscript:
    """Sentence finals are stable; the current sentence is replaceable preview."""

    def __init__(self) -> None:
        self._final: dict[str, str] = {}
        self._partial: dict[str, str] = {}

    def accept(self, sentence: dict) -> str:
        if not sentence:
            return self.preview
        if sentence.get("text") == "":
            if sentence.get("sentence_end") is True:
                self._partial.pop(str(sentence.get("sentence_id")), None)
            return self.preview
        sentence_id = sentence.get("sentence_id")
        if not isinstance(sentence_id, (int, str)) or isinstance(sentence_id, bool):
            raise AlibabaASRInvalidResponseError("missing sentence id")
        key = str(sentence_id)
        text = sentence.get("text")
        if not isinstance(text, str):
            raise AlibabaASRInvalidResponseError("invalid sentence text")
        if sentence.get("sentence_end") is True:
            self._partial.pop(key, None)
            if text.strip():
                self._final[key] = text.strip()
        elif text.strip() and key not in self._final:
            self._partial[key] = text.strip()
        return self.preview

    @property
    def preview(self) -> str:
        return " ".join([*self._final.values(), *self._partial.values()]).strip()

    @property
    def complete(self) -> str:
        if self._partial:
            raise AlibabaASRInvalidResponseError("task ended with unfinished sentence")
        result = " ".join(self._final.values()).strip()
        if not result:
            raise AlibabaASRInvalidResponseError("task ended without final transcript")
        return result


def streaming_asr_configured(api_url: str, api_key: str) -> bool:
    """Whether the default adapter accepts this Voice configuration.

    Batch ASR has weaker endpoint requirements, so a valid batch-only Voice
    setup can legitimately report streaming as unavailable.
    """

    endpoint = urlsplit(api_url)
    return (
        endpoint.scheme == "https"
        and bool(endpoint.hostname)
        and endpoint.hostname.lower().rstrip(".") != "dashscope.aliyuncs.com"
        and endpoint.username is None
        and endpoint.password is None
        and bool(api_key)
    )


class AlibabaStreamingASRSession:
    def __init__(self, api_url: str, api_key: str, timeout_seconds: float) -> None:
        if not streaming_asr_configured(api_url, api_key):
            raise AlibabaASRConfigurationError(
                "Streaming Voice requires a workspace-specific HTTPS endpoint "
                "and API key; the shared dashscope.aliyuncs.com endpoint does "
                "not provide streaming recognition"
            )
        endpoint = urlsplit(api_url)
        self.url = urlunsplit(("wss", endpoint.netloc, "/api-ws/v1/inference", "", ""))
        self._key = api_key
        self.timeout_seconds = timeout_seconds
        self.task_id = str(uuid.uuid4())
        self._socket = None

    async def open(self) -> None:
        try:
            self._socket = await connect(
                self.url,
                additional_headers={"Authorization": f"Bearer {self._key}"},
                open_timeout=self.timeout_seconds,
                max_size=128 * 1024,
                max_queue=4,
            )
            await self._socket.send(json.dumps({
                "header": {"action": "run-task", "task_id": self.task_id, "streaming": "duplex"},
                "payload": {
                    "task_group": "audio", "task": "asr", "function": "recognition",
                    "model": ALIBABA_STREAMING_ASR_MODEL,
                    "parameters": {"format": "pcm", "sample_rate": 16000}, "input": {},
                },
            }))
            event = await self.receive()
            if event != "task-started":
                raise AlibabaASRInvalidResponseError("task did not start")
        except TimeoutError as exc:
            await self.close()
            raise AlibabaASRTimeoutError("streaming ASR start timed out") from exc
        except (OSError, ConnectionClosed) as exc:
            await self.close()
            raise AlibabaASRNetworkError("streaming ASR connection failed") from exc

    async def send_audio(self, pcm: bytes) -> None:
        if self._socket is None:
            raise AlibabaASRNetworkError("streaming ASR is closed")
        try:
            await self._socket.send(pcm)
        except (OSError, ConnectionClosed) as exc:
            raise AlibabaASRNetworkError("streaming ASR audio send failed") from exc

    async def finish(self) -> None:
        if self._socket is None:
            raise AlibabaASRNetworkError("streaming ASR is closed")
        await self._socket.send(json.dumps({
            "header": {"action": "finish-task", "task_id": self.task_id, "streaming": "duplex"},
            "payload": {"input": {}},
        }))

    async def receive(self) -> tuple[str, dict] | str:
        if self._socket is None:
            raise AlibabaASRNetworkError("streaming ASR is closed")
        try:
            raw = await self._socket.recv()
        except (OSError, ConnectionClosed) as exc:
            raise AlibabaASRNetworkError("streaming ASR disconnected") from exc
        try:
            message = json.loads(raw)
            header = message["header"]
            if header["task_id"] != self.task_id:
                raise ValueError("task id mismatch")
            event = header["event"]
            if event == "result-generated":
                sentence = message["payload"]["output"].get("sentence") or {}
                if not isinstance(sentence, dict):
                    raise ValueError("sentence is not an object")
                return event, sentence
            if event == "task-failed":
                raise AlibabaASRRejectedError("streaming ASR task failed")
            if event not in {"task-started", "task-finished"}:
                raise ValueError("unknown event")
            return event
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AlibabaASRInvalidResponseError("invalid streaming ASR event") from exc

    async def close(self) -> None:
        if self._socket is not None:
            socket, self._socket = self._socket, None
            await socket.close()
