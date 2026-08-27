from __future__ import annotations

from fastapi.testclient import TestClient

from app import main as main_module
from app.build_info import APP_ID, APP_VERSION, BUILD_ID, calculate_build_id
from app.main import app
from app.models import DownloadItem, DownloadJob, Platform, SourceKind


def test_health_endpoint_accepts_local_host_and_rejects_dns_rebinding() -> None:
    client = TestClient(app, base_url="http://localhost")
    try:
        assert client.get("/api/health").json() == {
            "status": "ok",
            "app_id": APP_ID,
            "version": APP_VERSION,
            "build_id": BUILD_ID,
            "source_build_id": calculate_build_id(),
            "restart_required": False,
        }
        assert (
            client.get("/api/health", headers={"host": "attacker.example"}).status_code
            == 400
        )
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
                        "video_uri": "internal-media-credential",
                        "direct_candidates": [
                            {
                                "width": 1440,
                                "height": 2560,
                                "urls": ["https://v26-web.douyinvod.com/secret"],
                            }
                        ],
                    }
                },
            )
        ],
    )

    public = main_module._public_job(job)

    assert public.items[0].metadata["douyin_item_media"] == {
        "media_id": "7664225419386607205"
    }
    assert job.items[0].metadata["douyin_item_media"]["video_uri"] == (
        "internal-media-credential"
    )


def test_jobs_endpoint_never_serializes_douyin_direct_candidate_urls(
    monkeypatch, tmp_path
) -> None:
    secret_url = "https://v26-web.douyinvod.com/private-signed-stream"
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
    assert "direct_candidates" not in response.text
