from __future__ import annotations

import os
import threading

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.build_info import APP_ID, APP_VERSION, BUILD_ID, calculate_build_id
from app.main import app
from app.models import DownloadItem, DownloadJob, Platform, SourceKind
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
    ):
        assert classification_marker in static_response.text
    assert (
        "discoveryFailureMessage || localizeRuntimeMessage(job?.warning, job)"
        in static_response.text
    )
    assert "抖音暂时无法创建经过验证的请求。请在 Chrome" not in static_response.text


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
        verification_url="https://www.douyin.com/user/wrong-profile",
    )
    monkeypatch.setattr(main_module.manager, "get_job", lambda job_id: job)
    monkeypatch.setattr(main_module, "_open_chrome", opened.append)

    response = main_module.open_verification(job.id)

    assert response == {"status": "opened", "url": source_url}
    assert opened == [source_url]


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
        verification_url="https://evil.example/phish",
    )
    monkeypatch.setattr(main_module.manager, "get_job", lambda job_id: job)
    monkeypatch.setattr(main_module, "_open_chrome", opened.append)

    response = main_module.open_verification(job.id)

    assert response == {"status": "opened", "url": source_url}
    assert opened == [source_url]


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
        verification_url=source_url,
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
    monkeypatch.setattr(main_module, "_open_chrome", opened.append)

    response = main_module.open_verification(job.id)

    assert opened == [source_url]
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
