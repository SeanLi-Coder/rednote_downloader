from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
NODE_EXECUTION_TIMEOUT_SECONDS = 30


def test_douyin_redirect_messages_execute_with_safe_legacy_and_reason_parsing() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    source = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
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
            "程序遵守该设置，没有读取 Chrome Cookie",
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

    completed = subprocess.run(
        [node],
        input=harness + source + trailer,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=NODE_EXECUTION_TIMEOUT_SECONDS,
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


def test_interrupted_job_labels_queued_items_as_waiting_to_continue() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    source = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
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
            *[
                {"id": f"queued-{index}", "status": "queued"}
                for index in range(151)
            ],
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

    completed = subprocess.run(
        [node],
        input=harness + source + trailer,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=NODE_EXECUTION_TIMEOUT_SECONDS,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "label": "等待继续",
        "count": "151",
        "progress": "已暂停，等待继续；已处理 1 / 152 个作品",
    }


def test_nonretryable_failed_item_keeps_visible_error_without_retrying() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    source = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
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

    completed = subprocess.run(
        [node],
        input=harness + source + trailer,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=NODE_EXECUTION_TIMEOUT_SECONDS,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert "余下可见作品会继续下载" in result["error"]
    assert result["retryable"] is False
