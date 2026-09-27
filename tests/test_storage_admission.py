from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from app.auth import hash_invite_code
from app.auth_routes import CSRF_COOKIE_NAME
from app.config import Settings
from app.main import create_app
from app.repository import serialize_extra_information
from app.schemas import AIExtraction, AIItemFields
from app.services.ai import AIService, DisabledAIService
from app.services.storage_admission import (
    DATABASE_WRITE_ALLOWANCE, StorageAdmission, StorageAdmissionDenied,
)
from app.services.voice_storage import (
    EmptyVoiceUploadError, VoiceStorage, VoiceUploadTooLargeError,
)
from tests.conftest import FunctionAIService


INVITE_CODE = "storage-admission-invite"


class Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


def guard(tmp_path: Path, *, voice_max: int = 16, concurrent: int = 2,
          write_limit: int = 10, clock=None) -> StorageAdmission:
    return StorageAdmission(
        tmp_path / "database" / "app.db", tmp_path / "voice",
        min_free_bytes=100, write_window_seconds=60,
        write_limit=write_limit, voice_concurrent_limit=concurrent,
        voice_max_bytes=voice_max, clock=clock or Clock(),
    )


def test_relevant_filesystems_and_existing_reservations_control_admission(
    tmp_path: Path, monkeypatch,
) -> None:
    admission = guard(tmp_path, voice_max=16, concurrent=3)
    free = {"database": 100 + DATABASE_WRITE_ALLOWANCE + 10,
            "voice": 100 + 16 + 20}

    def filesystem(path):
        return (1, free["database"]) if path == admission.database_path.parent \
            else (2, free["voice"])

    monkeypatch.setattr(admission, "_filesystem", filesystem)
    with admission.reserve_voice():
        with pytest.raises(StorageAdmissionDenied, match="capacity"):
            admission.reserve_voice()
        # The database path has an independent reserve; Voice's free space
        # cannot make a database write safe on another filesystem.
        free["database"] = 100 + DATABASE_WRITE_ALLOWANCE - 1
        with pytest.raises(StorageAdmissionDenied, match="capacity"):
            admission.reserve_database_growth()
    free["database"] = 100 + DATABASE_WRITE_ALLOWANCE + 10
    with admission.reserve_voice():
        pass
    with admission.reserve_database_growth():
        pass
    free["database"] = 100 + 2 * DATABASE_WRITE_ALLOWANCE + 10
    with admission.reserve_database_growth():
        with admission.reserve_voice():
            pass


def test_same_filesystem_adds_voice_and_sqlite_allowances(tmp_path: Path, monkeypatch) -> None:
    admission = guard(tmp_path, voice_max=16)
    free = {"value": 100 + DATABASE_WRITE_ALLOWANCE + 16 - 1}
    monkeypatch.setattr(admission, "_filesystem", lambda _path: (1, free["value"]))
    with pytest.raises(StorageAdmissionDenied, match="capacity"):
        admission.reserve_voice()
    free["value"] += 1
    with admission.reserve_voice():
        with pytest.raises(StorageAdmissionDenied, match="capacity"):
            admission.reserve_database_growth()
    with admission.reserve_database_growth():
        pass


def test_write_window_and_voice_concurrency_expire_are_bounded(
    tmp_path: Path, monkeypatch,
) -> None:
    clock = Clock()
    admission = guard(tmp_path, concurrent=2, write_limit=2, clock=clock)
    monkeypatch.setattr(admission, "_filesystem", lambda _path: (1, 10_000_000))
    first = admission.reserve_voice()
    second = admission.reserve_voice()
    with pytest.raises(StorageAdmissionDenied, match="pressure"):
        admission.reserve_voice()
    first.release()
    second.release()
    # Released leases do not refund the bounded write window.
    with pytest.raises(StorageAdmissionDenied, match="pressure"):
        admission.reserve_voice()
    clock.now = 60
    third = admission.reserve_voice()
    fourth = admission.reserve_voice()
    with pytest.raises(StorageAdmissionDenied, match="pressure"):
        admission.reserve_voice()
    third.release()
    fourth.release()
    clock.now = 120
    with admission.reserve_database_growth():
        pass


def test_failed_and_cancelled_uploads_release_space_and_remove_parts(
    tmp_path: Path, monkeypatch,
) -> None:
    admission = guard(tmp_path, concurrent=1)
    monkeypatch.setattr(admission, "_filesystem", lambda _path: (1, 10_000_000))
    storage = VoiceStorage(tmp_path / "voice", 16, admission)

    async def broken():
        yield b"partial"
        raise OSError("connection lost")

    async def cancelled():
        yield b"partial"
        raise asyncio.CancelledError()

    with pytest.raises(OSError):
        asyncio.run(storage.store_original_async(broken()))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(storage.store_original_async(cancelled()))
    with pytest.raises(VoiceUploadTooLargeError):
        storage.store_original([b"x" * 17])
    with pytest.raises(EmptyVoiceUploadError):
        storage.store_original([b""])
    assert list(storage.original_root.rglob("*.part")) == []
    assert list(storage.original_root.rglob("*.bin")) == []
    saved = storage.store_original([b"audio"])
    assert saved.path.read_bytes() == b"audio"
    with admission.reserve_voice():
        pass


def test_database_low_water_blocks_growth_but_keeps_login_and_reduction(
    tmp_path: Path, monkeypatch,
) -> None:
    settings = Settings(
        database_path=tmp_path / "app.db",
        registration_mode="invite",
        invite_code_hash=hash_invite_code(INVITE_CODE),
        app_origin="http://testserver",
        session_cookie_secure=False,
        ai_provider="disabled",
        storage_min_free_bytes=100,
    )
    app = create_app(settings, ai_service=DisabledAIService())
    with TestClient(app) as client:
        registered = client.post("/api/auth/register", json={
            "invite_code": INVITE_CODE,
            "email": "storage@example.test", "password": "synthetic password",
            "display_name": "Storage", "timezone": "UTC",
        })
        assert registered.status_code == 201
        client.headers["X-CSRF-Token"] = client.cookies[CSRF_COOKIE_NAME]
        original = client.put("/api/capture-draft", json={
            "revision": 0, "current_text": "important original text",
        })
        assert original.status_code == 200
        draft = original.json()["draft"]
        monkeypatch.setattr(
            app.state.storage_admission, "_filesystem",
            lambda _path: (1, 100 + DATABASE_WRITE_ALLOWANCE - 1),
        )
        denied = client.post("/api/inputs", json={"original_text": "keep this"})
        assert denied.status_code == 507
        assert client.get("/api/items").json()["failed_inputs"] == []
        assert client.post("/api/auth/register", json={
            "invite_code": INVITE_CODE,
            "email": "second@example.test", "password": "synthetic password",
            "display_name": "Second", "timezone": "UTC",
        }).status_code == 507
        assert client.post("/api/auth/logout").status_code == 204
        assert client.post("/api/auth/login", json={
            "email": "storage@example.test", "password": "synthetic password",
        }).status_code == 200
        client.headers["X-CSRF-Token"] = client.cookies[CSRF_COOKIE_NAME]
        shortened = client.put("/api/capture-draft", json={
            "revision": draft["revision"], "current_text": "short",
        })
        assert shortened.status_code == 200
        assert client.delete("/api/capture-draft", params={
            "revision": shortened.json()["draft"]["revision"],
        }).status_code == 204


def test_item_extra_information_cannot_bypass_disk_or_size_boundaries(
    client_factory, monkeypatch,
) -> None:
    client = client_factory(FunctionAIService(lambda _text, _existing: AIExtraction(
        fields=AIItemFields(
            title="保存背景", type="other", status="active",
            extra_information={"note": "original background"},
        ),
        evidence_fields=set(),
    )))
    assert client.post("/api/inputs", json={"original_text": "原始背景"}).status_code == 202
    item_id = client.get("/api/items").json()["sortable_items"][0]["id"]
    guard = client.app.state.storage_admission
    monkeypatch.setattr(
        guard, "_filesystem",
        lambda _path: (1, guard.min_free_bytes + DATABASE_WRITE_ALLOWANCE - 1),
    )
    assert client.patch(f"/api/items/{item_id}", json={
        "extra_information": {"note": "x" * (
            client.app.state.settings.storage_item_extra_max_bytes + 1
        )},
    }).status_code == 413
    assert client.patch(f"/api/items/{item_id}", json={
        "extra_information": {"note": "expanded background with more context"},
    }).status_code == 507
    assert client.patch(f"/api/items/{item_id}", json={
        "is_pinned": True,
    }).status_code == 200
    reduced = client.patch(f"/api/items/{item_id}", json={
        "extra_information": None,
    })
    assert reduced.status_code == 200
    assert reduced.json()["extra_information"] is None


def test_database_write_holds_capacity_against_concurrent_growth_and_voice(
    tmp_path: Path, client_factory, monkeypatch,
) -> None:
    client = client_factory(FunctionAIService(lambda _text, _existing: AIExtraction(
        fields=AIItemFields(title="item", type="other", status="active"),
        evidence_fields=set(),
    )))
    assert client.post("/api/inputs", json={"original_text": "seed"}).status_code == 202
    item_id = client.get("/api/items").json()["sortable_items"][0]["id"]
    app = client.app
    admission = StorageAdmission(
        app.state.settings.database_path, tmp_path / "voice",
        min_free_bytes=100, write_window_seconds=60, write_limit=20,
        voice_concurrent_limit=2, voice_max_bytes=16,
    )
    free = 100 + 16 + 2 * DATABASE_WRITE_ALLOWANCE + 300
    monkeypatch.setattr(admission, "_filesystem", lambda _path: (1, free))
    app.state.storage_admission = admission
    voice = admission.reserve_voice()
    original_update = app.state.repository.update_item
    entered, finish = Event(), Event()

    def held_update(*args, **kwargs):
        entered.set()
        assert finish.wait(10)
        return original_update(*args, **kwargs)

    monkeypatch.setattr(app.state.repository, "update_item", held_update)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                client.patch, f"/api/items/{item_id}",
                json={"extra_information": {"note": "x" * 200}},
            )
            try:
                assert entered.wait(10)
                denied = client.post("/api/inputs", json={"original_text": "second"})
                assert denied.status_code == 507
                with app.state.database.connection() as connection:
                    assert connection.execute(
                        "SELECT COUNT(*) FROM item_inputs"
                    ).fetchone()[0] == 1
                assert client.get(
                    f"/api/items/{item_id}"
                ).json()["item"]["extra_information"] is None
                with pytest.raises(StorageAdmissionDenied, match="capacity"):
                    admission.reserve_voice()
            finally:
                finish.set()
            assert future.result().status_code == 200

        original_create = app.state.repository.create_input

        def failed_create(original_text, *args, **kwargs):
            if original_text == "failed":
                raise RuntimeError("synthetic database failure")
            return original_create(original_text, *args, **kwargs)

        monkeypatch.setattr(app.state.repository, "create_input", failed_create)
        with pytest.raises(RuntimeError, match="synthetic database failure"):
            client.post("/api/inputs", json={"original_text": "failed"})
        assert client.post("/api/inputs", json={"original_text": "second"}).status_code == 202
    finally:
        voice.release()


def test_draft_equal_size_and_shrink_remain_writable_below_low_water(
    client_factory, monkeypatch,
) -> None:
    client = client_factory(DisabledAIService())
    original = client.put("/api/capture-draft", json={
        "revision": 0, "current_text": "item",
    }).json()["draft"]
    guard = client.app.state.storage_admission
    monkeypatch.setattr(guard, "_filesystem", lambda _path: (
        1, guard.min_free_bytes - 1,
    ))
    revision = original["revision"]
    for text in ("item", "edit", "it"):
        response = client.put("/api/capture-draft", json={
            "revision": revision, "current_text": text,
        })
        assert response.status_code == 200
        revision = response.json()["draft"]["revision"]
    denied = client.put("/api/capture-draft", json={
        "revision": revision, "current_text": "items",
    })
    assert denied.status_code == 507
    assert client.get("/api/capture-draft").json()["draft"]["current_text"] == "it"


def test_item_equal_size_and_existing_over_limit_shrink_remain_writable(
    client_factory, monkeypatch,
) -> None:
    client = client_factory(FunctionAIService(lambda _text, _existing: AIExtraction(
        fields=AIItemFields(
            title="item", type="other", status="active",
            extra_information={"note": "x" * 300_000},
        ),
        evidence_fields=set(),
    )))
    assert client.post("/api/inputs", json={"original_text": "existing data"}).status_code == 202
    item_id = client.get("/api/items").json()["sortable_items"][0]["id"]
    guard = client.app.state.storage_admission
    free = {"value": guard.min_free_bytes - 1}
    monkeypatch.setattr(guard, "_filesystem", lambda _path: (1, free["value"]))
    path = f"/api/items/{item_id}"

    assert client.patch(path, json={"title": "edit"}).status_code == 200
    assert client.patch(path, json={"title": "ed"}).status_code == 200
    assert client.patch(path, json={"title": "edits"}).status_code == 507
    assert client.patch(path, json={
        "extra_information": {"note": "x" * 290_000},
    }).status_code == 200
    assert client.patch(path, json={
        "extra_information": {"note": "x" * 310_000},
    }).status_code == 413
    assert client.patch(path, json={
        "extra_information": {"note": "x" * 250_000},
    }).status_code == 200
    assert client.patch(path, json={
        "extra_information": {"note": "x" * 270_000},
    }).status_code == 413
    assert len(client.get(path).json()["item"]["extra_information"]["note"]) == 250_000
    free["value"] = guard.min_free_bytes + DATABASE_WRITE_ALLOWANCE + 10_000
    assert client.patch(path, json={
        "extra_information": {"note": "x" * 255_000},
    }).status_code == 200


def test_serialize_extra_information_is_canonical_for_size_boundaries() -> None:
    nested_forward = {"outer": {"z": False, "a": [True, 0]}}
    nested_reordered = {"outer": {"a": [True, 0], "z": False}}
    assert serialize_extra_information(nested_forward) == serialize_extra_information(
        nested_reordered
    )
    assert serialize_extra_information({"value": True}) != serialize_extra_information(
        {"value": 1}
    )
    assert serialize_extra_information(None) is None


@pytest.mark.parametrize("name", [
    "STORAGE_MIN_FREE_BYTES", "STORAGE_WRITE_WINDOW_SECONDS",
    "STORAGE_WRITE_LIMIT", "STORAGE_VOICE_CONCURRENT_LIMIT",
    "STORAGE_ITEM_EXTRA_MAX_BYTES",
])
@pytest.mark.parametrize("value", ["0", "invalid"])
def test_invalid_storage_configuration_fails_clearly(
    monkeypatch, tmp_path: Path, name: str, value: str,
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        Settings.from_environment(tmp_path / "empty.env")


def test_direct_settings_cannot_disable_item_size_boundary(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="STORAGE_ITEM_EXTRA_MAX_BYTES"):
        create_app(Settings(
            database_path=tmp_path / "app.db",
            storage_item_extra_max_bytes=0,
        ))


def test_stream_upload_handoff_is_bounded_and_expires(tmp_path: Path, monkeypatch) -> None:
    clock = Clock()
    admission = guard(tmp_path, concurrent=1, write_limit=20, clock=clock)
    monkeypatch.setattr(admission, "_filesystem", lambda _path: (1, 10_000_000))
    key = (1, 2, 3, 4, "client")

    held = admission.reserve_voice()
    with pytest.raises(StorageAdmissionDenied, match="pressure"):
        admission.reserve_voice()
    admission.park_stream_upload(key, held)
    held.release()
    # The parked lease keeps the Voice slot reserved for the late upload.
    with pytest.raises(StorageAdmissionDenied, match="pressure"):
        admission.reserve_voice()
    transferred = admission.take_stream_upload(key)
    assert transferred is not None
    with transferred:
        pass
    with admission.reserve_voice():
        pass

    orphaned = admission.reserve_voice()
    admission.park_stream_upload(key, orphaned)
    orphaned.release()
    clock.now = 181
    # An expired handoff releases its slot instead of leaking it.
    with admission.reserve_voice():
        pass
    assert admission.take_stream_upload(key) is None
