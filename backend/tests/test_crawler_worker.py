import asyncio
import base64
import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from types import ModuleType

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import crawler.app as bridge
import crawler.worker as worker
from crawler.worker import (
    SLIDER_MANUAL_VERIFICATION_MESSAGE,
    _with_system_browser,
    clear_chromium_singleton_files,
    install_douyin_slider_guard,
    install_weibo_login_compatibility,
    install_xhs_verification_monitor,
    provider_max_concurrency,
    profile_is_in_use,
    write_qr_image,
)
from crawler.worker_protocol import (
    MANUAL_VERIFICATION_CODE,
    classify_worker_exception,
    read_worker_request,
    read_worker_result,
    safe_exception_diagnostic,
    write_worker_request,
    write_worker_result,
)
from crawler.upstream_compatibility import verify_upstream_compatibility

PROVIDER_FIXTURES = (
    Path(__file__).parent / "fixtures" / "provider" / "platform_contracts.json"
)


def test_stale_chromium_singleton_files_are_removed(tmp_path: Path):
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        (tmp_path / name).write_text("stale", encoding="utf-8")

    assert profile_is_in_use(tmp_path) is False
    clear_chromium_singleton_files(tmp_path)

    assert not any((tmp_path / name).exists() for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"))


def test_bilibili_discovery_uses_bounded_configurable_concurrency(monkeypatch):
    assert provider_max_concurrency("xiaohongshu", "discover") == 1
    assert provider_max_concurrency("bilibili", "stage") == 1
    monkeypatch.setenv("BILIBILI_DISCOVERY_CONCURRENCY", "3")
    assert provider_max_concurrency("bilibili", "discover") == 3
    monkeypatch.setenv("BILIBILI_DISCOVERY_CONCURRENCY", "99")
    assert provider_max_concurrency("bilibili", "discover") == 4
    monkeypatch.setenv("BILIBILI_DISCOVERY_CONCURRENCY", "invalid")
    assert provider_max_concurrency("bilibili", "discover") == 3


def test_startup_cleanup_removes_abandoned_discovery_but_keeps_fresh_stage(
    tmp_path: Path,
    monkeypatch,
):
    now = 1_800_000_000.0
    discovery = tmp_path / f"discover-{'a' * 32}"
    fresh_stage = tmp_path / ("b" * 32)
    expired_stage = tmp_path / ("c" * 32)
    unrelated = tmp_path / "import-upload"
    for path in (discovery, fresh_stage, expired_stage, unrelated):
        path.mkdir()
        (path / "marker").write_text("fixture", encoding="utf-8")
    os.utime(discovery, (now, now))
    os.utime(fresh_stage, (now, now))
    os.utime(expired_stage, (now - bridge.STALE_STAGE_AGE_SECONDS - 1,) * 2)
    monkeypatch.setattr(bridge, "STAGING_ROOT", tmp_path)

    assert bridge.cleanup_stale_staging(now=now) == 2
    assert not discovery.exists()
    assert not expired_stage.exists()
    assert fresh_stage.exists()
    assert unrelated.exists()


def test_qr_read_cannot_downgrade_authenticated_session(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(bridge, "STATE_ROOT", tmp_path)
    bridge.write_state("douyin", "authenticated")
    bridge.qr_path("douyin").write_bytes(b"stale-qr")

    with pytest.raises(HTTPException) as caught:
        bridge.qr("douyin")

    assert caught.value.status_code == 409
    assert bridge.read_state("douyin")["status"] == "authenticated"


def test_qr_is_published_atomically_and_returned_as_non_cacheable_data_url(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(bridge, "STATE_ROOT", tmp_path)
    image = b"\x89PNG\r\n\x1a\nqr-image"
    encoded = base64.b64encode(image).decode("ascii")

    bridge.write_state("xiaohongshu", "starting")
    write_qr_image(bridge.qr_path("xiaohongshu"), f"data:image/png;base64,{encoded}")

    state = next(item for item in bridge.sessions() if item["platform"] == "xiaohongshu")
    assert state["status"] == "qr_ready"
    response = bridge.qr("xiaohongshu")
    payload = json.loads(response.body)
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert payload["status"] == "qr_ready"
    assert payload["image_data_url"] == f"data:image/png;base64,{encoded}"
    assert not list(tmp_path.glob("*.tmp"))


def test_session_state_rejects_unknown_status_and_cross_platform_files(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setattr(bridge, "STATE_ROOT", tmp_path)
    bridge.state_path("xiaohongshu").write_text(
        json.dumps(
            {
                "platform": "douyin",
                "status": "authenticated",
                "message": None,
            }
        ),
        encoding="utf-8",
    )

    assert bridge.read_state("xiaohongshu")["status"] == "logged_out"
    with pytest.raises(ValueError, match="Unknown platform session status"):
        bridge.write_state("xiaohongshu", "trusted_forever")


def test_discovery_limit_accepts_recovery_window_up_to_500():
    request = bridge.DiscoverRequest(
        platform="bilibili",
        profile_url="https://space.bilibili.com/1",
        limit=500,
    )
    assert request.limit == 500

    with pytest.raises(ValidationError):
        bridge.DiscoverRequest(
            platform="bilibili",
            profile_url="https://space.bilibili.com/1",
            limit=501,
        )


def test_health_checks_the_container_internal_novnc_port(monkeypatch):
    commands: list[tuple[str, ...]] = []

    def process_has_command(*parts):
        commands.append(parts)
        return True

    monkeypatch.setattr(
        bridge,
        "process_has_command",
        process_has_command,
    )
    monkeypatch.setattr(
        bridge,
        "NOVNC_HTML",
        SimpleNamespace(is_file=lambda: True),
    )
    monkeypatch.setattr(
        bridge,
        "X11_SOCKET",
        SimpleNamespace(exists=lambda: True),
    )
    monkeypatch.setattr(bridge, "NOVNC_PORT", 17900)
    monkeypatch.setattr(
        bridge,
        "provider_build_metadata",
        lambda: {
            "mediacrawler_commit": "d280d22",
            "xhshow_version": "0.2.0",
            "xhs_sign_override_sha256": "a" * 64,
        },
    )
    monkeypatch.setattr(
        bridge,
        "verify_upstream_compatibility",
        lambda _root: {
            "compatible": True,
            "platforms": {
                platform: {"compatible": True, "missing": []}
                for platform in ("xiaohongshu", "douyin", "bilibili", "weibo")
            },
        },
    )

    health = bridge.health()
    assert health["status"] == "ok"
    assert health["commit"] == "d280d22"
    assert health["xhshow_version"] == "0.2.0"
    assert all(health["platform_compatibility"].values())
    assert commands == [
        ("x11vnc", "-rfbport", "5900"),
        ("websockify", "7900", "127.0.0.1:5900"),
    ]


def test_upstream_compatibility_check_fails_closed_for_missing_tree(tmp_path: Path):
    result = verify_upstream_compatibility(tmp_path)

    assert result["compatible"] is False
    assert set(result["platforms"]) == {
        "xiaohongshu",
        "douyin",
        "bilibili",
        "weibo",
    }
    assert all(value["missing"] for value in result["platforms"].values())


@pytest.mark.asyncio
async def test_creator_values_match_each_pinned_platform_parser(monkeypatch):
    async def allow_public(_platform, _url):
        return None

    monkeypatch.setattr(bridge, "validate_public_platform_url", allow_public)
    cases = [
        (
            "bilibili",
            "https://space.bilibili.com/999999999999999999?from=archive",
            "https://space.bilibili.com/999999999999999999?from=archive",
        ),
        (
            "weibo",
            "https://weibo.com/u/999999999999999999",
            "999999999999999999",
        ),
        (
            "douyin",
            "https://www.douyin.com/user/MS4wLjABAAAAexample",
            "https://www.douyin.com/user/MS4wLjABAAAAexample",
        ),
        (
            "xiaohongshu",
            "https://www.xiaohongshu.com/user/profile/abc123?xsec_token=token",
            "https://www.xiaohongshu.com/user/profile/abc123?xsec_token=token",
        ),
    ]

    for platform, profile_url, expected in cases:
        request = bridge.DiscoverRequest(platform=platform, profile_url=profile_url)
        assert await bridge.creator_value(platform, request.profile_url) == expected

    video = bridge.DiscoverRequest(
        platform="bilibili",
        profile_url="https://www.bilibili.com/video/BV1notAProfile",
    )
    with pytest.raises(HTTPException) as caught:
        await bridge.creator_value(video.platform, video.profile_url)
    assert caught.value.status_code == 422


@pytest.mark.asyncio
async def test_logout_waits_for_active_collection_lock_before_removing_profile(
    tmp_path: Path, monkeypatch
):
    browser_root = tmp_path / "browser"
    state_root = tmp_path / "state"
    profile = browser_root / "bilibili"
    profile.mkdir(parents=True)
    (profile / "in-use").write_text("profile", "utf-8")
    lock = asyncio.Lock()
    await lock.acquire()
    monkeypatch.setattr(bridge, "BROWSER_ROOT", browser_root)
    monkeypatch.setattr(bridge, "STATE_ROOT", state_root)
    monkeypatch.setitem(bridge.locks, "bilibili", lock)
    bridge.processes.pop("bilibili", None)
    bridge.logout_in_progress.discard("bilibili")

    task = asyncio.create_task(bridge.logout("bilibili"))
    await asyncio.sleep(0)

    assert task.done() is False
    assert profile.is_dir()
    lock.release()
    result = await task

    assert result["status"] == "logged_out"
    assert not profile.exists()
    assert "bilibili" not in bridge.logout_in_progress


@pytest.mark.asyncio
async def test_system_chromium_is_injected_only_when_upstream_did_not_select_a_browser():
    calls = []

    async def launch(_self, *args, **kwargs):
        calls.append((args, kwargs))
        return kwargs

    wrapped = _with_system_browser(launch, "/usr/bin/chromium")

    assert (await wrapped(object(), headless=False))["executable_path"] == "/usr/bin/chromium"
    assert "executable_path" not in await wrapped(object(), channel="chrome")
    assert (await wrapped(object(), executable_path="/custom/chrome"))["executable_path"] == "/custom/chrome"
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_douyin_slider_guard_waits_for_human_and_rejects_automatic_movement(tmp_path: Path):
    called = []

    class FakeDouyinLogin:
        async def check_login_state(self):
            called.append("check-state")
            return len(called) >= 2

        async def move_slider(self, *_args, **_kwargs):
            called.append("move")

    state_path = tmp_path / "douyin.json"
    install_douyin_slider_guard(
        FakeDouyinLogin,
        state_path=state_path,
        timeout_seconds=1,
        poll_interval=0,
    )
    login = FakeDouyinLogin()

    await login.check_page_display_slider()
    state = json.loads(state_path.read_text("utf-8"))
    assert state["status"] == "manual_verification_required"
    assert state["manual_verification_url"].startswith("http://127.0.0.1:7900/")
    with pytest.raises(RuntimeError, match="automatic slider solving is disabled"):
        await login.move_slider("background", "gap")
    assert called == ["check-state", "check-state"]
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.asyncio
async def test_xhs_secondary_sms_verification_is_published_for_manual_completion(
    tmp_path: Path,
):
    original_finished = asyncio.Event()

    class Body:
        async def inner_text(self, timeout):
            assert timeout == 1_000
            return "为保障账号安全，请使用手机号验证并输入短信验证码"

    class Page:
        url = "https://www.xiaohongshu.com/verify/"

        def locator(self, selector):
            assert selector == "body"
            return Body()

    class FakeXhsLogin:
        context_page = Page()

        async def check_login_state(self, _session):
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            original_finished.set()
            return True

    state_path = tmp_path / "xiaohongshu.json"
    install_xhs_verification_monitor(
        FakeXhsLogin,
        state_path=state_path,
        poll_interval=0,
    )

    assert await FakeXhsLogin().check_login_state("old-session") is True
    assert original_finished.is_set()
    state = json.loads(state_path.read_text("utf-8"))
    assert state["status"] == "manual_verification_required"
    assert "短信或安全二次验证" in state["message"]
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.asyncio
async def test_weibo_login_uses_desktop_user_agent_and_rejects_zero_exit():
    calls: list[tuple] = []

    class FakeCrawler:
        async def launch_browser(
            self,
            chromium,
            playwright_proxy,
            user_agent,
            headless=True,
        ):
            calls.append((chromium, playwright_proxy, user_agent, headless))
            return "context"

    class FakeLogin:
        async def login_by_qrcode(self):
            raise SystemExit()

    install_weibo_login_compatibility(
        FakeCrawler,
        FakeLogin,
        desktop_user_agent="desktop-user-agent",
    )

    context = await FakeCrawler().launch_browser(
        "chromium",
        "proxy",
        "mobile-user-agent",
        False,
    )
    assert context == "context"
    assert calls == [
        ("chromium", "proxy", "desktop-user-agent", False),
    ]
    with pytest.raises(RuntimeError, match="failed before authentication"):
        await FakeLogin().login_by_qrcode()


@pytest.mark.asyncio
async def test_slider_guard_failure_becomes_manual_verification_state(tmp_path: Path, monkeypatch):
    class FailedProcess:
        returncode = 1

        async def communicate(self):
            return SLIDER_MANUAL_VERIFICATION_MESSAGE.encode(), None

    process = FailedProcess()
    lock = asyncio.Lock()
    await lock.acquire()
    monkeypatch.setattr(bridge, "STATE_ROOT", tmp_path)
    monkeypatch.setitem(bridge.locks, "douyin", lock)
    monkeypatch.setitem(bridge.processes, "douyin", process)
    write_worker_result(
        tmp_path / "login-douyin",
        classify_worker_exception(
            RuntimeError(SLIDER_MANUAL_VERIFICATION_MESSAGE),
            "login",
        ),
    )

    await bridge.finish_login("douyin", process)

    state = bridge.read_state("douyin")
    assert state["status"] == "manual_verification_required"
    assert "noVNC" in state["message"]
    assert lock.locked() is False


def test_worker_result_protocol_is_atomic_versioned_and_redacted(tmp_path: Path):
    result = classify_worker_exception(
        RuntimeError(
            "download failed for https://cdn.example.test/video.mp4?signature=secret"
        ),
        "staging",
    )

    destination = write_worker_result(tmp_path, result)
    loaded = read_worker_result(tmp_path, expected_phase="staging")

    assert destination.name == "bridge-result.json"
    assert loaded is not None
    assert loaded["code"] == "provider_execution_failed"
    assert loaded["retryable"] is True
    assert "secret" not in loaded["message"]
    assert "[query-redacted]" in loaded["message"]
    assert not list(tmp_path.glob("*.tmp"))


def test_retry_wrapper_diagnostic_uses_and_redacts_underlying_exception():
    class RetryWrapper(RuntimeError):
        def __init__(self):
            super().__init__("opaque retry wrapper")
            self.last_attempt = SimpleNamespace(
                exception=lambda: RuntimeError(
                    "API failed https://example.invalid/items?token=secret"
                )
            )

    diagnostic = safe_exception_diagnostic(RetryWrapper(), "fallback")

    assert diagnostic == (
        "API failed https://example.invalid/items?[query-redacted]"
    )


def test_worker_request_protocol_keeps_signed_values_in_restricted_job_file(
    tmp_path: Path,
):
    signed_value = "https://www.xiaohongshu.com/user/profile/1?xsec_token=secret"
    destination = write_worker_request(
        tmp_path,
        platform="xiaohongshu",
        mode="discover",
        value=signed_value,
        limit=20,
    )

    loaded = read_worker_request(
        tmp_path,
        expected_platform="xiaohongshu",
        expected_mode="discover",
    )

    assert destination.name == "bridge-request.json"
    assert loaded == {"value": signed_value, "limit": 20}
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.asyncio
async def test_spawn_never_places_signed_source_url_in_process_arguments(
    tmp_path: Path,
    monkeypatch,
):
    captured: list[str] = []

    class FakeProcess:
        returncode = None

    async def create_process(*args, **_kwargs):
        captured.extend(str(value) for value in args)
        return FakeProcess()

    monkeypatch.setattr(bridge, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(bridge, "BROWSER_ROOT", tmp_path / "browser")
    monkeypatch.setattr(
        bridge.asyncio,
        "create_subprocess_exec",
        create_process,
    )
    signed_value = (
        "https://www.xiaohongshu.com/user/profile/1?xsec_token=secret"
    )
    output = tmp_path / "job"

    process = await bridge.spawn(
        "xiaohongshu",
        "discover",
        output,
        signed_value,
        20,
    )

    assert isinstance(process, FakeProcess)
    assert signed_value not in captured
    assert read_worker_request(
        output,
        expected_platform="xiaohongshu",
        expected_mode="discover",
    )["value"] == signed_value
    bridge.active_processes.discard(process)


def test_worker_result_classifies_manual_verification_at_worker_boundary():
    result = classify_worker_exception(
        RuntimeError("captcha verification is required"),
        "login",
    )

    assert result["code"] == MANUAL_VERIFICATION_CODE
    assert result["phase"] == "login"
    assert result["retryable"] is True


def test_process_error_ignores_playwright_decoration_and_keeps_actionable_failure():
    output = b"\n".join([
        b"Traceback (most recent call last):",
        b"playwright._impl._errors.Error: BrowserType.launch: Executable doesn't exist at /missing/chrome",
        "╔════════════════════════════╗".encode(),
        b"Looks like Playwright was just installed or updated.",
        "╚════════════════════════════╝".encode(),
    ])

    assert bridge.safe_process_error(output, "fallback") == (
        "playwright._impl._errors.Error: BrowserType.launch: Executable doesn't exist at /missing/chrome"
    )


@pytest.mark.asyncio
async def test_process_timeout_terminates_worker():
    class HangingProcess:
        def __init__(self):
            self.returncode = None
            self.pid = None
            self.terminated = False
            self.done = asyncio.Event()

        async def communicate(self):
            await asyncio.Event().wait()

        def terminate(self):
            self.terminated = True
            self.returncode = -15
            self.done.set()

        async def wait(self):
            await self.done.wait()
            return self.returncode

        def kill(self):
            self.returncode = -9
            self.done.set()

    process = HangingProcess()
    with pytest.raises(asyncio.TimeoutError):
        await bridge.communicate(process, 0.001)
    assert process.terminated is True

    cancelled_process = HangingProcess()
    task = asyncio.create_task(bridge.communicate(cancelled_process, 60))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled_process.terminated is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("snapshot", "status_code"),
    [((11, 100), 413), ((0, 4), 507)],
)
async def test_stage_resource_monitor_stops_worker_before_completion(
    tmp_path: Path,
    monkeypatch,
    snapshot: tuple[int, int],
    status_code: int,
):
    class RunningProcess:
        def __init__(self):
            self.returncode = None
            self.pid = None
            self.stopped = asyncio.Event()

        async def communicate(self):
            await self.stopped.wait()
            return b"", None

        def terminate(self):
            self.returncode = -15
            self.stopped.set()

        async def wait(self):
            await self.stopped.wait()
            return self.returncode

        def kill(self):
            self.returncode = -9
            self.stopped.set()

    process = RunningProcess()
    monkeypatch.setattr(bridge, "stage_resource_snapshot", lambda _root: snapshot)

    with pytest.raises(bridge.StageResourceLimitError) as caught:
        await bridge.communicate(
            process,
            1,
            monitor_root=tmp_path,
            max_bytes=10,
            min_free_bytes=5,
            monitor_interval_seconds=0,
        )

    assert caught.value.status_code == status_code
    assert process.returncode == -15


def test_provider_contract_supplies_canonical_identity_and_explicit_media_slots(tmp_path: Path):
    payload = {
        "schema_version": 1,
        "items": {
            "170001": {
                "canonical_id": "BV1contract",
                "source_url": "https://www.bilibili.com/video/BV1contract",
                "original": True,
                "pinned": False,
                "content_type": "video",
                "expected_media_count": 1,
                "media_slots": [
                    {"ordinal": 1, "kind": "video", "slot_id": "video-001"}
                ],
                "unsupported_media": False,
            }
        },
    }
    (tmp_path / bridge.PROVIDER_CONTRACT_FILENAME).write_text(json.dumps(payload), "utf-8")

    contract = bridge.provider_contract(tmp_path)
    item, metadata = bridge.apply_provider_contract(
        {"remote_id": "170001", "source_url": "https://www.bilibili.com/video/av170001"},
        contract,
    )

    assert item["remote_id"] == "BV1contract"
    assert item["source_url"].endswith("/BV1contract")
    assert item["aliases"] == []
    assert metadata["expected_media_count"] == 1


@pytest.mark.parametrize(
    "case",
    json.loads(PROVIDER_FIXTURES.read_text(encoding="utf-8")),
    ids=lambda case: case["platform"],
)
def test_platform_contract_fixtures_preserve_identity_and_media_semantics(
    tmp_path: Path,
    case: dict,
):
    (tmp_path / bridge.PROVIDER_CONTRACT_FILENAME).write_text(
        json.dumps(case["contract"], ensure_ascii=False),
        encoding="utf-8",
    )

    contract = bridge.provider_contract(
        tmp_path,
        expected_platform=case["platform"],
        expected_mode="stage",
    )
    item, metadata = bridge.apply_provider_contract(
        bridge.normalize(case["platform"], case["raw"]),
        contract,
    )
    expected = case["expected"]
    media = [
        {"local_path": slot["staged_path"], "kind": slot["kind"]}
        for slot in metadata["media_slots"]
    ]
    bound, complete = bridge.bind_staged_media_to_slots(media, metadata)

    assert item["remote_id"] == expected["canonical_id"]
    assert item["aliases"] == expected["aliases"]
    assert item["original"] is expected["original"]
    assert item["content_type"] == expected["content_type"]
    assert [slot["kind"] for slot in metadata["media_slots"]] == expected["slot_kinds"]
    assert metadata["unsupported_media"] is expected["unsupported_media"]
    assert complete is True
    assert [record["slot_id"] for record in bound] == [
        slot["slot_id"] for slot in metadata["media_slots"]
    ]


def test_provider_contract_alias_matches_legacy_requested_identity():
    item = {"remote_id": "BV1contract", "aliases": ["170001"]}

    assert bridge.contract_identity_matches("BV1contract", item) is True
    assert bridge.contract_identity_matches("170001", item) is True
    assert bridge.contract_identity_matches("different", item) is False


def test_provider_contract_rejects_non_contiguous_media_slots(tmp_path: Path):
    payload = {
        "schema_version": 1,
        "items": {
            "42": {
                "canonical_id": "42",
                "source_url": "https://www.douyin.com/video/42",
                "original": True,
                "content_type": "video",
                "expected_media_count": 1,
                "media_slots": [
                    {"ordinal": 2, "kind": "video", "slot_id": "video-001"}
                ],
            }
        },
    }
    (tmp_path / bridge.PROVIDER_CONTRACT_FILENAME).write_text(json.dumps(payload), "utf-8")

    with pytest.raises(ValueError, match="invalid media slots"):
        bridge.apply_provider_contract({"remote_id": "42"}, bridge.provider_contract(tmp_path))


def test_provider_contract_rejects_implicit_text_only_result(tmp_path: Path):
    payload = {
        "schema_version": 1,
        "items": {
            "42": {
                "canonical_id": "42",
                "source_url": "https://m.weibo.cn/detail/42",
                "original": True,
                "content_type": "text",
                "expected_media_count": 0,
            }
        },
    }
    (tmp_path / bridge.PROVIDER_CONTRACT_FILENAME).write_text(json.dumps(payload), "utf-8")

    with pytest.raises(ValueError, match="media slots"):
        bridge.apply_provider_contract({"remote_id": "42"}, bridge.provider_contract(tmp_path))


def test_provider_contract_rejects_wrong_job_identity(tmp_path: Path):
    payload = {
        "schema_version": 1,
        "platform": "weibo",
        "mode": "discover",
        "items": {},
    }
    (tmp_path / bridge.PROVIDER_CONTRACT_FILENAME).write_text(json.dumps(payload), "utf-8")

    with pytest.raises(ValueError, match="platform"):
        bridge.provider_contract(
            tmp_path, expected_platform="douyin", expected_mode="discover"
        )


def test_media_slots_reject_same_kind_file_substitution():
    media = [
        {"local_path": "media/expected-one.jpg", "kind": "image"},
        {"local_path": "media/unrelated.jpg", "kind": "image"},
    ]
    contract = {
        "media_slots": [
            {
                "ordinal": 1,
                "kind": "image",
                "slot_id": "image-001",
                "staged_path": "media/expected-one.jpg",
            },
            {
                "ordinal": 2,
                "kind": "image",
                "slot_id": "image-002",
                "staged_path": "media/expected-two.jpg",
            },
        ]
    }

    bound, complete = bridge.bind_staged_media_to_slots(media, contract)

    assert bound == []
    assert complete is False


def test_media_slots_bind_every_file_once_in_contract_order():
    media = [
        {"local_path": "media/two.jpg", "kind": "image"},
        {"local_path": "media/one.jpg", "kind": "image"},
    ]
    contract = {
        "media_slots": [
            {
                "ordinal": 1,
                "kind": "image",
                "slot_id": "image-001",
                "staged_path": "media/one.jpg",
            },
            {
                "ordinal": 2,
                "kind": "image",
                "slot_id": "image-002",
                "staged_path": "media/two.jpg",
            },
        ]
    }

    bound, complete = bridge.bind_staged_media_to_slots(media, contract)

    assert complete is True
    assert [record["slot_id"] for record in bound] == ["image-001", "image-002"]


def test_staged_media_hashes_files_without_loading_them_whole(tmp_path: Path):
    image = tmp_path / "images" / "one.jpg"
    image.parent.mkdir()
    payload = b"image" * 300_000
    image.write_bytes(payload)

    media, downloaded_count, unrecognized_count = bridge.staged_media(tmp_path)

    assert downloaded_count == 1
    assert unrecognized_count == 0
    assert media[0]["sha256"] == hashlib.sha256(payload).hexdigest()
    assert media[0]["size_bytes"] == len(payload)


def test_staged_media_counts_unknown_json_as_invalid_but_ignores_bridge_files(tmp_path: Path):
    (tmp_path / bridge.PROVIDER_CONTRACT_FILENAME).write_text("{}", "utf-8")
    (tmp_path / "douyin_contents.jsonl").write_text("{}\n", "utf-8")
    (tmp_path / "unexpected.json").write_text("{}", "utf-8")

    media, downloaded_count, unrecognized_count = bridge.staged_media(tmp_path)

    assert media == []
    assert downloaded_count == 0
    assert unrecognized_count == 1


@pytest.mark.asyncio
async def test_incomplete_stage_returns_strict_manifest_and_removes_job(tmp_path: Path, monkeypatch):
    fixed_job_id = "a" * 32

    async def allow_session(_platform: str):
        return None

    async def fake_spawn(_platform: str, _mode: str, output: Path, _value: str, _limit: int):
        output.mkdir(parents=True, exist_ok=True)
        raw = {"aweme_id": "42", "aweme_url": "https://www.douyin.com/video/42", "type": "video"}
        (output / "douyin_contents.jsonl").write_text(json.dumps(raw) + "\n", "utf-8")
        contract = {
            "schema_version": 1,
            "platform": "douyin",
            "mode": "stage",
            "items": {
                "42": {
                    "canonical_id": "42",
                    "source_url": "https://www.douyin.com/video/42",
                    "original": True,
                    "pinned": False,
                    "content_type": "video",
                    "expected_media_count": 1,
                    "media_slots": [
                        {"ordinal": 1, "kind": "video", "slot_id": "video-001"}
                    ],
                    "unsupported_media": False,
                }
            },
        }
        (output / bridge.PROVIDER_CONTRACT_FILENAME).write_text(json.dumps(contract), "utf-8")
        return SimpleNamespace(returncode=0)

    async def fake_communicate(_process, _timeout, **_kwargs):
        return b"", None

    async def allow_public_url(_platform, _url):
        return None

    monkeypatch.setattr(bridge, "STAGING_ROOT", tmp_path)
    monkeypatch.setattr(bridge, "require_session", allow_session)
    monkeypatch.setattr(bridge, "spawn", fake_spawn)
    monkeypatch.setattr(bridge, "communicate", fake_communicate)
    monkeypatch.setattr(bridge, "validate_public_platform_url", allow_public_url)
    monkeypatch.setattr(bridge.uuid, "uuid4", lambda: SimpleNamespace(hex=fixed_job_id))

    result = await bridge.stage(bridge.StageRequest(
        platform="douyin",
        content_id="42",
        source_url="https://www.douyin.com/video/42",
    ))

    assert result["expected_media_count"] == 1
    assert result["downloaded_media_count"] == 0
    assert result["complete"] is False
    assert not (tmp_path / fixed_job_id).exists()


@pytest.mark.asyncio
async def test_stage_rejects_media_kind_substitution(tmp_path: Path, monkeypatch):
    fixed_job_id = "b" * 32

    async def allow_session(_platform: str):
        return None

    async def allow_public_url(_platform, _url):
        return None

    async def fake_spawn(_platform: str, _mode: str, output: Path, _value: str, _limit: int):
        output.mkdir(parents=True, exist_ok=True)
        raw = {"aweme_id": "42", "aweme_url": "https://www.douyin.com/video/42"}
        (output / "douyin_contents.jsonl").write_text(json.dumps(raw) + "\n", "utf-8")
        (output / "wrong-kind.jpg").write_bytes(b"not-empty")
        contract = {
            "schema_version": 1,
            "platform": "douyin",
            "mode": "stage",
            "items": {
                "42": {
                    "canonical_id": "42",
                    "source_url": "https://www.douyin.com/video/42",
                    "original": True,
                    "pinned": False,
                    "content_type": "video",
                    "expected_media_count": 1,
                    "media_slots": [
                        {
                            "ordinal": 1,
                            "kind": "video",
                            "slot_id": "video-001",
                            "staged_path": "wrong-kind.jpg",
                        }
                    ],
                    "unsupported_media": False,
                }
            },
        }
        (output / bridge.PROVIDER_CONTRACT_FILENAME).write_text(json.dumps(contract), "utf-8")
        return SimpleNamespace(returncode=0)

    async def fake_communicate(_process, _timeout, **_kwargs):
        return b"", None

    monkeypatch.setattr(bridge, "STAGING_ROOT", tmp_path)
    monkeypatch.setattr(bridge, "require_session", allow_session)
    monkeypatch.setattr(bridge, "validate_public_platform_url", allow_public_url)
    monkeypatch.setattr(bridge, "spawn", fake_spawn)
    monkeypatch.setattr(bridge, "communicate", fake_communicate)
    monkeypatch.setattr(bridge.uuid, "uuid4", lambda: SimpleNamespace(hex=fixed_job_id))

    result = await bridge.stage(
        bridge.StageRequest(
            platform="douyin",
            content_id="42",
            source_url="https://www.douyin.com/video/42",
        )
    )

    assert result["expected_media_count"] == result["downloaded_media_count"] == 1
    assert result["complete"] is False
    assert "media slots match False" in result["message"]
    assert not (tmp_path / fixed_job_id).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("short_host", ["xhslink.com", "xhslink.cn"])
async def test_short_link_redirect_is_revalidated_before_following(
    monkeypatch,
    short_host,
):
    requested: list[str] = []

    def fake_getaddrinfo(host, port, *_args):
        address = (
            "93.184.216.34"
            if host in {"xhslink.com", "xhslink.cn"}
            else "127.0.0.1"
        )
        return [(2, 1, 6, "", (address, port))]

    class RedirectResponse:
        is_redirect = True
        headers = {"location": "https://www.xiaohongshu.com/user/profile/secret"}

    class StreamContext:
        async def __aenter__(self):
            return RedirectResponse()

        async def __aexit__(self, *_args):
            return None

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def stream(self, _method, url):
            requested.append(url)
            return StreamContext()

    monkeypatch.setattr(bridge.socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(bridge.httpx, "AsyncClient", lambda **_kwargs: FakeClient())

    request = bridge.DiscoverRequest(
        platform="xiaohongshu",
        profile_url=f"https://{short_host}/abc",
    )
    with pytest.raises(HTTPException) as caught:
        await bridge.creator_value(request.platform, request.profile_url)

    assert caught.value.status_code == 422
    assert requested == [f"https://{short_host}/abc"]


@pytest.mark.asyncio
async def test_platform_url_rejects_embedded_credentials_before_dns_lookup(monkeypatch):
    resolved = False

    def should_not_resolve(*_args):
        nonlocal resolved
        resolved = True
        return []

    monkeypatch.setattr(bridge.socket, "getaddrinfo", should_not_resolve)
    request = bridge.DiscoverRequest(
        platform="bilibili",
        profile_url="https://username:password@space.bilibili.com/123",
    )

    with pytest.raises(HTTPException) as caught:
        await bridge.validate_public_platform_url(request.platform, request.profile_url)

    assert caught.value.status_code == 422
    assert "credentials" in caught.value.detail
    assert resolved is False


@pytest.mark.asyncio
async def test_platform_url_accepts_only_known_fake_ip_ranges_when_opted_in(monkeypatch):
    request = bridge.DiscoverRequest(
        platform="douyin",
        profile_url="https://v.douyin.com/abc",
    )

    def fake_getaddrinfo(_host, port, *_args):
        return [
            (2, 1, 6, "", ("198.18.0.25", port)),
            (10, 1, 6, "", ("fdfe:dcba:9876::10", port)),
        ]

    monkeypatch.setattr(bridge.socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(bridge, "ALLOW_FAKE_IP_DNS", False)
    with pytest.raises(HTTPException, match="Fake-IP"):
        await bridge.validate_public_platform_url(request.platform, request.profile_url)

    monkeypatch.setattr(bridge, "ALLOW_FAKE_IP_DNS", True)
    await bridge.validate_public_platform_url(request.platform, request.profile_url)


def _fake_package(monkeypatch, name: str, **children: ModuleType) -> ModuleType:
    package = ModuleType(name)
    package.__path__ = []
    for child_name, child in children.items():
        setattr(package, child_name, child)
    monkeypatch.setitem(sys.modules, name, package)
    return package


@pytest.mark.asyncio
async def test_pinned_xhs_fields_define_every_downloaded_image_and_video(monkeypatch):
    calls: list[dict] = []
    store_module = ModuleType("store.xhs")

    async def original(item):
        calls.append(item)

    store_module.update_xhs_note = original
    store_module.get_video_url_arr = lambda _item: ["https://cdn/video.mp4"]
    _fake_package(monkeypatch, "store", xhs=store_module)
    monkeypatch.setitem(sys.modules, "store.xhs", store_module)
    contract = {"items": {}}

    worker.install_provider_contract(
        SimpleNamespace(platform="xiaohongshu", mode="stage"),
        SimpleNamespace(),
        SimpleNamespace(),
        contract,
    )
    await store_module.update_xhs_note(
        {
            "note_id": "xhs-1",
            "xsec_token": "token",
            "xsec_source": "pc_user",
            "image_list": [
                {"url_default": "https://cdn/1.jpg"},
                {"url_default": "", "url": "https://cdn/2.jpg"},
                # The pinned downloader overwrites this fallback with None.
                {"url": "https://cdn/not-downloaded.jpg"},
            ],
        }
    )

    item = contract["items"]["xhs-1"]
    assert [slot["kind"] for slot in item["media_slots"]] == ["image", "image", "video"]
    assert [slot["slot_id"] for slot in item["media_slots"]] == [
        "image-001",
        "image-002",
        "video-001",
    ]
    assert all("source_sha256" in slot for slot in item["media_slots"])
    assert item["expected_media_count"] == 3
    assert calls and item["source_url"].endswith("xsec_source=pc_user")


@pytest.mark.asyncio
async def test_pinned_douyin_note_fields_use_note_identity_and_image_precedence(monkeypatch):
    store_module = ModuleType("store.douyin")

    async def original(_item):
        return None

    store_module.update_douyin_aweme = original
    store_module._extract_note_image_list = lambda item: item["image_urls"]
    store_module._extract_video_download_url = lambda item: item["video_url"]
    _fake_package(monkeypatch, "store", douyin=store_module)
    monkeypatch.setitem(sys.modules, "store.douyin", store_module)
    contract = {"items": {}}

    worker.install_provider_contract(
        SimpleNamespace(platform="douyin", mode="stage"),
        SimpleNamespace(),
        SimpleNamespace(),
        contract,
    )
    await store_module.update_douyin_aweme(
        {
            "aweme_id": "7525",
            "image_urls": ["https://cdn/1", "https://cdn/2", "https://cdn/3"],
            "video_url": "https://cdn/video",
            "is_repost": "0",
        }
    )

    item = contract["items"]["7525"]
    assert item["canonical_id"] == "7525"
    assert item["source_url"] == "https://www.douyin.com/note/7525"
    assert [slot["kind"] for slot in item["media_slots"]] == ["image"] * 3
    assert [slot["slot_id"] for slot in item["media_slots"]] == [
        "image-001",
        "image-002",
        "image-003",
    ]
    assert item["original"] is True
    assert worker.normalize_stage_value("douyin", item["source_url"]) == "7525"


@pytest.mark.asyncio
async def test_pinned_bilibili_aid_maps_to_stable_bv_identity(monkeypatch):
    store_module = ModuleType("store.bilibili")

    async def original(_item):
        return None

    store_module.update_bilibili_video = original
    _fake_package(monkeypatch, "store", bilibili=store_module)
    monkeypatch.setitem(sys.modules, "store.bilibili", store_module)
    help_module = ModuleType("media_platform.bilibili.help")
    help_module.parse_video_info_from_url = lambda value: SimpleNamespace(video_id=value)
    bilibili_package = ModuleType("media_platform.bilibili")
    bilibili_package.__path__ = []
    bilibili_package.help = help_module
    _fake_package(monkeypatch, "media_platform", bilibili=bilibili_package)
    monkeypatch.setitem(sys.modules, "media_platform.bilibili", bilibili_package)
    monkeypatch.setitem(sys.modules, "media_platform.bilibili.help", help_module)
    crawler = SimpleNamespace()
    contract = {"items": {}}

    worker.install_provider_contract(
        SimpleNamespace(platform="bilibili", mode="stage"),
        crawler,
        SimpleNamespace(MAX_CONCURRENCY_NUM=1),
        contract,
    )
    await store_module.update_bilibili_video(
        {
            "View": {
                "aid": 170001,
                "bvid": "BV1contract",
                "copyright": 1,
                "is_top": "0",
            }
        }
    )

    item = contract["items"]["170001"]
    assert item["canonical_id"] == "BV1contract"
    assert item["source_url"].endswith("/BV1contract")
    assert item["aliases"] == ["170001"]
    assert item["pinned"] is False
    assert item["original"] is True

    await store_module.update_bilibili_video(
        {
            "View": {
                "aid": 170002,
                "bvid": "BV1repost",
                "copyright": 2,
            }
        }
    )
    assert contract["items"]["170002"]["original"] is False


@pytest.mark.asyncio
async def test_bilibili_stage_streams_every_page_and_durl_segment(tmp_path: Path, monkeypatch):
    store_module = ModuleType("store.bilibili")

    async def original(_item):
        return None

    store_module.update_bilibili_video = original
    _fake_package(monkeypatch, "store", bilibili=store_module)
    monkeypatch.setitem(sys.modules, "store.bilibili", store_module)
    help_module = ModuleType("media_platform.bilibili.help")
    help_module.parse_video_info_from_url = lambda value: SimpleNamespace(video_id=value)
    bilibili_package = ModuleType("media_platform.bilibili")
    bilibili_package.__path__ = []
    bilibili_package.help = help_module
    _fake_package(monkeypatch, "media_platform", bilibili=bilibili_package)
    monkeypatch.setitem(sys.modules, "media_platform.bilibili", bilibili_package)
    monkeypatch.setitem(sys.modules, "media_platform.bilibili.help", help_module)

    payloads = {
        "https://cdn/p1-1": b"page-one-segment-one",
        "https://cdn/p1-2": b"page-one-segment-two",
        "https://cdn/p2-1": b"page-two-segment-one",
    }

    class FakeResponse:
        is_redirect = False

        def __init__(self, payload: bytes):
            self.payload = payload
            self.headers = {"content-length": str(len(payload))}

        def raise_for_status(self):
            return None

        async def aiter_bytes(self, _chunk_size):
            midpoint = len(self.payload) // 2
            yield self.payload[:midpoint]
            yield self.payload[midpoint:]

    class StreamContext:
        def __init__(self, response):
            self.response = response

        async def __aenter__(self):
            return self.response

        async def __aexit__(self, *_args):
            return None

    class FakeHttpClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def stream(self, _method, url, **_kwargs):
            return StreamContext(FakeResponse(payloads[url]))

    async def allow_public_media(_url):
        return None

    monkeypatch.setattr(worker.httpx, "AsyncClient", lambda **_kwargs: FakeHttpClient())
    monkeypatch.setattr(worker, "_validate_public_media_url", allow_public_media)

    class FakeCrawler:
        def __init__(self):
            self.bili_client = SimpleNamespace(headers={"Referer": "https://bilibili.com"}, timeout=10)

        async def get_video_play_url_task(self, _aid, cid, _semaphore):
            if cid == 11:
                return {"durl": [{"url": "https://cdn/p1-1"}, {"url": "https://cdn/p1-2"}]}
            return {"durl": [{"url": "https://cdn/p2-1"}]}

    crawler = FakeCrawler()
    contract = {"items": {}}
    config = SimpleNamespace(
        MAX_CONCURRENCY_NUM=1,
        ENABLE_GET_MEIDAS=True,
        SAVE_DATA_PATH=str(tmp_path),
        CRAWLER_MAX_SLEEP_SEC=0,
    )
    worker.install_provider_contract(
        SimpleNamespace(platform="bilibili", mode="stage"),
        crawler,
        config,
        contract,
    )
    detail = {
        "View": {
            "aid": 170001,
            "bvid": "BV1contract",
            "copyright": 1,
            "cid": 11,
            "pages": [{"cid": 11}, {"cid": 22}],
        }
    }
    await store_module.update_bilibili_video(detail)
    await crawler.get_bilibili_video(detail, asyncio.Semaphore(1))

    item = contract["items"]["170001"]
    files = sorted((tmp_path / "bili" / "videos" / "170001").glob("*.mp4"))
    assert [slot["kind"] for slot in item["media_slots"]] == ["video"] * 3
    assert item["expected_media_count"] == 3
    assert item["unsupported_media"] is False
    assert [slot["staged_path"] for slot in item["media_slots"]] == [
        "bili/videos/170001/p001-segment001.mp4",
        "bili/videos/170001/p001-segment002.mp4",
        "bili/videos/170001/p002-segment001.mp4",
    ]
    assert [path.name for path in files] == [
        "p001-segment001.mp4",
        "p001-segment002.mp4",
        "p002-segment001.mp4",
    ]
    assert [path.read_bytes() for path in files] == [payloads[key] for key in payloads]


@pytest.mark.asyncio
async def test_streaming_media_rejects_content_length_before_reading(tmp_path: Path, monkeypatch):
    iterated = False

    class FakeResponse:
        is_redirect = False
        headers = {"content-length": "11"}

        def raise_for_status(self):
            return None

        async def aiter_bytes(self, _chunk_size):
            nonlocal iterated
            iterated = True
            yield b"too-large"

    class StreamContext:
        async def __aenter__(self):
            return FakeResponse()

        async def __aexit__(self, *_args):
            return None

    class FakeClient:
        def stream(self, *_args, **_kwargs):
            return StreamContext()

    async def allow_public_media(_url):
        return None

    monkeypatch.setattr(worker, "_validate_public_media_url", allow_public_media)
    destination = tmp_path / "video.mp4"
    downloaded, written, limited = await worker._stream_http_media(
        FakeClient(),
        "https://cdn/video",
        destination,
        headers={},
        timeout=10,
        max_bytes=10,
    )

    assert (downloaded, written, limited) == (False, 0, True)
    assert iterated is False
    assert not destination.exists()


@pytest.mark.asyncio
async def test_streaming_media_removes_partial_file_when_chunks_cross_limit(
    tmp_path: Path, monkeypatch
):
    class FakeResponse:
        is_redirect = False
        headers = {}

        def raise_for_status(self):
            return None

        async def aiter_bytes(self, _chunk_size):
            yield b"123456"
            yield b"789012"

    class StreamContext:
        async def __aenter__(self):
            return FakeResponse()

        async def __aexit__(self, *_args):
            return None

    class FakeClient:
        def stream(self, *_args, **_kwargs):
            return StreamContext()

    async def allow_public_media(_url):
        return None

    monkeypatch.setattr(worker, "_validate_public_media_url", allow_public_media)
    destination = tmp_path / "video.mp4"
    downloaded, written, limited = await worker._stream_http_media(
        FakeClient(),
        "https://cdn/video",
        destination,
        headers={},
        timeout=10,
        max_bytes=10,
    )

    assert (downloaded, written, limited) == (False, 6, True)
    assert not destination.exists()
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.asyncio
async def test_pinned_weibo_fields_mark_video_unsupported_without_losing_images(monkeypatch):
    store_module = ModuleType("store.weibo")

    async def original(_item):
        return None

    store_module.update_weibo_note = original
    _fake_package(monkeypatch, "store", weibo=store_module)
    monkeypatch.setitem(sys.modules, "store.weibo", store_module)
    contract = {"items": {}}

    note_item = {
        "mblog": {
            "id": "501",
            "bid": "Plegacy",
            "pics": [
                {
                    "pid": "one",
                    "url": "https://cdn/thumb.jpg",
                    "large": {"url": "https://cdn/original.jpg"},
                },
                "https://cdn/2.jpg",
            ],
            "page_info": {
                "type": "video",
                "media_info": {"stream_url_hd": "https://cdn/video.mp4"},
            },
        }
    }
    downloaded_mblogs: list[dict] = []

    async def get_note_info_task(**_kwargs):
        return note_item

    async def get_note_images(mblog):
        downloaded_mblogs.append(mblog)

    async def batch_comments(_ids):
        return None

    crawler = SimpleNamespace(
        get_note_info_task=get_note_info_task,
        get_note_images=get_note_images,
        batch_get_notes_comments=batch_comments,
    )
    worker.install_provider_contract(
        SimpleNamespace(platform="weibo", mode="stage"),
        crawler,
        SimpleNamespace(MAX_CONCURRENCY_NUM=1, WEIBO_SPECIFIED_ID_LIST=["501"]),
        contract,
    )
    await store_module.update_weibo_note(note_item)
    await crawler.get_specified_notes()

    item = contract["items"]["501"]
    assert [slot["kind"] for slot in item["media_slots"]] == ["image", "image"]
    assert [slot["slot_id"] for slot in item["media_slots"]] == [
        "image-001",
        "image-002",
    ]
    assert item["unsupported_media"] is True
    assert item["content_type"] == "video"
    assert item["aliases"] == ["Plegacy"]
    assert downloaded_mblogs[0]["pics"] == [
        {"url": "https://cdn/original.jpg", "pid": "one"},
        "https://cdn/2.jpg",
    ]


@pytest.mark.asyncio
async def test_douyin_discovery_stops_at_limit_and_marks_truncation(monkeypatch):
    store_module = ModuleType("store.douyin")

    async def original(_item):
        return None

    store_module.update_douyin_aweme = original
    store_module._extract_note_image_list = lambda _item: []
    store_module._extract_video_download_url = lambda _item: "https://cdn/video"
    _fake_package(monkeypatch, "store", douyin=store_module)
    monkeypatch.setitem(sys.modules, "store.douyin", store_module)

    class FakeDouYinClient:
        def __init__(self):
            self.calls = 0

        async def get_user_aweme_posts(self, _sec_user_id, _cursor):
            self.calls += 1
            if self.calls == 1:
                items = [
                    {"aweme_id": "repost", "is_repost": 1},
                    {"aweme_id": "1"},
                    {"aweme_id": "2"},
                ]
            else:
                items = [{"aweme_id": "3"}, {"aweme_id": "4"}]
            return {
                "aweme_list": items,
                "has_more": 1,
                "max_cursor": str(self.calls),
            }

    client_module = ModuleType("media_platform.douyin.client")
    client_module.DouYinClient = FakeDouYinClient
    douyin_package = ModuleType("media_platform.douyin")
    douyin_package.__path__ = []
    douyin_package.client = client_module
    _fake_package(monkeypatch, "media_platform", douyin=douyin_package)
    monkeypatch.setitem(sys.modules, "media_platform.douyin", douyin_package)
    monkeypatch.setitem(sys.modules, "media_platform.douyin.client", client_module)
    contract = {"items": {}, "discovery": {}}
    captured: list[str] = []

    worker.install_provider_contract(
        SimpleNamespace(platform="douyin", mode="discover"),
        SimpleNamespace(),
        SimpleNamespace(CRAWLER_MAX_NOTES_COUNT=3),
        contract,
    )
    client = FakeDouYinClient()
    result = await client.get_all_user_aweme_posts(
        "creator",
        callback=lambda items: _capture_ids(items, captured, "aweme_id"),
    )

    assert [item["aweme_id"] for item in result] == ["1", "2", "3"]
    assert captured == ["1", "2", "3"]
    assert client.calls == 2
    assert contract["discovery"]["truncated"] is True


async def _capture_ids(items: list[dict], destination: list[str], key: str) -> None:
    destination.extend(str(item[key]) for item in items)


@pytest.mark.asyncio
async def test_weibo_discovery_patch_does_not_require_an_initialized_client(monkeypatch):
    store_module = ModuleType("store.weibo")

    async def original(_item):
        return None

    store_module.update_weibo_note = original
    _fake_package(monkeypatch, "store", weibo=store_module)
    monkeypatch.setitem(sys.modules, "store.weibo", store_module)

    class FakeWeiboClient:
        async def get_notes_by_creator(self, _creator_id, _container_id, since_id):
            if not since_id:
                return {
                    "cards": [
                        {
                            "card_type": 9,
                            "mblog": {"id": "repost", "retweeted_status": {"id": "original"}},
                        },
                        {"card_type": 9, "mblog": {"id": "1"}},
                        {"card_type": 9, "mblog": {"id": "2"}},
                    ],
                    "cardlistInfo": {"since_id": "next", "total": 3},
                }
            return {
                "cards": [
                    {"card_type": 9, "mblog": {"id": "2"}},
                    {"card_type": 9, "mblog": {"id": "3"}},
                ],
                "cardlistInfo": {"since_id": "0", "total": 3},
            }

    client_module = ModuleType("media_platform.weibo.client")
    client_module.WeiboClient = FakeWeiboClient
    weibo_package = ModuleType("media_platform.weibo")
    weibo_package.__path__ = []
    weibo_package.client = client_module
    _fake_package(monkeypatch, "media_platform", weibo=weibo_package)
    monkeypatch.setitem(sys.modules, "media_platform.weibo", weibo_package)
    monkeypatch.setitem(sys.modules, "media_platform.weibo.client", client_module)
    contract = {"items": {}, "discovery": {}}

    # The real crawler assigns wb_client only inside start(); installation must
    # therefore patch the pinned client class rather than an absent instance.
    worker.install_provider_contract(
        SimpleNamespace(platform="weibo", mode="discover"),
        SimpleNamespace(),
        SimpleNamespace(CRAWLER_MAX_NOTES_COUNT=3),
        contract,
    )
    result = await FakeWeiboClient().get_all_notes_by_creator_id(
        "creator", "container", crawl_interval=0
    )

    assert [card["mblog"]["id"] for card in result] == ["1", "2", "3"]
    assert contract["discovery"]["truncated"] is False


@pytest.mark.asyncio
async def test_weibo_discovery_keeps_newest_pages_when_a_later_page_fails(
    monkeypatch,
):
    store_module = ModuleType("store.weibo")

    async def original(_item):
        return None

    store_module.update_weibo_note = original
    _fake_package(monkeypatch, "store", weibo=store_module)
    monkeypatch.setitem(sys.modules, "store.weibo", store_module)

    class FakeWeiboClient:
        calls = 0

        async def get_notes_by_creator(self, _creator_id, _container_id, _since_id):
            self.calls += 1
            if self.calls == 1:
                return {
                    "cards": [
                        {"card_type": 9, "mblog": {"id": "newest-1"}},
                        {"card_type": 9, "mblog": {"id": "newest-2"}},
                    ],
                    "cardlistInfo": {"since_id": "next"},
                }
            raise RuntimeError(
                "page rejected https://m.weibo.cn/api?token=secret"
            )

    client_module = ModuleType("media_platform.weibo.client")
    client_module.WeiboClient = FakeWeiboClient
    weibo_package = ModuleType("media_platform.weibo")
    weibo_package.__path__ = []
    weibo_package.client = client_module
    _fake_package(monkeypatch, "media_platform", weibo=weibo_package)
    monkeypatch.setitem(sys.modules, "media_platform.weibo", weibo_package)
    monkeypatch.setitem(sys.modules, "media_platform.weibo.client", client_module)
    contract = {"items": {}, "discovery": {}}

    worker.install_provider_contract(
        SimpleNamespace(platform="weibo", mode="discover"),
        SimpleNamespace(),
        SimpleNamespace(CRAWLER_MAX_NOTES_COUNT=500),
        contract,
    )
    result = await FakeWeiboClient().get_all_notes_by_creator_id(
        "creator",
        "container",
        crawl_interval=0,
    )

    assert [card["mblog"]["id"] for card in result] == [
        "newest-1",
        "newest-2",
    ]
    assert contract["discovery"] == {
        "truncated": True,
        "partial_failure": {
            "code": "provider_page_failed",
            "message": (
                "page rejected "
                "https://m.weibo.cn/api?[query-redacted]"
            ),
        },
    }
