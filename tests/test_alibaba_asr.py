from __future__ import annotations

import asyncio
import base64
import json
import threading
from pathlib import Path

import httpx
import pytest

from app.services.alibaba_asr import (
    AlibabaASRAuthenticationError,
    AlibabaASRClient,
    AlibabaASRConfigurationError,
    AlibabaASRInvalidResponseError,
    AlibabaASRNetworkError,
    AlibabaASRQuotaError,
    AlibabaASRRejectedError,
    AlibabaASRTimeoutError,
    AlibabaASRUnavailableError,
)


def make_client(handler) -> tuple[AlibabaASRClient, httpx.AsyncClient]:
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return (
        AlibabaASRClient(
            api_url="https://workspace.example.invalid/asr",
            api_key="synthetic-test-key",
            timeout_seconds=3,
            client=http_client,
        ),
        http_client,
    )


def run_transcription(provider: AlibabaASRClient, audio: Path, **kwargs):
    return asyncio.run(provider.transcribe(audio, **kwargs))


@pytest.mark.parametrize(
    "api_url",
    [
        "http://dashscope.example.invalid/generation",
        "https://user:password@dashscope.example.invalid/generation",
        "https://dashscope.example.invalid/generation#fragment",
        "https://[invalid",
        "not-a-url",
    ],
)
def test_adapter_rejects_insecure_or_credential_bearing_endpoint(api_url: str):
    with pytest.raises(AlibabaASRConfigurationError, match="HTTPS endpoint"):
        AlibabaASRClient(
            api_url=api_url,
            api_key="synthetic-test-key",
            timeout_seconds=3,
        )


@pytest.mark.parametrize(
    "timeout", [0, -1, True, float("nan"), float("inf"), 121]
)
def test_adapter_rejects_invalid_timeout(timeout: float):
    with pytest.raises(AlibabaASRConfigurationError, match="at most 120"):
        AlibabaASRClient(
            api_url="https://example.invalid/asr",
            api_key="synthetic-test-key",
            timeout_seconds=timeout,
        )


def test_direct_webm_request_matches_provider_contract_and_minimizes_metadata(
    tmp_path: Path,
):
    audio = tmp_path / "audio.webm"
    audio.write_bytes(b"synthetic-webm")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"output": {"text": "机器转写"}, "request_id": "request-1"},
        )

    provider, http_client = make_client(handler)
    try:
        result = run_transcription(
            provider,
            audio,
            format="webm",
            sample_rate_hz=None,
        )
    finally:
        asyncio.run(http_client.aclose())

    assert result.transcript == "机器转写"
    assert result.request_id == "request-1"
    assert len(requests) == 1
    request = requests[0]
    payload = json.loads(request.content)
    assert payload["model"] == "qwen-audio-3.0-asr-flash"
    assert payload["parameters"] == {"format": "webm"}
    assert set(payload) == {"model", "input", "parameters"}
    serialized = request.content.decode("utf-8")
    for forbidden in ("user_id", "draft", "item_id", "storage_key", "filename"):
        assert forbidden not in serialized
    data_url = payload["input"]["messages"][0]["content"][0]["input_audio"][
        "data"
    ]
    assert data_url == "data:audio/webm;base64," + base64.b64encode(
        b"synthetic-webm"
    ).decode("ascii")
    assert request.headers["authorization"] == "Bearer synthetic-test-key"
    assert request.headers["x-dashscope-sse"] == "disable"


def test_normalized_wav_request_sends_only_derived_audio_and_16khz(
    tmp_path: Path,
):
    audio = tmp_path / "derived.wav"
    audio.write_bytes(b"derived-only")
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"output": {"text": "转写"}, "request_id": "request-2"},
        )

    provider, http_client = make_client(handler)
    try:
        run_transcription(provider, audio, format="wav", sample_rate_hz=16_000)
    finally:
        asyncio.run(http_client.aclose())

    assert payloads[0]["parameters"] == {
        "format": "wav",
        "sample_rate": "16000",
    }
    data_url = payloads[0]["input"]["messages"][0]["content"][0][
        "input_audio"
    ]["data"]
    assert data_url == "data:audio/wav;base64," + base64.b64encode(
        b"derived-only"
    ).decode("ascii")


@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [
        (400, AlibabaASRRejectedError),
        (401, AlibabaASRAuthenticationError),
        (403, AlibabaASRAuthenticationError),
        (429, AlibabaASRQuotaError),
        (500, AlibabaASRUnavailableError),
        (503, AlibabaASRUnavailableError),
    ],
)
def test_http_failures_are_classified_without_body_or_key_leakage(
    tmp_path: Path,
    status_code: int,
    error_type: type[Exception],
):
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"synthetic")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status_code,
            json={"secret_provider_body": "must-never-be-copied"},
        )

    provider, http_client = make_client(handler)
    try:
        with pytest.raises(error_type) as captured:
            run_transcription(
                provider,
                audio,
                format="wav",
                sample_rate_hz=16_000,
            )
    finally:
        asyncio.run(http_client.aclose())
    assert calls == 1
    assert "must-never-be-copied" not in str(captured.value)
    assert "synthetic-test-key" not in str(captured.value)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"output": {"text": ""}, "request_id": "request"},
        {"output": {"text": "transcript"}, "request_id": ""},
        {"output": {"wrong": "path"}, "request_id": "request"},
    ],
)
def test_success_requires_nonempty_transcript_and_request_id(
    tmp_path: Path,
    body: dict,
):
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"synthetic")
    provider, http_client = make_client(
        lambda request: httpx.Response(200, json=body)
    )
    try:
        with pytest.raises(AlibabaASRInvalidResponseError):
            run_transcription(
                provider,
                audio,
                format="wav",
                sample_rate_hz=16_000,
            )
    finally:
        asyncio.run(http_client.aclose())


def test_network_and_timeout_are_distinct_and_never_retried(tmp_path: Path):
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"synthetic")
    timeout_calls = 0

    def timeout_handler(request: httpx.Request):
        nonlocal timeout_calls
        timeout_calls += 1
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    provider, http_client = make_client(timeout_handler)
    try:
        with pytest.raises(AlibabaASRTimeoutError):
            run_transcription(
                provider,
                audio,
                format="wav",
                sample_rate_hz=16_000,
            )
    finally:
        asyncio.run(http_client.aclose())
    assert timeout_calls == 1

    network_calls = 0

    def network_handler(request: httpx.Request):
        nonlocal network_calls
        network_calls += 1
        raise httpx.ConnectError("synthetic network", request=request)

    provider, http_client = make_client(network_handler)
    try:
        with pytest.raises(AlibabaASRNetworkError):
            run_transcription(
                provider,
                audio,
                format="wav",
                sample_rate_hz=16_000,
            )
    finally:
        asyncio.run(http_client.aclose())
    assert network_calls == 1


def test_audio_read_and_base64_work_do_not_block_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    audio = tmp_path / "audio.webm"
    audio.write_bytes(b"synthetic")
    entered = threading.Event()
    release = threading.Event()
    original_read_bytes = Path.read_bytes

    def blocking_read(path: Path) -> bytes:
        entered.set()
        assert release.wait(timeout=5)
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", blocking_read)
    provider, http_client = make_client(
        lambda request: httpx.Response(
            200,
            json={"output": {"text": "ok"}, "request_id": "request"},
        )
    )

    async def scenario() -> int:
        task = asyncio.create_task(
            provider.transcribe(audio, format="webm", sample_rate_hz=None)
        )
        ticks = 0
        while not entered.is_set():
            ticks += 1
            await asyncio.sleep(0)
        for _ in range(5):
            ticks += 1
            await asyncio.sleep(0)
        assert not task.done()
        release.set()
        await task
        return ticks

    try:
        assert asyncio.run(scenario()) >= 5
    finally:
        release.set()
        asyncio.run(http_client.aclose())
