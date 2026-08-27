from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent


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
            "Redirect reason: unrecognized-host",
            "校验指纹：0123456789ab",
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
        capture_output=True,
        check=False,
        timeout=10,
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
