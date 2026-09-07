from __future__ import annotations

import os
import threading
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import main as main_module
from app.build_info import APP_ID, APP_VERSION, BUILD_ID, calculate_build_id
from app.errors import TemporaryAccessError
from app.main import app
from app.models import (
    DownloadItem,
    DownloadJob,
    ItemStatus,
    JobStatus,
    Platform,
    SourceKind,
)
from app.runtime import clear_runtime_identity, configure_runtime_identity


def test_health_endpoint_accepts_local_host_and_rejects_dns_rebinding(
    monkeypatch,
) -> None:
    configure_runtime_identity(
        instance_id="a" * 32,
        stop_token="test_secret_" + "b" * 32,
        server_port=18765,
    )
    client = TestClient(app, base_url="http://localhost")
    try:
        assert client.get("/api/health").json() == {
            "status": "ok",
            "app_id": APP_ID,
            "version": APP_VERSION,
            "build_id": BUILD_ID,
            "source_build_id": calculate_build_id(),
            "restart_required": False,
            "instance_id": "a" * 32,
            "server_pid": os.getpid(),
            "server_port": 18765,
        }
        assert (
            client.get("/api/health", headers={"host": "attacker.example"}).status_code
            == 400
        )
    finally:
        client.close()
        clear_runtime_identity(instance_id="a" * 32)


def test_runtime_stop_requires_matching_secret_and_stops_asynchronously(
    monkeypatch,
) -> None:
    stopped = threading.Event()
    instance_id = "c" * 32
    configure_runtime_identity(
        instance_id=instance_id,
        stop_token="runtime_test_secret_" + "d" * 32,
        server_port=18766,
    )
    monkeypatch.setattr(main_module, "_request_process_stop", stopped.set)
    client = TestClient(app, base_url="http://localhost")
    try:
        assert client.post("/api/runtime/stop").status_code == 403
        assert (
            client.post(
                "/api/runtime/stop",
                headers={"X-Original-Media-Stop-Token": "wrong-secret"},
            ).status_code
            == 403
        )
        response = client.post(
            "/api/runtime/stop",
            headers={
                "X-Original-Media-Stop-Token": "runtime_test_secret_" + "d" * 32
            },
        )
        assert response.status_code == 200
        assert response.json() == {
            "status": "stopping",
            "instance_id": instance_id,
        }
        assert stopped.wait(timeout=1)
    finally:
        client.close()
        clear_runtime_identity(instance_id=instance_id)


def test_runtime_stop_is_disabled_without_managed_runtime_token(
    monkeypatch,
) -> None:
    clear_runtime_identity(instance_id="unmanaged")
    client = TestClient(app, base_url="http://localhost")
    try:
        assert client.post("/api/runtime/stop").status_code == 503
    finally:
        client.close()


def test_index_injects_build_identity_and_disables_html_cache() -> None:
    client = TestClient(app, base_url="http://localhost")
    try:
        response = client.get("/")
        static_response = client.get(f"/static/app.js?v={BUILD_ID}")
        styles_response = client.get(f"/static/styles.css?v={BUILD_ID}")
    finally:
        client.close()

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert "__APP_ID__" not in response.text
    assert "__APP_VERSION__" not in response.text
    assert "__BUILD_ID__" not in response.text
    assert f'<meta name="app-build" content="{BUILD_ID}">' in response.text
    assert f'/static/app.js?v={BUILD_ID}' in response.text

    assert static_response.status_code == 200
    assert static_response.headers["cache-control"] == "no-store, max-age=0"
    assert styles_response.status_code == 200
    assert styles_response.headers["cache-control"] == "no-store, max-age=0"
    for progress_marker in (
        "Checking Douyin Live Photo quality",
        "Checking Douyin direct quality",
        "Checking Douyin quality",
        "Retrying Douyin quality",
        "正在检测抖音 Live Photo 最高画质",
        "正在检测抖音直连候选画质",
        "正在重试",
    ):
        assert progress_marker in static_response.text
    for classification_marker in (
        "这是旧版本把通用签名失败误标成了验证码",
        "抖音返回了其他视频或其他作者的数据，程序已拦截",
        "抖音签名解析在拿到可验证响应前遇到临时网络或超时错误",
        "只有抖音明确显示验证码或登录页面时才需要打开 Chrome",
        "小红书任务中的作品身份或主页归属无法验证",
        "小红书短链接这次跳到了与首次解析不同的作品或主页",
        "最终下载文件与已验证的最高画质不一致",
        "这是旧版本留下的不完整抖音主页队列",
        "已保留的旧抖音文件",
        "FFprobe 未返回码率或完整媒体大小",
        "invalid-host”不是实际域名",
        "主机名可能包含敏感标识",
        "媒体地址被降级为非 HTTPS",
        "跳转目标是 IP 地址，不是可验证的官方 CDN 域名",
        "跳转目标是本地、内网或保留用途域名",
        "缺少把它绑定到该作品最高画质的完整校验指纹",
        "CDN 域名族",
        "校验指纹",
        "不要发送带签名的完整媒体链接",
        "这是旧版本保存的抖音媒体跳转错误",
        "旧版没有记录实际 CDN 主机",
        "旧版抖音短链任务保存的跳转错误",
    ):
        assert classification_marker in static_response.text
    assert static_response.text.index(
        'text.includes("media endpoint redirected to an unrecognized Douyin CDN host")'
    ) < static_response.text.index(
        'text.includes("Douyin Live Photo authoritative quality source was temporarily unavailable")'
    )
    assert "warningPresentation(job)" in static_response.text
    assert "showIssueToast(job" in static_response.text
    assert "localizedPrimaryJobIssueMessage(job)" in static_response.text
    assert "抖音暂时无法创建经过验证的请求。请在 Chrome" not in static_response.text
    item_error_rule = styles_response.text.rsplit(".item-error {", 1)[1].split("}", 1)[0]
    for declaration in (
        "overflow: visible;",
        "overflow-wrap: anywhere;",
        "text-overflow: clip;",
        "white-space: pre-line;",
    ):
        assert declaration in item_error_rule


def test_douyin_item_verification_always_opens_original_video(
    monkeypatch, tmp_path
) -> None:
    source_url = "https://www.douyin.com/video/7664225419386607205"
    opened = []
    job = DownloadJob(
        id="direct-item",
        source_url=source_url,
        platform=Platform.DOUYIN,
        source_kind=SourceKind.ITEM,
        output_root=str(tmp_path),
        status=JobStatus.NEEDS_AUTH,
        verification_url="https://www.douyin.com/user/wrong-profile",
    )
    monkeypatch.setattr(main_module.manager, "get_job", lambda job_id: job)
    monkeypatch.setattr(
        main_module,
        "_open_chrome",
        lambda url, profile: opened.append((url, profile)),
    )

    response = main_module.open_verification(job.id)

    assert response == {"status": "opened", "url": source_url}
    assert opened == [(source_url, None)]


def test_verification_rejects_job_that_does_not_currently_require_auth(
    monkeypatch,
    tmp_path,
) -> None:
    source_url = "https://www.douyin.com/video/7664225419386607205"
    job = DownloadJob(
        id="not-auth-blocked",
        source_url=source_url,
        platform=Platform.DOUYIN,
        source_kind=SourceKind.ITEM,
        output_root=str(tmp_path),
        status=JobStatus.FAILED,
    )
    monkeypatch.setattr(main_module.manager, "get_job", lambda job_id: job)
    monkeypatch.setattr(
        main_module,
        "_open_chrome",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Chrome must not open for a non-auth failure")
        ),
    )

    with pytest.raises(HTTPException) as error:
        main_module.open_verification(job.id)

    assert error.value.status_code == 409
    assert "does not currently require" in str(error.value.detail)


def test_douyin_profile_verification_never_opens_untrusted_url(
    monkeypatch, tmp_path
) -> None:
    source_url = "https://www.douyin.com/user/MS4wLjABAAAATEST"
    opened = []
    job = DownloadJob(
        id="profile-verification",
        source_url=source_url,
        platform=Platform.DOUYIN,
        source_kind=SourceKind.PROFILE,
        output_root=str(tmp_path),
        status=JobStatus.NEEDS_AUTH,
        verification_url="https://evil.example/phish",
    )
    monkeypatch.setattr(main_module.manager, "get_job", lambda job_id: job)
    monkeypatch.setattr(
        main_module,
        "_open_chrome",
        lambda url, profile: opened.append((url, profile)),
    )

    response = main_module.open_verification(job.id)

    assert response == {"status": "opened", "url": source_url}
    assert opened == [(source_url, None)]


def test_xiaohongshu_profile_verification_opens_bound_needs_auth_note(
    monkeypatch, tmp_path
) -> None:
    profile_id = "62f8ad0b000000001e01f1b9"
    note_id = "693e3a810000000019025182"
    source_url = f"https://www.xiaohongshu.com/user/profile/{profile_id}"
    note_url = (
        f"https://www.xiaohongshu.com/explore/{note_id}"
        "?xsec_token=SAFE_TOKEN%3D&xsec_source=pc_user"
    )
    job = DownloadJob(
        id="xhs-profile-verification",
        source_url=source_url,
        platform=Platform.XIAOHONGSHU,
        source_kind=SourceKind.PROFILE,
        output_root=str(tmp_path),
        status=JobStatus.NEEDS_AUTH,
        verification_url=note_url,
        cookie_profile="Profile 1",
        items=[
            DownloadItem(
                id="blocked-note",
                media_id=note_id,
                source_url=note_url,
                status=ItemStatus.NEEDS_AUTH,
                metadata={
                    "xiaohongshu_profile_id": profile_id,
                    "profile_note_membership_verified": True,
                },
            )
        ],
    )
    opened: list[tuple[str, str | None]] = []
    monkeypatch.setattr(main_module.manager, "get_job", lambda job_id: job)
    monkeypatch.setattr(
        main_module,
        "_open_chrome",
        lambda url, profile: opened.append((url, profile)),
    )

    response = main_module.open_verification(job.id)

    assert opened == [(note_url, "Profile 1")]
    assert response["status"] == "opened"
    assert "xsec_token" not in response["url"]
    assert response["url"].endswith("xsec_source=pc_user")


@pytest.mark.parametrize(
    ("stored_profile", "auto_selected"),
    [(None, False), ("Profile 9", True)],
)
def test_xiaohongshu_verification_refreshes_automatic_profile_before_opening(
    monkeypatch,
    tmp_path,
    stored_profile: str | None,
    auto_selected: bool,
) -> None:
    profile_id = "62f8ad0b000000001e01f1b9"
    note_id = "693e3a810000000019025182"
    source_url = f"https://www.xiaohongshu.com/user/profile/{profile_id}"
    note_url = f"https://www.xiaohongshu.com/explore/{note_id}"
    job = DownloadJob(
        id="legacy-xhs-null-profile-verification",
        source_url=source_url,
        platform=Platform.XIAOHONGSHU,
        source_kind=SourceKind.PROFILE,
        output_root=str(tmp_path),
        status=JobStatus.NEEDS_AUTH,
        verification_url=note_url,
        cookie_browser="chrome",
        cookie_profile=stored_profile,
        cookie_profile_auto_selected=auto_selected,
        items=[
            DownloadItem(
                id="blocked-note",
                media_id=note_id,
                source_url=note_url,
                status=ItemStatus.NEEDS_AUTH,
                metadata={
                    "xiaohongshu_profile_id": profile_id,
                    "profile_note_membership_verified": True,
                },
            )
        ],
    )
    bound_job = job.model_copy(
        deep=True,
        update={
            "cookie_profile": "Profile 3",
            "cookie_profile_auto_selected": True,
        },
    )
    bound_calls: list[str] = []
    opened: list[tuple[str, str | None]] = []
    monkeypatch.setattr(main_module.manager, "get_job", lambda job_id: job)
    monkeypatch.setattr(
        main_module.manager,
        "bind_xiaohongshu_verification_profile",
        lambda job_id: bound_calls.append(job_id) or bound_job,
    )
    monkeypatch.setattr(
        main_module,
        "_open_chrome",
        lambda url, profile: opened.append((url, profile)),
    )

    response = main_module.open_verification(job.id)

    assert bound_calls == [job.id]
    assert opened == [(note_url, "Profile 3")]
    assert response == {"status": "opened", "url": note_url}


def test_legacy_xiaohongshu_verification_without_session_does_not_open_chrome(
    monkeypatch,
    tmp_path,
) -> None:
    source_url = "https://www.xiaohongshu.com/user/profile/example"
    job = DownloadJob(
        id="legacy-xhs-no-verification-profile",
        source_url=source_url,
        platform=Platform.XIAOHONGSHU,
        source_kind=SourceKind.PROFILE,
        output_root=str(tmp_path),
        status=JobStatus.NEEDS_AUTH,
        cookie_browser="chrome",
        cookie_profile=None,
    )
    monkeypatch.setattr(main_module.manager, "get_job", lambda job_id: job)
    monkeypatch.setattr(
        main_module.manager,
        "bind_xiaohongshu_verification_profile",
        lambda job_id: (_ for _ in ()).throw(
            TemporaryAccessError("No authenticated Xiaohongshu Chrome profile")
        ),
    )
    monkeypatch.setattr(
        main_module,
        "_open_chrome",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Chrome must not open with an unbound default profile")
        ),
    )

    with pytest.raises(HTTPException) as error:
        main_module.open_verification(job.id)

    assert error.value.status_code == 400
    assert "No authenticated" in str(error.value.detail)


def test_xiaohongshu_profile_verification_accepts_refreshed_canonical_note(
    monkeypatch, tmp_path
) -> None:
    profile_id = "62f8ad0b000000001e01f1b9"
    note_id = "693e3a810000000019025182"
    source_url = f"https://www.xiaohongshu.com/user/profile/{profile_id}"
    saved_url = (
        f"https://www.xiaohongshu.com/explore/{note_id}"
        "?xsec_token=expired&xsec_source=pc_user"
    )
    refreshed_url = f"https://www.xiaohongshu.com/explore/{note_id}"
    job = DownloadJob(
        id="xhs-profile-canonical-verification",
        source_url=source_url,
        platform=Platform.XIAOHONGSHU,
        source_kind=SourceKind.PROFILE,
        output_root=str(tmp_path),
        status=JobStatus.NEEDS_AUTH,
        verification_url=refreshed_url,
        cookie_profile="Profile 1",
        items=[
            DownloadItem(
                id="blocked-note",
                media_id=note_id,
                source_url=saved_url,
                status=ItemStatus.NEEDS_AUTH,
                metadata={
                    "xiaohongshu_profile_id": profile_id,
                    "profile_note_membership_verified": True,
                },
            )
        ],
    )
    opened: list[tuple[str, str | None]] = []
    monkeypatch.setattr(main_module.manager, "get_job", lambda job_id: job)
    monkeypatch.setattr(
        main_module,
        "_open_chrome",
        lambda url, profile: opened.append((url, profile)),
    )

    response = main_module.open_verification(job.id)

    assert opened == [(refreshed_url, "Profile 1")]
    assert response == {"status": "opened", "url": refreshed_url}


def test_xiaohongshu_short_link_verification_opens_bound_note(
    monkeypatch, tmp_path
) -> None:
    note_id = "693e3a810000000019025182"
    note_url = f"https://www.xiaohongshu.com/explore/{note_id}"
    job = DownloadJob(
        id="xhs-short-verification",
        source_url="https://xhslink.com/a/stable-code",
        platform=Platform.XIAOHONGSHU,
        source_kind=SourceKind.SHORT_LINK,
        resolved_source_kind=SourceKind.ITEM,
        resolved_source_id=note_id,
        output_root=str(tmp_path),
        status=JobStatus.NEEDS_AUTH,
        verification_url=note_url,
        cookie_profile="Profile 1",
        items=[
            DownloadItem(
                id="blocked-note",
                media_id=note_id,
                source_url=note_url,
                status=ItemStatus.NEEDS_AUTH,
                metadata={
                    "xiaohongshu_resolved_source_kind": SourceKind.ITEM.value,
                    "xiaohongshu_resolved_source_url": note_url,
                },
            )
        ],
    )
    opened: list[tuple[str, str | None]] = []
    monkeypatch.setattr(main_module.manager, "get_job", lambda job_id: job)
    monkeypatch.setattr(
        main_module,
        "_open_chrome",
        lambda url, profile: opened.append((url, profile)),
    )

    response = main_module.open_verification(job.id)

    assert opened == [(note_url, "Profile 1")]
    assert response == {"status": "opened", "url": note_url}


@pytest.mark.parametrize(
    "tamper",
    [
        "untrusted-verification-url",
        "wrong-note-id",
        "wrong-profile-membership",
        "multiple-needs-auth-items",
    ],
)
def test_xiaohongshu_profile_verification_falls_back_for_unbound_state(
    monkeypatch, tmp_path, tamper: str
) -> None:
    profile_id = "62f8ad0b000000001e01f1b9"
    note_id = "693e3a810000000019025182"
    source_url = f"https://www.xiaohongshu.com/user/profile/{profile_id}"
    note_url = f"https://www.xiaohongshu.com/explore/{note_id}"
    item = DownloadItem(
        id="blocked-note",
        media_id=note_id,
        source_url=note_url,
        status=ItemStatus.NEEDS_AUTH,
        metadata={
            "xiaohongshu_profile_id": profile_id,
            "profile_note_membership_verified": True,
        },
    )
    verification_url = note_url
    items = [item]
    if tamper == "untrusted-verification-url":
        verification_url = "https://evil.example/phish"
    elif tamper == "wrong-note-id":
        verification_url = "https://www.xiaohongshu.com/explore/aaaaaaaaaaaaaaaaaaaaaaaa"
    elif tamper == "wrong-profile-membership":
        item.metadata["xiaohongshu_profile_id"] = "different-profile"
    elif tamper == "multiple-needs-auth-items":
        items.append(
            DownloadItem(
                id="other-blocked-note",
                media_id="bbbbbbbbbbbbbbbbbbbbbbbb",
                source_url="https://www.xiaohongshu.com/explore/bbbbbbbbbbbbbbbbbbbbbbbb",
                status=ItemStatus.NEEDS_AUTH,
            )
        )
    job = DownloadJob(
        id="tampered-xhs-profile-verification",
        source_url=source_url,
        platform=Platform.XIAOHONGSHU,
        source_kind=SourceKind.PROFILE,
        output_root=str(tmp_path),
        status=JobStatus.NEEDS_AUTH,
        verification_url=verification_url,
        cookie_profile="Profile 1",
        items=items,
    )
    opened: list[tuple[str, str | None]] = []
    monkeypatch.setattr(main_module.manager, "get_job", lambda job_id: job)
    monkeypatch.setattr(
        main_module,
        "_open_chrome",
        lambda url, profile: opened.append((url, profile)),
    )

    response = main_module.open_verification(job.id)

    assert opened == [(source_url, "Profile 1")]
    assert response == {"status": "opened", "url": source_url}


def test_xiaohongshu_verification_rejects_tampered_insecure_original_source(
    monkeypatch, tmp_path
) -> None:
    source_url = "http://www.xiaohongshu.com/user/profile/example"
    job = DownloadJob(
        id="insecure-xhs-source",
        source_url=source_url,
        platform=Platform.XIAOHONGSHU,
        source_kind=SourceKind.PROFILE,
        output_root=str(tmp_path),
        status=JobStatus.NEEDS_AUTH,
    )
    monkeypatch.setattr(main_module.manager, "get_job", lambda job_id: job)
    monkeypatch.setattr(
        main_module,
        "_open_chrome",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Chrome must not open an insecure persisted source")
        ),
    )

    with pytest.raises(HTTPException) as error:
        main_module.open_verification(job.id)

    assert error.value.status_code == 422
    assert "no longer trusted" in str(error.value.detail)


def test_public_job_hides_internal_douyin_media_identity(tmp_path) -> None:
    job = DownloadJob(
        id="public-job",
        source_url="https://www.douyin.com/video/7664225419386607205",
        platform=Platform.DOUYIN,
        source_kind=SourceKind.ITEM,
        output_root=str(tmp_path),
        items=[
            DownloadItem(
                id="target",
                media_id="7664225419386607205",
                source_url="https://www.douyin.com/video/7664225419386607205",
                metadata={
                    "douyin_item_media": {
                        "media_id": "7664225419386607205",
                        "media_kind": "video",
                        "title": "Public title",
                        "video_uri": "internal-media-credential",
                        "direct_candidates": [
                            {
                                "width": 1440,
                                "height": 2560,
                                "urls": ["https://v26-web.douyinvod.com/secret"],
                            }
                        ],
                    },
                    "douyin_profile_media": {
                        "media_id": "7664225419386607205",
                        "media_kind": "image",
                        "title": "Public image title",
                        "image_assets": [
                            {
                                "index": 1,
                                "candidates": [
                                    "https://p3-sign.douyinpic.com/private-image"
                                ],
                            }
                        ],
                        "live_photo_assets": [
                            {
                                "index": 1,
                                "candidates": [
                                    "https://v26-web.douyinvod.com/private-live-photo"
                                ],
                            }
                        ],
                    },
                },
            )
        ],
    )

    public = main_module._public_job(job)

    assert public.items[0].metadata["douyin_item_media"] == {
        "media_id": "7664225419386607205",
        "media_kind": "video",
        "title": "Public title",
    }
    assert public.items[0].metadata["douyin_profile_media"] == {
        "media_id": "7664225419386607205",
        "media_kind": "image",
        "title": "Public image title",
    }
    assert job.items[0].metadata["douyin_item_media"]["video_uri"] == (
        "internal-media-credential"
    )
    assert "image_assets" in job.items[0].metadata["douyin_profile_media"]
    assert "live_photo_assets" in job.items[0].metadata["douyin_profile_media"]


def test_public_xiaohongshu_urls_redact_xsec_token_but_keep_internal_url(
    monkeypatch,
    tmp_path,
) -> None:
    secret = "TOP_SECRET_XSEC_TOKEN"
    source_url = (
        "https://www.xiaohongshu.com/explore/6411cf99000000001300b6d9"
        f"?xsec_token={secret}&xsec_source=pc_user"
    )
    job = DownloadJob(
        id="public-xhs-token",
        source_url=source_url,
        platform=Platform.XIAOHONGSHU,
        source_kind=SourceKind.ITEM,
        output_root=str(tmp_path),
        status=JobStatus.NEEDS_AUTH,
        verification_url=source_url,
        cookie_profile="Default",
        items=[
            DownloadItem(
                id="xhs-item",
                source_url=source_url,
                metadata={"nested_url": source_url},
            )
        ],
    )

    public = main_module._public_job(job)
    public_json = public.model_dump_json()

    assert secret not in public_json
    assert "xsec_token" not in public.source_url
    assert "xsec_source=pc_user" in public.source_url
    assert secret in job.source_url
    assert secret in job.items[0].source_url

    opened = []
    monkeypatch.setattr(main_module.manager, "get_job", lambda job_id: job)
    monkeypatch.setattr(
        main_module,
        "_open_chrome",
        lambda url, profile: opened.append((url, profile)),
    )

    response = main_module.open_verification(job.id)

    assert opened == [(source_url, "Default")]
    assert secret not in response["url"]
    assert "xsec_token" not in response["url"]


def test_jobs_endpoint_never_serializes_douyin_direct_candidate_urls(
    monkeypatch, tmp_path
) -> None:
    secret_url = "https://v26-web.douyinvod.com/private-signed-stream"
    secret_image_url = "https://p3-sign.douyinpic.com/private-signed-image"
    secret_live_url = "https://v26-web.douyinvod.com/private-signed-live-photo"
    job = DownloadJob(
        id="public-list-job",
        source_url="https://www.douyin.com/video/7664225419386607205",
        platform=Platform.DOUYIN,
        source_kind=SourceKind.ITEM,
        output_root=str(tmp_path),
        items=[
            DownloadItem(
                id="target",
                media_id="7664225419386607205",
                source_url="https://www.douyin.com/video/7664225419386607205",
                metadata={
                    "douyin_item_media": {
                        "media_id": "7664225419386607205",
                        "direct_candidates": [
                            {"width": 1440, "height": 2560, "urls": [secret_url]}
                        ],
                        "image_assets": [
                            {"index": 1, "candidates": [secret_image_url]}
                        ],
                        "live_photo_assets": [
                            {"index": 1, "candidates": [secret_live_url]}
                        ],
                    }
                },
            )
        ],
    )
    monkeypatch.setattr(main_module.manager, "list_jobs", lambda: [job])
    client = TestClient(app)
    try:
        response = client.get("/api/jobs", headers={"host": "127.0.0.1:8787"})
    finally:
        client.close()

    assert response.status_code == 200
    assert secret_url not in response.text
    assert secret_image_url not in response.text
    assert secret_live_url not in response.text
    assert "direct_candidates" not in response.text
    assert "image_assets" not in response.text
    assert "live_photo_assets" not in response.text


@pytest.mark.parametrize("malformed", [[], "signed-url", 123])
def test_public_job_removes_malformed_douyin_media_cache(malformed, tmp_path) -> None:
    job = DownloadJob(
        id="malformed-public-cache",
        source_url="https://www.douyin.com/video/7664225419386607205",
        platform=Platform.DOUYIN,
        source_kind=SourceKind.ITEM,
        output_root=str(tmp_path),
        items=[
                DownloadItem(
                    id="target",
                    source_url=(
                        "https://www.douyin.com/video/7664225419386607205"
                    ),
                    metadata={
                    "douyin_item_media": malformed,
                    "douyin_profile_media": malformed,
                },
            )
        ],
    )

    public = main_module._public_job(job)

    assert "douyin_item_media" not in public.items[0].metadata
    assert "douyin_profile_media" not in public.items[0].metadata


def test_public_job_includes_discovery_activity_state(tmp_path) -> None:
    started_at = datetime(2026, 9, 7, 2, 0, tzinfo=timezone.utc)
    job = DownloadJob(
        id="activity-publication",
        source_url="https://www.douyin.com/user/example",
        platform=Platform.DOUYIN,
        source_kind=SourceKind.PROFILE,
        output_root=str(tmp_path),
        status=JobStatus.DISCOVERING,
        activity_message="Fetching Douyin signed profile page 3/300",
        activity_started_at=started_at,
    )

    public = main_module._public_job(job)

    assert public.activity_message == "Fetching Douyin signed profile page 3/300"
    assert public.activity_started_at == started_at
    assert public.warning is None
