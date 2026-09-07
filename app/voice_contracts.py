"""Frozen domain contracts for the Public v0.5 Voice foundation."""

from __future__ import annotations

import re


MAX_CAPTURE_TEXT_LENGTH = 10_000
MAX_VOICE_SEGMENT_DURATION_SECONDS = 60.0
DEFAULT_VOICE_MAX_UPLOAD_BYTES = 16 * 1024 * 1024

# These values are schema identities in Stage 1. Provider integration and
# configuration remain deferred to Stage 2.
ALIBABA_ASR_PROVIDER = "alibaba"
ALIBABA_ASR_MODEL = "qwen-audio-3.0-asr-flash"

# Only the tuple proven by the approved v0.5 browser acceptance evidence is
# eligible for direct Provider submission. Other decodable media is normalized.
DIRECT_CONTAINER_ALIASES = frozenset({"matroska", "webm"})
DIRECT_CODEC = "opus"
DIRECT_SAMPLE_RATE_HZ = 48_000
DIRECT_CHANNELS = 1

CLIENT_SEGMENT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def validate_client_segment_id(value: str) -> str:
    if not isinstance(value, str) or CLIENT_SEGMENT_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("invalid client_segment_id")
    return value
