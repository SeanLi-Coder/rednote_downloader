"""Exercise the actual downloader UI without browser sessions or public requests."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_ROOT = PROJECT_ROOT / "app" / "static"

# Importing the adapter keeps these tests bound to the real backend wording, so a
# change to the English warning text cannot silently drift away from the UI check.
from app import kuaishou as ks


def run_ui(expression: str) -> object:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    source = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    marker = "  initialize();\n})();\n"
    assert source.count(marker) == 1
    source = source.replace(
        marker,
        "  window.testAPI = {getPlatform, platformMeta, isProfileJob, "
        "verificationTarget, localizeDiscoveryActivity, localizeRuntimeMessage, "
        "composeIssueMessage, issuePresentation, issueTitleForJob, "
        "cookieDiagnosticCode, warningPresentation};\n})();\n",
    )
    result = subprocess.run(
        [node, "-e", "globalThis.window = {};\n"
         "globalThis.document = {querySelector: () => null};\n" + source
         + f"\nprocess.stdout.write(JSON.stringify({expression}));\n"],
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("url", [
    "https://www.kuaishou.com/short-video/3x12345678",
    "https://www.kuaishou.com/profile/3x98765432",
    "https://v.kuaishou.com/abcdef",
    "https://m.gifshow.com/fw/photo/3x12345678",
    "https://m.gifshow.com/fw/user/3x98765432",
])
def test_kuaishou_jobs_have_a_recognizable_platform(url):
    assert run_ui(f"window.testAPI.platformMeta({json.dumps({'url': url})})") == {
        "key": "kuaishou", "glyph": "快", "label": "快手",
    }


@pytest.mark.parametrize(("job", "expected"), [
    ({"platform": "kuaishou", "source_kind": "profile"}, "原主页"),
    ({"platform": "kuaishou", "source_kind": "item"}, "原视频"),
    ({"url": "https://www.kuaishou.com/profile/3x98765432"}, "原主页"),
    ({"url": "https://m.gifshow.com/fw/user/3x98765432"}, "原主页"),
    ({"url": "https://v.kuaishou.com/abcdef", "source_kind": "profile"}, "原主页"),
    ({"platform": "kuaishou", "source_kind": "short_link", "resolved_source_kind": "profile"}, "原主页"),
    ({"url": "https://www.kuaishou.com/short-video/3x12345678"}, "原视频"),
])
def test_verification_opens_the_correct_kind_of_original_page(job, expected):
    assert run_ui(f"window.testAPI.verificationTarget({json.dumps(job)})") == expected


@pytest.mark.parametrize("platform", ["xiaohongshu", "bilibili", "youtube", "douyin"])
def test_existing_platform_identity_is_preserved(platform):
    assert run_ui(f"window.testAPI.getPlatform({json.dumps({'platform': platform})})") == platform


def test_explicit_kuaishou_platform_wins_over_share_query_text():
    job = {
        "platform": "kuaishou",
        "source_url": "https://v.kuaishou.com/abcdef?ref=youtube",
    }
    assert run_ui(f"window.testAPI.getPlatform({json.dumps(job)})") == "kuaishou"


def test_entry_form_advertises_supported_kuaishou_inputs_and_login_limit():
    source = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    assert 'platform-dot orange' in source
    assert "快手支持单个视频、图片作品、分享短链接和作者主页" in source
    assert "如网站要求登录或验证" in source
    assert "有权保存" in source
    assert 'id="chrome-profile"' in source
    assert "保存后只影响新任务" in source


def run_profile_settings_ui(initial_profile, typed_profile):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    source = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    marker = "  initialize();\n})();\n"
    assert source.count(marker) == 1
    source = source.replace(
        marker,
        "  window.testAPI = {state, elements, loadConfig, saveConfig, createJob};\n})();\n",
    )
    script = (
        "const initial = " + json.dumps(initial_profile) + ";\n"
        "const typed = " + json.dumps(typed_profile) + ";\n"
        "const requests = [];\n"
        "const notices = [];\n"
        "const element = () => ({value:'', checked:false, textContent:'', disabled:false, "
        "focus(){}, setAttribute(){}, append(value){notices.push(value.textContent)}, "
        "classList:{add(){},remove(){}}, querySelector(){return null}});\n"
        "const ids = new Map(['#chrome-profile','#chrome-cookies','#download-dir',"
        "'#save-settings-button','#settings-saved','#toast-region','#form-error',"
        "'#url-input'].map(id => [id, element()]));\n"
        "globalThis.window = {setTimeout(){}};\n"
        "globalThis.document = {querySelector: id => ids.get(id) || null, "
        "createElement: () => element()};\n"
        "globalThis.fetch = async (path, options={}) => {\n"
        "  requests.push({path, method: options.method || 'GET', body: options.body || null});\n"
        "  return {ok:true,status:200,headers:{get(){return 'application/json'}},"
        "json:async () => options.method ? {} : "
        "{download_dir:'/tmp/fixture-downloads',use_chrome_cookies:true,chrome_profile:initial}};\n"
        "};\n"
        + source
        + "\n(async () => {\n"
        "  const ui = window.testAPI;\n"
        "  ui.state.backendReady = true; ui.state.initialized = true;\n"
        "  await ui.loadConfig();\n"
        "  ui.elements.chromeProfile.value = typed;\n"
        "  await ui.createJob({preventDefault(){}});\n"
        "  const unsavedError = ui.elements.formError.textContent;\n"
        "  await ui.saveConfig({preventDefault(){}});\n"
        "  process.stdout.write(JSON.stringify({requests, notices, unsavedError, "
        "savedProfile:ui.state.chromeProfile, field:ui.elements.chromeProfile.value}));\n"
        "})().catch(error => {process.stderr.write(String(error));process.exitCode=1});\n"
    )
    result = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, encoding="utf-8",
        timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_profile_settings_are_saved_before_new_job_and_trim_new_selection():
    result = run_profile_settings_ui("Default", " Profile 2 ")
    assert "请先保存下载设置" in result["unsavedError"]
    assert [request["path"] for request in result["requests"]] == [
        "/api/config", "/api/config",
    ]
    assert json.loads(result["requests"][1]["body"])["chrome_profile"] == "Profile 2"
    assert result["savedProfile"] == "Profile 2"
    assert result["field"] == "Profile 2"
    assert any("旧任务保留原来的设置" in notice for notice in result["notices"])


def test_profile_settings_reject_new_paths_without_sending_put():
    result = run_profile_settings_ui("Default", "../Profile 2")
    assert [request["path"] for request in result["requests"]] == ["/api/config"]
    assert result["savedProfile"] == "Default"
    assert any("只填写" in notice or "请填写 Default" in notice for notice in result["notices"])


def test_profile_settings_preserve_unchanged_legacy_value():
    result = run_profile_settings_ui("/legacy/profile", "/legacy/profile")
    assert json.loads(result["requests"][1]["body"])["chrome_profile"] == "/legacy/profile"


def test_config_api_validates_new_profile_names_and_preserves_legacy(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app import main as main_module

    saved = []
    monkeypatch.setattr(main_module, "config", main_module.AppConfig(
        download_dir=str(tmp_path), chrome_profile=None,
    ))
    monkeypatch.setattr(main_module, "_save_config", lambda value: saved.append(value.model_copy()))
    monkeypatch.setattr(main_module.manager, "default_output_root", main_module.manager.default_output_root)
    monkeypatch.setattr(main_module.manager.downloader_config, "cookie_browser", main_module.manager.downloader_config.cookie_browser)
    monkeypatch.setattr(main_module.manager.downloader_config, "cookie_profile", main_module.manager.downloader_config.cookie_profile)
    client = TestClient(main_module.app, base_url="http://localhost")
    try:
        payload = {"download_dir": str(tmp_path), "use_chrome_cookies": True}
        for name in ("../Profile 1", "Profile 0", "Other Browser"):
            response = client.put("/api/config", json={**payload, "chrome_profile": name})
            assert response.status_code == 422
            assert "Chrome Profile" in response.json()["detail"]
        assert not saved

        response = client.put("/api/config", json={**payload, "chrome_profile": " Profile 2 "})
        assert response.status_code == 200
        assert response.json()["chrome_profile"] == "Profile 2"
        assert saved[-1].chrome_profile == "Profile 2"

        response = client.put("/api/config", json={**payload, "chrome_profile": ""})
        assert response.status_code == 200
        assert response.json()["chrome_profile"] is None

        main_module.config.chrome_profile = "/legacy/profile"
        response = client.put("/api/config", json={**payload, "chrome_profile": "/legacy/profile"})
        assert response.status_code == 200
        assert response.json()["chrome_profile"] == "/legacy/profile"
    finally:
        client.close()


def test_invalid_kuaishou_port_is_a_client_error_without_url_disclosure():
    from fastapi.testclient import TestClient

    from app import main as main_module

    client = TestClient(main_module.app, base_url="http://localhost")
    try:
        response = client.post(
            "/api/jobs",
            json={"url": "https://www.kuaishou.com:abc/short-video/3x12345678"},
        )
        assert response.status_code == 422
        assert response.json()["detail"] == "Invalid URL port"
        assert "kuaishou.com" not in response.text
    finally:
        client.close()


@pytest.mark.parametrize(("message", "expected"), [
    ("Opening Kuaishou in Chrome to read verified video metadata", "正在通过 Chrome"),
    ("Kuaishou: verified 37 videos across 3 pages", "已验证 37 个视频（3 页）"),
    ("Refreshing Kuaishou video links before download", "刷新下载地址"),
    ("Kuaishou profile discovery is incomplete. Only verified videos were queued; retry.", "不能把当前数量视为全部作品"),
    ("Kuaishou requires security verification. Open Chrome to complete the challenge, then retry.", "不会绕过验证码"),
    ("Kuaishou requires login. Open Chrome to sign in, enable Chrome Cookie, then retry.", "开启 Chrome Cookie"),
    ("Kuaishou Chrome cookies could not be read. Quit Chrome and retry.", "不会静默切换账号"),
    ("Kuaishou profile video belongs to a different author; the response was blocked", "身份未通过校验"),
    ("Kuaishou share link target changed; retry with a new task", "指向的作品或作者已变化"),
    ("Kuaishou media redirect was blocked before requesting an untrusted target", "不可信地址"),
    ("Kuaishou was blocked by the local DNS or web filter. Check the configured proxy or network policy; Chrome verification is not required.", "不是快手验证码"),
    ("Kuaishou browser TLS certificate verification failed. Check the configured proxy certificate or network policy; certificate verification was not disabled.", "不会关闭证书验证"),
])
def test_kuaishou_activity_and_errors_are_localized_without_losing_limits(message, expected):
    result = run_ui(
        f"window.testAPI.localizeRuntimeMessage({json.dumps(message)}, {{platform:'kuaishou'}})"
    )
    assert expected in result
    assert "Kuaishou" not in result


def test_live_discovery_progress_reports_verified_items_not_an_invented_total():
    result = run_ui(
        "window.testAPI.localizeDiscoveryActivity('Kuaishou: verified 37 videos across 3 pages',"
        " {platform:'kuaishou', source_kind:'profile'})"
    )
    assert result == "正在读取快手主页，已验证 37 个视频（3 页）"


COOKIE_DIAGNOSTIC_CASES = [
    ("cookie_decryption_failed", "钥匙串"),
    ("cookie_permission_denied", "完全磁盘访问权限"),
    ("cookie_database_locked", "被占用或锁定"),
    ("chrome_data_directory_missing", "Chrome 的用户数据目录"),
    ("chrome_profile_invalid", "Profile 名称不符合"),
    ("chrome_profile_missing", "Profile 目录已不存在"),
    ("cookie_database_missing", "没有找到 Cookie 数据库"),
    ("cookie_database_invalid", "数据库的结构或内容无法读取"),
    ("cookie_storage_failed", "本地存储错误"),
    ("cookie_reader_failed", "读取组件发生依赖"),
    ("cookie_access_unknown", "不足以归类到具体原因"),
]


@pytest.mark.parametrize(("diagnostic", "expected"), COOKIE_DIAGNOSTIC_CASES)
def test_each_cookie_diagnostic_gets_its_own_guidance(diagnostic, expected):
    """R5: every whitelisted category has distinct advice, not one generic line."""
    raw = (
        "Kuaishou Chrome cookies could not be read. Quit Chrome and retry, or "
        f"disable Chrome Cookie explicitly. Diagnostic: {diagnostic}."
    )
    result = run_ui(
        f"window.testAPI.localizeRuntimeMessage({json.dumps(raw)}, {{platform:'kuaishou'}})"
    )
    assert expected in result
    assert diagnostic in result
    assert "Kuaishou" not in result


@pytest.mark.parametrize(("diagnostic", "expected"), COOKIE_DIAGNOSTIC_CASES)
def test_composed_cookie_issue_keeps_the_specific_category(diagnostic, expected):
    """R5: the composed alert must not collapse back into generic quit-Chrome advice."""
    job = {
        "platform": "kuaishou",
        "status": "failed",
        "issue_code": "cookie_unavailable",
        "diagnostic_code": diagnostic,
        "issue_message": (
            "Kuaishou Chrome cookies could not be read. Quit Chrome and retry, or "
            f"disable Chrome Cookie explicitly. Diagnostic: {diagnostic}."
        ),
    }
    message = run_ui(
        "window.testAPI.composeIssueMessage('cookie_unavailable',"
        f" {json.dumps(job['issue_message'])}, {json.dumps(job)},"
        f" {json.dumps(diagnostic)})"
    )
    assert expected in message
    assert "具体原因：" in message
    assert diagnostic in message


@pytest.mark.parametrize("diagnostic", [code for code, _ in COOKIE_DIAGNOSTIC_CASES])
def test_job_title_names_the_specific_cookie_reason(diagnostic):
    """R5: the alert title states the real reason instead of one fixed headline."""
    job = {
        "platform": "kuaishou",
        "status": "failed",
        "issue_code": "cookie_unavailable",
        "diagnostic_code": diagnostic,
        "error": "Kuaishou Chrome cookies could not be read.",
    }
    title = run_ui(f"window.testAPI.issueTitleForJob({json.dumps(job)})")
    assert title.startswith("Chrome Cookie 读取失败：")
    assert title != "Chrome Cookie 读取失败"


def test_cookie_diagnostic_is_recovered_from_a_persisted_message():
    """R5: an older task that stored only text still resolves its category."""
    job = {
        "platform": "kuaishou",
        "status": "failed",
        "issue_code": "cookie_unavailable",
        "error": (
            "Kuaishou Chrome cookies could not be read. Quit Chrome and retry, or "
            "disable Chrome Cookie explicitly. Diagnostic: cookie_database_locked."
        ),
    }
    assert run_ui(f"window.testAPI.cookieDiagnosticCode({json.dumps(job)})") is None
    title = run_ui(f"window.testAPI.issueTitleForJob({json.dumps(job)})")
    assert "被占用" in title


def test_backend_cookie_diagnostic_whitelist_has_matching_ui_guidance():
    from app.browser import COOKIE_DIAGNOSTIC_CODES

    assert {code for code, _ in COOKIE_DIAGNOSTIC_CASES} == COOKIE_DIAGNOSTIC_CODES


@pytest.mark.parametrize(("diagnostic", "expected"), COOKIE_DIAGNOSTIC_CASES)
def test_douyin_cookie_diagnostics_reach_the_actual_issue_card(diagnostic, expected):
    job = {
        "platform": "douyin", "status": "failed", "issue_code": "cookie_unavailable",
        "diagnostic_code": diagnostic,
        "issue_message": f"Chrome cookies could not be read. Diagnostic code: {diagnostic}.",
    }
    message = run_ui(
        "window.testAPI.composeIssueMessage('cookie_unavailable',"
        f" {json.dumps(job['issue_message'])}, {json.dumps(job)}, {json.dumps(diagnostic)})"
    )
    assert expected in message
    assert diagnostic in message
    assert "验证码" not in message or "不是" in message or "不需要" in message


def test_unknown_cookie_failure_does_not_assert_chrome_is_still_running():
    presentation = run_ui(
        "window.testAPI.issuePresentation('cookie_unavailable', '', 'cookie_access_unknown')"
    )
    assert "不能证明 Chrome 未退出" in presentation["solution"]
    assert "完全退出 Chrome 后重试" not in presentation["solution"]


@pytest.mark.parametrize(
    "value",
    [
        "",
        None,
        "cookie_permission_denied; rm -rf /",
        "/Users/someone/Library/Application Support/Google/Chrome/Default",
        "sessionid=secret-value",
        "signing-validation-failed",
        "CONSTRUCTOR",
        "__proto__",
    ],
)
def test_unknown_or_hostile_diagnostic_values_are_never_displayed(value):
    """R5: only a known safe code is accepted; anything else is dropped."""
    job = {
        "platform": "kuaishou",
        "status": "failed",
        "issue_code": "cookie_unavailable",
        "diagnostic_code": value,
        "error": f"Kuaishou Chrome cookies could not be read. Diagnostic: {value}.",
    }
    assert run_ui(f"window.testAPI.cookieDiagnosticCode({json.dumps(job)})") is None
    message = run_ui(
        "window.testAPI.composeIssueMessage('cookie_unavailable',"
        f" {json.dumps(job['error'])}, {json.dumps(job)}, null)"
    )
    for secret in ("rm -rf", "sessionid", "Chrome/Default", "__proto__"):
        assert secret not in message


def test_disabled_cookie_task_still_gets_its_own_distinct_advice():
    """R5: an explicit cookie-off task must not be shown a local-failure category."""
    raw = "Chrome Cookie is disabled for this task; no browser cookies were read."
    presentation = run_ui(
        f"window.testAPI.issuePresentation('cookie_unavailable', {json.dumps(raw)}, null)"
    )
    assert presentation["diagnostic"] is None
    assert "这个任务创建时关闭了 Chrome Cookie" in presentation["description"]


def localize(text):
    return run_ui(
        f"window.testAPI.localizeRuntimeMessage({json.dumps(text)}, {{platform:'kuaishou'}})"
    )


def test_profile_retry_wait_is_localized_with_progress_and_no_loss():
    """2c: the retry status tells the user work is kept and pagination resumes."""
    result = localize(
        "Kuaishou rate limited the profile; waiting before continuing (2/3)"
    )
    assert "第 2/3 次" in result
    assert "已验证的作品不会丢失" in result
    assert "不会从头重新读取" in result
    assert "Kuaishou" not in result


@pytest.mark.parametrize(("reason", "localized"), [
    ("rejected a profile page", "拒绝了当前主页分页请求"),
    ("profile service is unavailable", "主页服务暂时不可用"),
    ("profile network request failed", "主页网络请求失败"),
])
def test_each_profile_retry_reason_remains_visible(reason, localized):
    result = localize(
        f"Kuaishou {reason}; waiting before continuing (2/3)"
    )
    assert localized in result
    assert "第 2/3 次" in result
    assert "已验证的作品不会丢失" in result


def test_profile_interruption_keeps_works_and_names_the_reason():
    """2c: the interrupted warning states the fixed reason category in Chinese."""
    result = localize(
        f"{ks.PROFILE_INTERRUPTED} Reason category: rate_limited."
    )
    assert "本次读取不完整" in result
    assert "已保存的文件都会保留" in result
    assert "重新读取主页" in result
    assert "校验后复用" in result
    assert "从中断的位置继续" not in result
    assert "不会从头重走" not in result
    assert "网站限制了请求频率" in result
    assert "rate_limited" in result
    assert "Kuaishou" not in result


@pytest.mark.parametrize(
    "category", ["rate_limited", "request_rejected", "site_unavailable", "network_error"]
)
def test_every_recoverable_interruption_category_is_translated(category):
    """2c: no recoverable category falls through to a raw untranslated code."""
    result = localize(f"{ks.PROFILE_INTERRUPTED} Reason category: {category}.")
    assert f"（{category}）" in result
    assert "中断原因：" in result


def test_unknown_interruption_category_is_still_shown_not_dropped():
    """2c: an unrecognized category is displayed raw rather than silently lost."""
    result = localize(f"{ks.PROFILE_INTERRUPTED} Reason category: some_new_code.")
    assert "some_new_code" in result
    assert "中断原因：" in result


def test_skipped_works_summary_lists_affected_ids_and_reasons():
    """2c: the user sees how many works were skipped, why, and which ones."""
    result = localize(
        f"{ks.PROFILE_INCOMPLETE} 3 work(s) could not be verified and were not "
        "queued: no_verifiable_media x2, unsupported_media_type x1. "
        "Affected: #2:3xcover1, #5:3xalbum1, #7:3xcover2."
    )
    assert "3 个作品无法验证" in result
    assert "没有可验证的媒体" in result
    assert "媒体类型暂不支持" in result
    assert "#2:3xcover1" in result
    assert "#7:3xcover2" in result


def test_skipped_works_summary_reports_the_capped_overflow():
    """2c: a capped detail list must say so instead of implying completeness."""
    result = localize(
        f"{ks.PROFILE_INCOMPLETE} 25 work(s) could not be verified and were not "
        "queued: no_verifiable_media x25. Affected: #1:3xa, #2:3xb, #3:3xc, "
        "#4:3xd, #5:3xe, #6:3xf, #7:3xg, #8:3xh, #9:3xi, #10:3xj. "
        "(+10 more listed in task details) A further 5 were only counted; "
        "details are capped."
    )
    assert "25 个作品无法验证" in result
    assert "另有 10 个仅在任务详情中列出" in result
    assert "另有 5 个仅计数" in result


def test_affected_work_list_is_not_parsed_as_reasons():
    """2c: the reason segment parser must not pick up affected-work identifiers."""
    result = localize(
        f"{ks.PROFILE_INCOMPLETE} 2 work(s) could not be verified and were not "
        "queued: queue_limit_reached x2. Affected: #11:3xalpha1, #12:3xbeta2."
    )
    assert "已达到作品数量保护上限（queue_limit_reached） 2 个" in result
    assert result.count("已达到作品数量保护上限") == 1
    assert "#11:3xalpha1" in result


def test_empty_profile_failure_is_not_shown_as_an_empty_profile():
    """2c: an interruption with nothing verified must read as a failure to retry."""
    result = localize(
        "Kuaishou stopped serving the author feed before any work could be "
        "verified, so nothing was queued. Wait a few minutes, then retry the "
        "original profile. Reason category: request_rejected."
    )
    assert "没有加入任何下载项" in result
    assert "不是主页为空" in result
    assert "网站拒绝了请求" in result
    assert "Kuaishou" not in result


def test_problem_summary_never_leaks_site_text_through_localization():
    """2c: captions, cookies and signed URLs must not appear in the UI text."""
    hostile = (
        f"{ks.PROFILE_INCOMPLETE} 1 work(s) could not be verified and were not "
        "queued: no_verifiable_media x1. Affected: #1:3xvideo1."
    )
    result = localize(hostile)
    for secret in ("sessionid", "cookie", "signature", "kwaicdn.com"):
        assert secret not in result


def test_unknown_kuaishou_message_is_safe_and_actionable():
    result = localize(
        "Kuaishou unexpected route: https://fictional.invalid/media?signature=secret"
    )
    assert "从原链接重试一次" in result
    assert "版本和 build ID" in result
    assert "fictional.invalid" not in result
    assert "signature=secret" not in result
