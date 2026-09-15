from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.errors import SiteIssueCode


PROJECT_ROOT = Path(__file__).resolve().parent.parent
NODE_EXECUTION_TIMEOUT_SECONDS = 30
ISSUE_SOLUTION_MARKERS = {
    "rate_limited": "等待 1–2 分钟",
    "verification_required": "打开 Chrome 验证",
    "login_required": "Chrome Profile 登录",
    "request_rejected": "检查代理或 VPN",
    "site_processing": "网站生成原文件",
    "content_unavailable": "本工具无法下载",
    "region_restricted": "本工具不会绕过地区限制",
    "site_response_changed": "从原链接重新解析",
    "media_link_expired": "刷新当前作品的媒体地址",
    "site_unavailable": "等待服务恢复",
    "network_error": "检查网络、DNS",
    "cookie_unavailable": "完全退出 Chrome",
    "security_blocked": "不要手动放行未知地址",
    "local_configuration": "brew install ffmpeg",
    "unknown": "版本号和 build ID",
}


def _run_node_script(
    node: str,
    script: str,
    tmp_path: Path,
    filename: str,
) -> subprocess.CompletedProcess[str]:
    script_path = tmp_path / filename
    script_path.write_text(script, encoding="utf-8")
    return subprocess.run(
        [node, str(script_path)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=NODE_EXECUTION_TIMEOUT_SECONDS,
    )


@pytest.mark.parametrize(
    "prefix",
    [
        "Douyin signed data failed identity or integrity validation.",
        "Douyin author-feed data failed identity or integrity validation.",
        "Douyin signed discovery failed before a verified response was available.",
        "Douyin automatic item refresh did not pass identity or integrity validation. "
        "The task was paused without downloading a fallback.",
        "Douyin automatic media refresh did not pass identity or integrity validation. "
        "The task was paused without downloading a fallback.",
    ],
)
def test_signing_validation_displays_only_allowlisted_diagnostics(tmp_path, prefix):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    source = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    source = source.replace(
        "  initialize();\n})();\n",
        "  window.__localizeRuntimeMessage = localizeRuntimeMessage;\n})();\n",
    )
    cases = {
        "ssr-redirect": "原作品页面返回了跳转指令",
        "ssr-unknown-item": "原作品页面没有提供可确认的目标作品身份",
        "ssr-item-mismatch": "原作品页面返回的作品 ID 与目标作品不一致",
        "ssr-author-mismatch": "原作品页面返回的作者身份与任务绑定的作者不一致",
        "ssr-conflicting-items": "相互冲突的作品信息",
        "detail-item-mismatch": "详情接口返回的作品 ID 与目标作品不一致",
        "detail-author-mismatch": "详情接口返回的作者身份与任务绑定的作者不一致",
        "detail-response-redirect": "详情接口的响应地址与已验证的请求目标不一致",
        "detail-metadata-incomplete": "详情接口缺少完成作品校验所需的媒体信息",
        "ssr-metadata-incomplete": "原作品页面缺少完成作品校验所需的媒体信息",
        "signer-html-invalid": "签名初始化页面未通过格式或完整性校验",
        "signer-script-invalid": "签名初始化脚本未通过来源或完整性校验",
        "signing-validation-failed": "现有诊断不足以确定更具体的原因",
        "signing-runtime-error": "签名组件运行异常",
    }
    messages = [f"{prefix} Diagnostic code: {code}." for code in cases]
    completed = _run_node_script(
        node,
        "globalThis.window = {};\n"
        "globalThis.document = {querySelector: () => null};\n"
        + source
        + f"\nconst inputs = {json.dumps(messages)};\n"
        + "process.stdout.write(JSON.stringify(inputs.map((value) => "
        "window.__localizeRuntimeMessage(value))));\n",
        tmp_path,
        "signing-validation-diagnostics.js",
    )
    assert completed.returncode == 0, completed.stderr
    localized = json.loads(completed.stdout)
    for (code, description), message in zip(cases.items(), localized, strict=True):
        assert description in message
        assert f"诊断码：{code}。" in message
        assert "版本号和 build ID" in message
        assert "不需要打开 Chrome 验证" in message
        assert "等待一两分钟" not in message
        assert "Diagnostic code:" not in message
        if code in {
            "ssr-unknown-item",
            "ssr-item-mismatch",
            "ssr-author-mismatch",
            "ssr-conflicting-items",
            "detail-item-mismatch",
            "detail-author-mismatch",
        }:
            assert "新建任务尝试一次" in message
            assert "停止反复重试" in message
        if code == "ssr-redirect":
            assert "不等于已确认跳到了其他视频" in message
            assert "更新到最新版后从原链接重新解析一次" in message
        if code == "signing-validation-failed":
            assert "当前作品未能完成校验" in message
            assert "签名响应未通过校验" not in message
            assert "不要反复更新或重试" in message


@pytest.mark.parametrize(
    "prefix",
    [
        "Douyin signed data failed identity or integrity validation.",
        "Douyin author-feed data failed identity or integrity validation.",
        "Douyin signed discovery failed before a verified response was available.",
        "Douyin automatic item refresh did not pass identity or integrity validation. "
        "The task was paused without downloading a fallback.",
        "Douyin automatic media refresh did not pass identity or integrity validation. "
        "The task was paused without downloading a fallback.",
    ],
)
def test_signing_validation_hides_missing_malformed_and_injected_details(
    tmp_path, prefix
):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    source = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    source = source.replace(
        "  initialize();\n})();\n",
        "  window.__localizeRuntimeMessage = localizeRuntimeMessage;\n})();\n",
    )
    secret = "https://sensitive.example/media?signature=PRIVATE_TOKEN"
    invalid_suffixes = [
        "",
        " Diagnostic code: unknown-private-code.",
        " Diagnostic code: constructor.",
        " Diagnostic code: __proto__.",
        " Diagnostic code: ssr-redirect",
        " Diagnostic code: SSR-REDIRECT.",
        " Diagnostic code: ssr-redirect.extra.",
        " Diagnostic code: ssr-redirect. trailing-private-data",
        " Diagnostic code: ssr-redirect.\n",
        " Diagnostic code: ssr-redirect.\r\n",
        " Diagnostic code: ssr-redirect.\ntrailing-private-data",
        " Diagnostic code: ssr-redirect. Diagnostic code: detail-item-mismatch.",
        " Diagnostic code: ssr-redirect\u0000.",
        f" Diagnostic code: {secret}.",
        f" Diagnostic code: ssr-redirect.{secret}",
    ]
    messages = [prefix + suffix for suffix in invalid_suffixes]
    messages += [
        f"{prefix} {secret}",
        f"{prefix} media endpoint redirected to an unrecognized Douyin CDN host {secret}",
        "Douyin author-feed data failed identity or integrity validation.",
        "Douyin signed discovery failed before a verified response was available.",
    ]
    messages.append(f"{prefix} {secret} Diagnostic code: ssr-redirect.")
    completed = _run_node_script(
        node,
        "globalThis.window = {};\n"
        "globalThis.document = {querySelector: () => null};\n"
        + source
        + f"\nconst inputs = {json.dumps(messages)};\n"
        + "process.stdout.write(JSON.stringify(inputs.map((value) => "
        "window.__localizeRuntimeMessage(value))));\n",
        tmp_path,
        "signing-validation-unsafe-diagnostics.js",
    )
    assert completed.returncode == 0, completed.stderr
    localized = json.loads(completed.stdout)
    for message in localized[:-1]:
        assert message == localized[0]
        assert "本次记录没有可识别的诊断码" in message
        assert "无法据此确定失败原因" in message
        assert "旧版本" not in message
        assert "请更新" not in message
        assert "不要反复更新或重试" in message
        assert "版本号和 build ID" in message
        assert "诊断码：" not in message
    assert "诊断码：ssr-redirect。" in localized[-1]
    for message in localized:
        assert "PRIVATE_TOKEN" not in message
        assert "sensitive.example" not in message
        assert "signature=" not in message
        assert "unknown-private-code" not in message
        assert "trailing-private-data" not in message
        assert "不需要打开 Chrome 验证" in message


@pytest.mark.parametrize(
    "prefix",
    [
        "Douyin media was discovered, but its highest quality could not be verified.",
        "Douyin Live Photo author-feed quality source could not be verified.",
    ],
)
def test_duration_mismatch_shows_measured_values_and_retry_advice(tmp_path, prefix):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    source = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    source = source.replace(
        "  initialize();\n})();\n",
        "  window.__localizeRuntimeMessage = localizeRuntimeMessage;\n})();\n",
    )
    message = (
        f"{prefix} Probe details: default: "
        "media duration did not match the requested Douyin item "
        "(expected 12.000s, measured 1.000s, tolerance 0.500s)"
    )
    completed = _run_node_script(
        node,
        "globalThis.window = {};\n"
        "globalThis.document = {querySelector: () => null};\n"
        + source
        + f"\nprocess.stdout.write(window.__localizeRuntimeMessage({json.dumps(message)}));\n",
        tmp_path,
        "duration-mismatch.js",
    )
    assert completed.returncode == 0, completed.stderr
    assert "作品声明时长 12.000 秒，完整媒体实测 1.000 秒" in completed.stdout
    assert "允许差值 0.500 秒" in completed.stdout
    assert "重试" in completed.stdout


def test_douyin_redirect_messages_execute_with_safe_legacy_and_reason_parsing(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    source = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    tail = "  initialize();\n})();\n"
    assert tail in source
    source = source.replace(
        tail,
        "  window.__localizeRuntimeMessage = localizeRuntimeMessage;\n})();\n",
    )
    cases = [
        (
            "media endpoint redirected to an unrecognized Douyin CDN host "
            "(host: invalid-host)",
            "invalid-host”不是实际域名",
        ),
        (
            "Douyin media redirect could not be trusted. Redirect host: "
            "invalid-host",
            "invalid-host”不是实际域名",
        ),
        (
            "Douyin media redirect could not be trusted. Redirect host: "
            "unavailable; Redirect host fingerprint: 0123456789ab; "
            "Redirect reason: unrecognized-host. Probe details: default: "
            "media endpoint redirected to an unrecognized Douyin CDN host",
            "已自动尝试四条官方同画质路由",
        ),
        (
            "Douyin media redirect could not be trusted. Redirect host: "
            "secret-token.com; Redirect reason: unrecognized-host",
            "新版再次拦截时会显示可反馈的校验指纹",
        ),
        (
            "media endpoint redirected to an unrecognized Douyin CDN host "
            "(host: media.vendor-cdn.net; reason: non-https-scheme)",
            "媒体地址被降级为非 HTTPS",
        ),
        (
            "Douyin media redirect could not be trusted. Redirect host: "
            "secret-token.edge.pstatp.com; Redirect reason: "
            "unverified-source-binding",
            "缺少把该地址绑定到这条作品最高画质所需的完整校验指纹",
        ),
        (
            "media endpoint redirected to an unrecognized Douyin CDN host "
            "(host: unavailable; reason: local-or-special-use-host)",
            "跳转目标是本地、内网或保留用途域名",
        ),
        (
            "media endpoint redirected to an unrecognized Douyin CDN host "
            "(host: unavailable; reason: too-many-redirects)",
            "媒体地址的连续跳转次数超过安全上限",
        ),
        (
            "media endpoint redirected to an unrecognized Douyin CDN host "
            "(host: legacy-cdn.vendor-cdn.net)",
            "主机名可能包含敏感标识",
        ),
        (
            "Douyin media redirect could not be trusted. Redirect host: "
            "pstatp.com; Redirect host fingerprint: unavailable; Redirect port: "
            "8443; Redirect reason: nonstandard-port",
            "CDN 域名族：pstatp.com，端口：8443",
        ),
        (
            "Probe details: default: media endpoint redirected to an "
            "unrecognized Douyin CDN host (host: unavailable; "
            "host-fingerprint: 38c1b2b0b3d0; port: 33443; reason: "
            "nonstandard-port)",
            "当前版本会在任务执行时从原任务链接自动刷新当前作品一次",
        ),
        (
            "Refreshing this Douyin item from the original task link after a "
            "blocked media route",
            "只刷新当前作品并自动重试",
        ),
        (
            "Douyin automatic item refresh was skipped because Chrome Cookie is "
            "disabled for this task",
            "程序遵守该任务设置，没有读取 Chrome Cookie",
        ),
        (
            "Douyin automatic item refresh returned media below the previously "
            "verified quality floor",
            "分辨率、编码或码率低于任务此前已验证的质量档",
        ),
        (
            "media endpoint redirected to an unrecognized Douyin CDN host "
            "(host: unavailable; host-fingerprint: unavailable; reason: "
            "nonstandard-port)",
            "端口：旧记录未保存",
        ),
        (
            "This partially downloaded Douyin profile entry was not returned by "
            "a complete verified profile refresh. It is no longer available for "
            "automatic retry; existing files were preserved.",
            "余下可见作品会继续下载",
        ),
        (
            "Douyin profile retry returned only a partial author feed. Previously "
            "queued media entries were not reused; retry after a short wait before "
            "downloading any item.",
            "没有下载低清文件",
        ),
        (
            "No current authenticated Xiaohongshu session was found in the "
            "selected Chrome profile. This is a Chrome login/profile setting "
            "issue; CAPTCHA verification is not required.",
            "不是验证码",
        ),
        (
            "Xiaohongshu did not accept the selected Chrome profile's login "
            "session. This is a login-session issue; a CAPTCHA is not required "
            "unless Chrome actually displays one.",
            "同一个 Profile",
        ),
        (
            "Xiaohongshu displayed an explicit verification challenge.",
            "已明确显示验证码",
        ),
        (
            "The Xiaohongshu task has an unsupported cookie-browser setting.",
            "浏览器 Cookie 设置无效",
        ),
        (
            "Xiaohongshu identified this work as a video but returned no trusted "
            "video stream. The cover image was not downloaded as a substitute.",
            "没有把封面图片冒充视频保存",
        ),
        (
            "This Xiaohongshu profile item was completed by an older version "
            "without verified media-type metadata. Existing files were preserved.",
            "重新识别它是图片还是视频",
        ),
        (
            "Douyin signed discovery stopped after 120 seconds without verified "
            "progress. Retry after a short wait; Chrome verification is not "
            "required. Reason category: no-progress-timeout.",
            "120 秒无有效进展后自动停止",
        ),
        (
            "Douyin temporarily limited a signed request after automatic retries. "
            "Wait a minute or two and retry. Reason category: http-429.",
            "HTTP 429 限流",
        ),
        (
            "Douyin temporarily limited a signed request after automatic retries. "
            "Wait a minute or two and retry. Reason category: "
            "api-unbound-empty-page.",
            "空终页没有绑定当前作者",
        ),
        (
            "Douyin returned incomplete or changed signed response data after "
            "automatic retries. The response could not be verified, so the task "
            "was paused without downloading a fallback. Reason category: "
            "api-filtered-images-base.",
            "没有把背景音频误当成视频",
        ),
        (
            "Douyin quality verification made no media progress for 120 seconds. "
            "The task was paused; completed files were preserved.",
            "连续 120 秒没有收到新的媒体字节或有效探测结果后自动停止",
        ),
        (
            "Douyin media transfer made no media progress for 120 seconds. "
            "The task was paused; completed files were preserved.",
            "连续 120 秒没有收到任何新字节后自动停止",
        ),
        (
            "Some Douyin image positions reported Live Photo data, but an older "
            "version could not inspect the motion file.",
            "部分抖音图片位带有 Live Photo 数据",
        ),
        (
            "Chrome cookies could not be read, so anonymous access was used. "
            "The profile may be incomplete and restricted high-quality formats "
            "may be missing. Some Douyin image positions reported Live Photo "
            "data, but no complete trusted motion rendition was available. The "
            "highest-pixel static images will be saved for those positions. "
            "Create a new task from the original link later to retry the dynamic "
            "versions.",
            "无法读取 Chrome Cookie",
        ),
        (
            "A local DNS or web filter blocked Douyin before the site loaded. "
            "Allow Douyin in the local filter, disable DNS filtering, or switch "
            "networks, then retry the original link. Chrome verification is not "
            "required.",
            "在过滤器中放行抖音",
        ),
        (
            "Could not save settings. Check free disk space and write permissions "
            "for the project's data folder, then save again. The previous settings "
            "are still in use.",
            "程序仍在使用之前的设置。请检查磁盘剩余空间",
        ),
        (
            "Douyin media was discovered, but its highest quality could not be "
            "verified. Probe details: author-feed-3: verified media was below "
            "the author-feed 1080x1920 rendition (measured 720x1280)",
            "该地址声明 1080×1920，实际文件只有 720×1280",
        ),
        (
            "Douyin Live Photo author-feed quality source could not be verified. "
            "Probe details: author-feed-2: verified media was below the "
            "author-feed 1440x2560 rendition (measured 1080x1920)",
            "该地址声明 1440×2560，实际文件只有 1080×1920",
        ),
    ]
    harness = (
        "globalThis.window = {};\n"
        "globalThis.document = {querySelector: () => null};\n"
        f"const __cases = {json.dumps([value for value, _ in cases])};\n"
    )
    trailer = (
        "\nprocess.stdout.write(JSON.stringify("
        "__cases.map(value => window.__localizeRuntimeMessage(value))));\n"
    )

    completed = _run_node_script(
        node,
        harness + source + trailer,
        tmp_path,
        "douyin-redirect-messages.js",
    )

    assert completed.returncode == 0, completed.stderr
    messages = json.loads(completed.stdout)
    assert len(messages) == len(cases)
    for message, (_, expected) in zip(messages, cases):
        assert expected in message
        assert "must-not-persist" not in message
        assert "private.mp4" not in message
    for legacy_message in messages[:2]:
        assert "把这个主机名发给开发者" not in legacy_message
    assert "校验指纹：0123456789ab" in messages[2]
    assert "把这个校验指纹发给开发者" in messages[2]
    assert "secret-token.com" not in messages[3]
    assert "media.vendor-cdn.net" not in messages[4]
    known_unbound_message = messages[5]
    assert "CDN 域名族：pstatp.com" in known_unbound_message
    assert "secret-token" not in known_unbound_message
    assert "尚未识别" not in known_unbound_message
    assert "检查代理" not in known_unbound_message
    assert "legacy-cdn.vendor-cdn.net" not in messages[8]
    assert "把这个主机名发给开发者" not in messages[8]
    assert "无法判断" in messages[9]
    assert "代理或 VPN" in messages[9]
    assert "不需要打开 Chrome 验证" in messages[9]
    assert "稍后从原链接新建任务" in messages[-6]
    assert "明确无水印的动态图版本" in messages[-5]
    assert "Some Douyin image positions" not in messages[-5]
    assert "关闭 DNS 过滤、切换网络" in messages[-4]
    assert "不需要打开 Chrome 验证" in messages[-4]
    assert "不需要打开 Chrome 验证" in messages[-2]
    assert "检查备用源" in messages[-2]


def test_interrupted_job_labels_queued_items_as_waiting_to_continue(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    source = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    tail = "  initialize();\n})();\n"
    assert tail in source
    source = source.replace(
        tail,
        "  window.__activeCountLabel = activeCountLabel;\n"
        "  window.__getCounts = getCounts;\n"
        "  window.__progressDescription = progressDescription;\n})();\n",
    )
    job = {
        "id": "paused-douyin-profile",
        "status": "interrupted",
        "platform": "douyin",
        "source_kind": "profile",
        "author": "Verified author",
        "total_items": 152,
        "completed_items": 0,
        "failed_items": 1,
        "discovery_complete": False,
        "items": [
            {"id": "failed", "status": "failed", "retryable": True},
            *[{"id": f"queued-{index}", "status": "queued"} for index in range(151)],
        ],
    }
    harness = (
        "globalThis.window = {};\n"
        "globalThis.document = {querySelector: () => null};\n"
        f"const __job = {json.dumps(job)};\n"
    )
    trailer = (
        "\nconst __counts = window.__getCounts(__job);\n"
        "process.stdout.write(JSON.stringify({"
        "label: window.__activeCountLabel(__job, __counts.active), "
        "count: String(__counts.active), "
        "progress: window.__progressDescription(__job, __counts)}));\n"
    )

    completed = _run_node_script(
        node,
        harness + source + trailer,
        tmp_path,
        "interrupted-job-labels.js",
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "label": "等待继续",
        "count": "151",
        "progress": "已暂停，等待继续；已处理 1 / 152 个作品",
    }


def test_douyin_live_photo_outputs_are_labeled_by_saved_media(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    source = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    tail = "  initialize();\n})();\n"
    assert tail in source
    source = source.replace(
        tail,
        "  window.__itemType = itemType;\n})();\n",
    )
    items = [
        {
            "media_type": "video",
            "selected_format": "douyin-highest-live-photos-or-images",
            "output_paths": ["/tmp/2026-09-09-Live [1]-001.mp4"],
        },
        {
            "media_type": "image",
            "selected_format": "douyin-highest-live-photos-or-images",
            "output_paths": [
                "/tmp/2026-09-09-Live [1]-001.mp4",
                "/tmp/2026-09-09-Live [1]-002.webp",
            ],
        },
        {
            "media_type": "image",
            "selected_format": "douyin-highest-images",
            "output_paths": ["/tmp/2026-09-09-Static [1]-001.webp"],
        },
    ]
    harness = (
        "globalThis.window = {};\n"
        "globalThis.document = {querySelector: () => null};\n"
        f"const __items = {json.dumps(items)};\n"
    )
    trailer = (
        "\nprocess.stdout.write(JSON.stringify("
        "__items.map(value => window.__itemType(value))));\n"
    )

    completed = _run_node_script(
        node,
        harness + source + trailer,
        tmp_path,
        "douyin-live-photo-item-types.js",
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == ["动态图", "动态图 + 图片", "图片"]


def test_xiaohongshu_profile_auth_labels_the_blocked_note_as_verification_target(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    source = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    tail = "  initialize();\n})();\n"
    assert tail in source
    source = source.replace(
        tail,
        "  window.__verificationTarget = verificationTarget;\n"
        "  window.__isXiaohongshuVerificationItem = "
        "isXiaohongshuVerificationItem;\n})();\n",
    )
    jobs = [
        {
            "platform": "xiaohongshu",
            "source_kind": "profile",
            "source_url": "https://www.xiaohongshu.com/user/profile/example",
            "verification_url": (
                "https://www.xiaohongshu.com/explore/"
                "693e3a810000000019025182?xsec_source=pc_user"
            ),
        },
        {
            "platform": "xiaohongshu",
            "source_kind": "profile",
            "source_url": "https://www.xiaohongshu.com/user/profile/example",
            "verification_url": "https://www.xiaohongshu.com/user/profile/example",
        },
        {
            "platform": "douyin",
            "source_kind": "profile",
            "source_url": "https://www.douyin.com/user/example",
            "verification_url": "https://www.douyin.com/user/example",
        },
    ]
    harness = (
        "globalThis.window = {};\n"
        "globalThis.document = {querySelector: () => null};\n"
        f"const __jobs = {json.dumps(jobs)};\n"
    )
    trailer = (
        "\nprocess.stdout.write(JSON.stringify(__jobs.map(job => ({"
        "item: window.__isXiaohongshuVerificationItem(job), "
        "target: window.__verificationTarget(job)}))));\n"
    )

    completed = _run_node_script(
        node,
        harness + source + trailer,
        tmp_path,
        "xiaohongshu-verification-target.js",
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == [
        {"item": True, "target": "触发验证的作品"},
        {"item": False, "target": "原主页"},
        {"item": False, "target": "原主页"},
    ]


def test_nonretryable_failed_item_keeps_visible_error_without_retrying(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    source = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    tail = "  initialize();\n})();\n"
    assert tail in source
    assert 'if (state.filter === "failed") return isFailed(item);' in source
    source = source.replace(
        tail,
        "  window.__itemError = itemError;\n"
        "  window.__isRetryableItem = isRetryableItem;\n})();\n",
    )
    item = {
        "status": "failed",
        "retryable": False,
        "error": (
            "This partially downloaded Douyin profile entry was not returned by "
            "a complete verified profile refresh."
        ),
    }
    harness = (
        "globalThis.window = {};\n"
        "globalThis.document = {querySelector: () => null};\n"
        f"const __item = {json.dumps(item)};\n"
    )
    trailer = (
        "\nprocess.stdout.write(JSON.stringify({"
        "error: window.__itemError(__item, {}), "
        "retryable: window.__isRetryableItem(__item)}));\n"
    )

    completed = _run_node_script(
        node,
        harness + source + trailer,
        tmp_path,
        "nonretryable-failed-item.js",
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert "余下可见作品会继续下载" in result["error"]
    assert result["retryable"] is False


def test_structured_issue_helpers_drive_titles_messages_and_auth_state(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    source = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    tail = "  initialize();\n})();\n"
    assert tail in source
    assert "if (!isRunning(job) && isRetryableItem(item)" in source
    source = source.replace(
        tail,
        "  window.__issueCode = issueCode;\n"
        "  window.__issueTitleForJob = issueTitleForJob;\n"
        "  window.__localizedIssueMessage = localizedIssueMessage;\n"
        "  window.__localizedPrimaryJobIssueMessage = "
        "localizedPrimaryJobIssueMessage;\n"
        "  window.__issueResolutionText = issueResolutionText;\n"
        "  window.__issueToastMessage = issueToastMessage;\n"
        "  window.__warningToastMessage = warningToastMessage;\n"
        "  window.__warningPresentation = warningPresentation;\n"
        "  window.__authRequired = authRequired;\n"
        "  window.__statusLabel = statusLabel;\n})();\n",
    )
    jobs = {
        "rate": {
            "status": "failed",
            "issue_code": "rate_limited",
            "issue_message": "opaque remote response 7f3a",
            "items": [],
        },
        "login": {
            "status": "needs_auth",
            "issue_code": "login_required",
            "issue_message": "opaque login response",
            "items": [],
        },
        "captcha": {
            "status": "needs_auth",
            "issue_code": "verification_required",
            "issue_message": "opaque challenge response",
            "items": [],
        },
        "staleChild": {
            "status": "failed",
            "issue_code": "rate_limited",
            "issue_message": "opaque rate response",
            "items": [
                {
                    "status": "needs_auth",
                    "issue_code": "verification_required",
                }
            ],
        },
        "cookieDisabled": {
            "status": "failed",
            "issue_message": (
                "Douyin automatic item refresh was skipped because Chrome Cookie "
                "is disabled for this task"
            ),
            "items": [],
        },
        "legacyRiskControl": {
            "status": "failed",
            "issue_message": "网络环境存在风险，请稍后再试",
            "items": [],
        },
        "completedWarning": {
            "status": "completed",
            "warning": (
                "Chrome cookies could not be read, so anonymous access was used"
            ),
            "items": [{"status": "completed"}],
        },
        "completedLivePhotoFallback": {
            "status": "completed",
            "warning": (
                "Some Douyin image positions reported Live Photo data, but no "
                "complete trusted motion rendition was available. The highest-pixel "
                "static images will be saved for those positions. Create a new task "
                "from the original link later to retry the dynamic versions."
            ),
            "items": [{"status": "completed"}],
        },
        "completedCookieFallback": {
            "status": "completed",
            "cookie_fallback_used": True,
            "items": [{"status": "completed"}],
        },
        "incompleteDiscovery": {
            "status": "completed",
            "discovery_complete": False,
            "items": [{"status": "completed"}],
        },
    }
    harness = (
        "globalThis.window = {};\n"
        "globalThis.document = {querySelector: () => null};\n"
        f"const __jobs = {json.dumps(jobs)};\n"
        f"const __issueCodes = {json.dumps([code.value for code in SiteIssueCode])};\n"
        "const __issueJobs = Object.fromEntries(__issueCodes.map((code) => [code, {\n"
        "  status: 'failed',\n"
        "  issue_code: code,\n"
        "  issue_message: `opaque-${code}`,\n"
        "  items: [],\n"
        "}]));\n"
    )
    trailer = """
const output = {
  rate: {
    code: window.__issueCode(__jobs.rate),
    title: window.__issueTitleForJob(__jobs.rate),
    message: window.__localizedPrimaryJobIssueMessage(__jobs.rate),
    auth: window.__authRequired(__jobs.rate),
    status: window.__statusLabel(__jobs.rate),
  },
  login: {
    title: window.__issueTitleForJob(__jobs.login),
    auth: window.__authRequired(__jobs.login),
    status: window.__statusLabel(__jobs.login),
  },
  captcha: {
    title: window.__issueTitleForJob(__jobs.captcha),
    auth: window.__authRequired(__jobs.captcha),
    status: window.__statusLabel(__jobs.captcha),
  },
  staleAuth: window.__authRequired(__jobs.staleChild),
  cookie: window.__localizedIssueMessage(__jobs.cookieDisabled),
  legacyRiskControl: {
    code: window.__issueCode(__jobs.legacyRiskControl),
    title: window.__issueTitleForJob(__jobs.legacyRiskControl),
    auth: window.__authRequired(__jobs.legacyRiskControl),
  },
  toast: window.__issueToastMessage(__jobs.rate, '网站正在限流'),
  completedWarningToast: window.__warningToastMessage(__jobs.completedWarning),
  warningPaths: {
    completedWarning: window.__warningPresentation(__jobs.completedWarning),
    completedLivePhotoFallback: window.__warningPresentation(__jobs.completedLivePhotoFallback),
    completedCookieFallback: window.__warningPresentation(__jobs.completedCookieFallback),
    incompleteDiscovery: window.__warningPresentation(__jobs.incompleteDiscovery),
  },
  solutionCoverage: Object.fromEntries(
    __issueCodes.map((code) => [code, window.__issueResolutionText(code)])
  ),
  messageCoverage: Object.fromEntries(
    __issueCodes.map((code) => [code, {
      primary: window.__localizedPrimaryJobIssueMessage(__issueJobs[code]),
      item: window.__localizedIssueMessage(__issueJobs[code]),
    }])
  ),
};
process.stdout.write(JSON.stringify(output));
"""

    completed = _run_node_script(
        node,
        harness + source + trailer,
        tmp_path,
        "structured-issue-helpers.js",
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["rate"]["code"] == "rate_limited"
    assert result["rate"]["title"] == "网站正在限流"
    assert "网站明确返回了限流信号" in result["rate"]["message"]
    assert "发生了什么：" in result["rate"]["message"]
    assert "解决办法：" in result["rate"]["message"]
    assert "opaque remote response 7f3a" in result["rate"]["message"]
    assert result["rate"]["auth"] is False
    assert result["rate"]["status"] == "被限流"
    assert result["login"] == {
        "title": "需要登录或建立站点会话",
        "auth": True,
        "status": "需登录",
    }
    assert result["captcha"] == {
        "title": "网站要求完成验证码",
        "auth": True,
        "status": "等验证码",
    }
    assert result["staleAuth"] is False
    assert "开启 Chrome Cookie 后，从原链接创建一个新任务" in result["cookie"]
    assert "继续旧任务仍会保持 Cookie 关闭" in result["cookie"]
    assert result["legacyRiskControl"] == {
        "code": "request_rejected",
        "title": "网站拒绝了本次请求",
        "auth": False,
    }
    assert result["toast"].startswith("正在识别作者…：网站正在限流\n发生了什么：")
    assert "\n解决办法：" in result["toast"]
    assert "等待 1–2 分钟" in result["toast"]
    assert result["completedWarningToast"].startswith("正在识别作者…\n发生了什么：")
    assert "\n解决办法：" in result["completedWarningToast"]
    assert "完全退出 Chrome" in result["completedWarningToast"]
    for warning in result["warningPaths"].values():
        assert warning is not None
        assert warning["message"].startswith("发生了什么：")
        assert "\n解决办法：" in warning["message"]
    assert "完全退出 Chrome" in result["warningPaths"]["completedWarning"]["message"]
    assert result["warningPaths"]["completedLivePhotoFallback"] == {
        "title": "部分 Live Photo 已保存为静态图",
        "message": (
            "发生了什么：作品的部分图片位标记了 Live Photo，但程序没有取得完整"
            "可信且明确无水印的动态图，因此没有把低清或来源不明的视频冒充原始"
            "动态图。\n解决办法：这些位置已保存最高像素静态图；若之后仍想取得动态"
            "图，请稍后从原链接新建任务重试。"
        ),
        "isAlert": False,
    }
    assert (
        "完全退出 Chrome"
        in result["warningPaths"]["completedCookieFallback"]["message"]
    )
    assert (
        "从原链接重新解析" in result["warningPaths"]["incompleteDiscovery"]["message"]
    )
    assert set(ISSUE_SOLUTION_MARKERS) == {code.value for code in SiteIssueCode}
    assert set(result["solutionCoverage"]) == set(ISSUE_SOLUTION_MARKERS)
    assert set(result["messageCoverage"]) == set(ISSUE_SOLUTION_MARKERS)
    for code, marker in ISSUE_SOLUTION_MARKERS.items():
        message = result["solutionCoverage"][code]
        assert message.startswith("发生了什么：")
        assert "\n解决办法：" in message
        assert marker in message
        solution = message.split("\n解决办法：", 1)[1].strip()
        assert len(solution) >= 20
        for rendered in result["messageCoverage"][code].values():
            assert rendered.startswith("发生了什么：")
            assert "\n解决办法：" in rendered
            assert marker in rendered
            assert f"opaque-{code}" in rendered
            assert rendered.index("解决办法：") < rendered.index(f"opaque-{code}")


def test_issue_causes_and_solutions_preserve_visible_line_breaks() -> None:
    styles = (PROJECT_ROOT / "app" / "static" / "styles.css").read_text(
        encoding="utf-8"
    )

    assert ".auth-copy p" in styles
    assert ".item-error" in styles
    assert styles.count("white-space: pre-line;") >= 3
    toast_rule = styles.rsplit(".toast {", 1)[1].split("}", 1)[0]
    assert "overflow-wrap: anywhere;" in toast_rule
    assert "white-space: pre-line;" in toast_rule
    assert "word-break: break-word;" in toast_rule


def test_discovery_activity_and_active_item_progress_are_visible(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    source = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    tail = "  initialize();\n})();\n"
    assert tail in source
    source = source.replace(
        tail,
        "  window.__getCounts = getCounts;\n"
        "  window.__getProgress = getProgress;\n"
        "  window.__localizeDiscoveryActivity = localizeDiscoveryActivity;\n"
        "  window.__localizeRuntimeMessage = localizeRuntimeMessage;\n"
        "  window.__progressDescription = progressDescription;\n})();\n",
    )
    discovery_job = {
        "id": "discovering-profile",
        "status": "discovering",
        "platform": "douyin",
        "source_kind": "profile",
        "activity_message": (
            "Retrying Douyin signed profile page 3/300 request 2/3 "
            "(reason: http-429)"
        ),
        "activity_started_at": "2026-09-07T02:00:00Z",
        "items": [],
    }
    transfer_job = {
        "id": "active-transfer",
        "status": "downloading",
        "platform": "douyin",
        "total_items": 4,
        "completed_items": 1,
        "failed_items": 0,
        "active_item_id": "current",
        "items": [
            {"id": "done", "status": "completed"},
            {
                "id": "current",
                "status": "downloading",
                "progress": {"percent": 40},
            },
            {"id": "queued-1", "status": "queued"},
            {"id": "queued-2", "status": "queued"},
        ],
    }
    original_read_job = {
        **transfer_job,
        "items": [
            {
                "id": "current",
                "status": "downloading",
                "progress": {
                    "filename": (
                        "Reading the Douyin original file to verify quality (2k)"
                    ),
                    "percent": 100,
                },
            }
        ],
    }
    transfer_start_job = {
        **transfer_job,
        "items": [
            {
                "id": "current",
                "status": "downloading",
                "progress": {"filename": "Starting Douyin original media transfer"},
            }
        ],
    }
    quality_wait_job = {
        **transfer_job,
        "items": [
            {
                "id": "current",
                "status": "downloading",
                "progress": {
                    "filename": "Waiting for another Douyin quality check (12s)"
                },
            }
        ],
    }
    media_candidate_job = {
        **transfer_job,
        "items": [
            {
                "id": "current",
                "status": "downloading",
                "progress": {
                    "filename": "Checking Douyin media candidate 4/24 (2k)",
                    "percent": 100,
                },
            }
        ],
    }
    quality_candidate_read_job = {
        **transfer_job,
        "items": [
            {
                "id": "current",
                "status": "downloading",
                "progress": {
                    "filename": (
                        "Reading Douyin quality candidate 4/24 (author-feed-1)"
                    ),
                    "percent": 100,
                },
            }
        ],
    }
    harness = (
        "globalThis.window = {};\n"
        "globalThis.document = {querySelector: () => null};\n"
        f"const __discoveryJob = {json.dumps(discovery_job)};\n"
        f"const __transferJob = {json.dumps(transfer_job)};\n"
        f"const __originalReadJob = {json.dumps(original_read_job)};\n"
        f"const __transferStartJob = {json.dumps(transfer_start_job)};\n"
        f"const __qualityWaitJob = {json.dumps(quality_wait_job)};\n"
        f"const __mediaCandidateJob = {json.dumps(media_candidate_job)};\n"
        f"const __qualityCandidateReadJob = "
        f"{json.dumps(quality_candidate_read_job)};\n"
    )
    trailer = (
        "\nDate.now = () => Date.parse('2026-09-07T02:01:05Z');\n"
        "const __discoveryCounts = window.__getCounts(__discoveryJob);\n"
        "const __transferCounts = window.__getCounts(__transferJob);\n"
        "process.stdout.write(JSON.stringify({"
        "activity: window.__progressDescription(__discoveryJob, __discoveryCounts), "
        "freshSession: window.__localizeDiscoveryActivity("
        "'Retrying Douyin signed profile with a fresh signing session "
        "(reason: api-status-nonzero)', __discoveryJob), "
        "itemPage: window.__localizeDiscoveryActivity("
        "'Opening the original Douyin item page', __discoveryJob), "
        "resume: window.__localizeDiscoveryActivity("
        "'Resuming Douyin signed profile at page 2/300 (44 verified items)', "
        "__discoveryJob), "
        "browserStart: window.__localizeDiscoveryActivity("
        "'Starting bounded Douyin browser profile fallback "
        "(reason: signed-integrity; 87s remaining)', __discoveryJob), "
        "browserScan: window.__localizeDiscoveryActivity("
        "'Scanning Douyin browser fallback round 4/300 "
        "(12 verified item(s); 63s remaining)', __discoveryJob), "
        "browserAdded: window.__localizeDiscoveryActivity("
        "'Douyin browser fallback added 3 verified item(s) "
        "(15 total; 120s remaining)', __discoveryJob), "
        "browserTimeout: window.__localizeRuntimeMessage("
        "'Douyin profile discovery stopped after 120 seconds without new "
        "verified profile media. Retry.', __discoveryJob), "
        "progress: window.__getProgress(__transferJob, true), "
        "originalRead: window.__progressDescription(__originalReadJob, __transferCounts), "
        "probeOverall: window.__getProgress(__originalReadJob, true), "
        "transferStart: window.__progressDescription(__transferStartJob, __transferCounts), "
        "qualityWait: window.__progressDescription(__qualityWaitJob, __transferCounts), "
        "mediaCandidate: window.__progressDescription(__mediaCandidateJob, __transferCounts), "
        "qualityCandidateRead: window.__progressDescription("
        "__qualityCandidateReadJob, __transferCounts), "
        "mediaCandidateOverall: window.__getProgress(__mediaCandidateJob, true), "
        "qualityCandidateReadOverall: window.__getProgress("
        "__qualityCandidateReadJob, true)"
        "}));\n"
    )

    completed = _run_node_script(
        node,
        harness + source + trailer,
        tmp_path,
        "discovery-activity-progress.js",
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "activity": (
            "抖音主页第 3 页请求暂时失败（HTTP 429 限流），正在重试"
            "（2/3）（已等待 1 分 5 秒）"
        ),
        "freshSession": ("抖音主页签名会话暂时失败（接口业务状态异常），正在重新建立"),
        "itemPage": "正在打开原抖音作品页，读取完整图文/Live Photo 详情",
        "resume": "正在从抖音主页第 2 页续跑（已验证 44 个作品）",
        "browserStart": (
            "正在切换到有时限的抖音主页浏览器解析"
            "（签名响应未通过完整性校验，剩余 87 秒）"
        ),
        "browserScan": (
            "正在滚动读取抖音主页（第 4/300 轮，已验证 12 个作品，" "剩余 63 秒）"
        ),
        "browserAdded": "浏览器解析新增 3 个抖音作品（共 15 个，剩余 120 秒）",
        "browserTimeout": (
            "抖音主页解析已在连续 120 秒没有发现新的、可验证作品后自动停止。"
            "任务没有假死，也没有把空响应或其他作者的内容当成完成结果；"
            "请稍等一两分钟后从原主页继续，不需要打开 Chrome 验证。"
        ),
        "progress": 35,
        "originalRead": "正在读取抖音原文件以完成画质校验（2k）",
        "probeOverall": 25,
        "transferStart": "正在开始传输抖音原文件",
        "qualityWait": "正在等待前一个抖音画质校验完成（已等待 12 秒）",
        "mediaCandidate": "正在检测抖音媒体候选 4/24（2k）",
        "qualityCandidateRead": ("正在读取抖音画质候选 4/24（author-feed-1）"),
        "mediaCandidateOverall": 25,
        "qualityCandidateReadOverall": 25,
    }


def test_backend_connection_recovers_without_bypassing_version_checks(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    source = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    tail = "  initialize();\n})();\n"
    assert tail in source
    source = source.replace(
        tail,
        "  window.test = { initialize, poll, verifyBackendBuild, api, state };\n})();\n",
    )
    harness = r"""
const assert = require("node:assert/strict");
const vm = require("node:vm");

function makeApp(healthResponses, configResponses = []) {
  const elements = new Map();
  const intervals = [];
  const timeouts = new Map();
  const calls = [];
  const sources = [];
  let timeoutId = 0;
  const element = () => ({
    textContent: "", value: "", hidden: false, disabled: false, dataset: {},
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener() {}, setAttribute() {}, append() {}, replaceChildren() {},
    querySelector() { return this; }, querySelectorAll() { return []; }
  });
  const meta = { "app-id": "downloader", "app-version": "1.0", "app-build": "build-a" };
  const document = {
    hidden: false, body: element(), addEventListener() {},
    querySelector(selector) {
      const match = selector.match(/^meta\[name="(.*)"\]$/);
      if (match) return { content: meta[match[1]] };
      if (!elements.has(selector)) elements.set(selector, element());
      return elements.get(selector);
    },
    querySelectorAll() { return [...elements.values()]; },
    createElement: element
  };
  const healthy = {
    status: "ok", app_id: "downloader", version: "1.0",
    build_id: "build-a", source_build_id: "build-a", restart_required: false
  };
  const jsonResponse = (body) => ({
    ok: true, status: 200, headers: { get: () => "application/json" },
    json: async () => body
  });
  const context = {
    document, AbortController,
    window: {
      addEventListener() {},
      setInterval(fn, delay) { intervals.push({ fn, delay }); },
      setTimeout(fn, delay) { const id = ++timeoutId; timeouts.set(id, { fn, delay }); return id; },
      clearTimeout(id) { timeouts.delete(id); }
    },
    EventSource: class {
      constructor() { this.readyState = 1; sources.push(this); }
      addEventListener() {}
      close() { this.closed = true; }
    },
    fetch: async (path, options) => {
      calls.push({ path, method: options?.method || "GET" });
      if (path === "/api/health") {
        const response = healthResponses.shift() || "ok";
        if (response === "network") throw new Error("temporary disconnect");
        if (response === "timeout") return new Promise((resolve, reject) => {
          options.signal.addEventListener("abort", () => reject(new Error("aborted")));
        });
        if (response === "503") return { ok: false, status: 503 };
        if (response === "bad-json") return { ok: true, json: async () => { throw new Error("invalid JSON"); } };
        if (response === "mismatch") return jsonResponse({ ...healthy, build_id: "build-b" });
        return jsonResponse(healthy);
      }
      if (path === "/api/config") {
        if (configResponses.shift() === "503") return { ok: false, status: 503, json: async () => ({ detail: "temporary settings failure" }) };
        return jsonResponse({ download_dir: "/tmp/downloads", use_chrome_cookies: true });
      }
      if (path === "/api/jobs") return jsonResponse([]);
      return jsonResponse({ status: "ok" });
    }
  };
  context.window.EventSource = context.EventSource;
  vm.runInNewContext(__source, context);
  return { test: context.window.test, intervals, timeouts, calls, sources, elements };
}

(async () => {
  const startup = makeApp(["network", "ok", "503", "ok"]);
  await startup.test.initialize();
  assert.equal(startup.test.state.versionBlocked, false);
  assert.equal(startup.test.state.initialized, false);
  assert.equal(startup.intervals.filter(timer => timer.delay === 4000).length, 1);
  assert.match(startup.elements.get("#connection-status").textContent, /自动重连/);
  await assert.rejects(startup.test.api("/api/jobs/a/retry", { method: "POST" }), /自动重连/);
  assert.equal(startup.calls.filter(call => call.method === "POST").length, 0);

  await startup.intervals.find(timer => timer.delay === 4000).fn();
  assert.equal(startup.test.state.initialized, true);
  assert.equal(startup.test.state.backendReady, true);
  assert.equal(startup.sources.length, 1);
  assert.equal(startup.elements.get("#download-dir").value, "/tmp/downloads");
  await startup.test.api("/api/jobs/a/retry", { method: "POST" });
  assert.equal(startup.calls.filter(call => call.method === "POST").length, 1);

  await startup.test.poll();
  assert.equal(startup.test.state.versionBlocked, false);
  assert.equal(startup.test.state.backendReady, false);
  await assert.rejects(startup.test.api("/api/jobs/a/cancel", { method: "POST" }), /自动重连/);
  await startup.test.poll();
  assert.equal(startup.test.state.backendReady, true);
  assert.equal(startup.sources.length, 1);
  await startup.test.api("/api/config", { method: "PUT", body: "{}" });

  const settingsFailure = makeApp(["ok", "ok"], ["503", "ok"]);
  await settingsFailure.test.initialize();
  assert.equal(settingsFailure.test.state.initialized, false);
  assert.equal(settingsFailure.test.state.backendReady, true);
  await assert.rejects(settingsFailure.test.api("/api/jobs", { method: "POST" }), /读取后台设置/);
  assert.equal(settingsFailure.calls.filter(call => call.method === "POST").length, 0);
  await settingsFailure.test.poll();
  assert.equal(settingsFailure.test.state.initialized, true);
  assert.equal(settingsFailure.sources.length, 1);
  assert.equal(settingsFailure.elements.get("#download-dir").value, "/tmp/downloads");
  assert.equal(settingsFailure.calls.filter(call => call.path === "/api/config").length, 2);
  await settingsFailure.test.api("/api/jobs", { method: "POST" });

  const malformed = makeApp(["bad-json", "ok"]);
  assert.equal(await malformed.test.verifyBackendBuild(), false);
  assert.equal(malformed.test.state.versionBlocked, false);
  assert.equal(await malformed.test.verifyBackendBuild(), true);

  const timedOut = makeApp(["timeout", "ok"]);
  const pending = timedOut.test.verifyBackendBuild();
  assert.equal(timedOut.timeouts.size, 1);
  const timer = [...timedOut.timeouts.values()][0];
  assert.equal(timer.delay, 5000);
  timer.fn();
  assert.equal(await pending, false);
  assert.equal(timedOut.timeouts.size, 0);
  assert.equal(timedOut.test.state.versionBlocked, false);
  assert.equal(await timedOut.test.verifyBackendBuild(), true);

  const mismatch = makeApp(["mismatch", "ok"]);
  assert.equal(await mismatch.test.verifyBackendBuild(), false);
  assert.equal(mismatch.test.state.versionBlocked, true);
  assert.equal(mismatch.test.state.backendReady, false);
  assert.equal(mismatch.elements.get("#download-button").disabled, true);
  await assert.rejects(mismatch.test.api("/api/jobs/a/retry", { method: "POST" }), /版本不一致/);
  assert.equal(await mismatch.test.verifyBackendBuild(), false);
  assert.equal(mismatch.calls.length, 1);
  process.stdout.write("connection recovery and version protection passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    completed = _run_node_script(
        node,
        f"const __source = {json.dumps(source)};\n" + harness,
        tmp_path,
        "backend-connection-recovery.js",
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "connection recovery and version protection passed"
