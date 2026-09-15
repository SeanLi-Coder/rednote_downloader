(() => {
  "use strict";

  const elements = {
    authAlert: document.querySelector("#auth-alert"),
    authMessage: document.querySelector("#auth-message"),
    authTitle: document.querySelector("#auth-title"),
    authOpenButton: document.querySelector("#auth-open-button"),
    authRetryButton: document.querySelector("#auth-retry-button"),
    cancelButton: document.querySelector("#cancel-button"),
    chromeCookies: document.querySelector("#chrome-cookies"),
    connectionStatus: document.querySelector("#connection-status"),
    downloadButton: document.querySelector("#download-button"),
    downloadDir: document.querySelector("#download-dir"),
    downloadForm: document.querySelector("#download-form"),
    emptyState: document.querySelector("#empty-state"),
    failureCount: document.querySelector("#failure-count"),
    activeCount: document.querySelector("#active-count"),
    activeCountLabel: document.querySelector("#active-count-label"),
    buildInfo: document.querySelector("#build-info"),
    filterTabs: document.querySelector("#filter-tabs"),
    formError: document.querySelector("#form-error"),
    historyList: document.querySelector("#history-list"),
    itemTemplate: document.querySelector("#item-template"),
    itemsList: document.querySelector("#items-list"),
    itemsSummary: document.querySelector("#items-summary"),
    jobEta: document.querySelector("#job-eta"),
    jobHeading: document.querySelector("#job-heading"),
    jobPlatform: document.querySelector("#job-platform"),
    jobSpeed: document.querySelector("#job-speed"),
    jobStatus: document.querySelector("#job-status"),
    jobTime: document.querySelector("#job-time"),
    jobView: document.querySelector("#job-view"),
    platformMark: document.querySelector("#platform-mark"),
    progressBar: document.querySelector("#progress-bar"),
    progressLabel: document.querySelector("#progress-label"),
    progressPercent: document.querySelector("#progress-percent"),
    progressTrack: document.querySelector("#progress-track"),
    refreshButton: document.querySelector("#refresh-button"),
    retryAllButton: document.querySelector("#retry-all-button"),
    retryAllLabel: document.querySelector("#retry-all-label"),
    saveSettingsButton: document.querySelector("#save-settings-button"),
    settingsForm: document.querySelector("#settings-form"),
    settingsSaved: document.querySelector("#settings-saved"),
    successCount: document.querySelector("#success-count"),
    toastRegion: document.querySelector("#toast-region"),
    totalCount: document.querySelector("#total-count"),
    urlInput: document.querySelector("#url-input"),
    versionAlert: document.querySelector("#version-alert"),
    versionAlertDetail: document.querySelector("#version-alert-detail"),
    warningAlert: document.querySelector("#warning-alert"),
    warningMessage: document.querySelector("#warning-message"),
    warningTitle: document.querySelector("#warning-title")
  };

  const state = {
    backendReady: false,
    chromeProfile: null,
    eventSource: null,
    filter: "all",
    initialized: false,
    jobs: new Map(),
    polling: false,
    pollingTick: 0,
    refreshTimer: null,
    selectedJobId: null,
    versionBlocked: false
  };

  const statusLabels = {
    cancelled: "已取消",
    completed: "已完成",
    downloading: "下载中",
    failed: "失败",
    needs_auth: "等待验证",
    pending: "等待中",
    preparing: "准备中",
    retrying: "重试中",
    unknown: "状态未知"
  };

  const issueTitles = {
    rate_limited: "网站正在限流",
    verification_required: "网站要求完成验证码",
    login_required: "需要登录或建立站点会话",
    request_rejected: "网站拒绝了本次请求",
    site_processing: "作品仍在网站处理中",
    content_unavailable: "作品已删除、私密或不可见",
    region_restricted: "作品在当前地区不可用",
    site_response_changed: "网站返回的数据发生变化",
    media_link_expired: "媒体地址已失效",
    site_unavailable: "网站服务暂时异常",
    network_error: "网络连接暂时异常",
    cookie_unavailable: "Chrome Cookie 读取失败",
    security_blocked: "异常媒体响应已安全拦截",
    local_configuration: "本机运行环境需要处理",
    unknown: "任务执行遇到异常"
  };

  const issueStatusLabels = {
    rate_limited: "被限流",
    verification_required: "等验证码",
    login_required: "需登录",
    request_rejected: "请求被拒",
    site_processing: "网站处理中",
    content_unavailable: "不可用",
    region_restricted: "地区受限",
    site_response_changed: "响应异常",
    media_link_expired: "地址失效",
    site_unavailable: "网站异常",
    network_error: "网络异常",
    cookie_unavailable: "Cookie 异常",
    security_blocked: "已拦截",
    local_configuration: "环境异常",
    unknown: "失败"
  };

  const douyinLivePhotoStaticFallbackWarning = "Some Douyin image positions reported Live Photo data, but no complete trusted motion rendition was available. The highest-pixel static images will be saved for those positions. Create a new task from the original link later to retry the dynamic versions.";

  const issueDescriptions = {
    rate_limited: "网站明确返回了限流信号。程序已经停止继续请求，避免整批作品连续失败。",
    verification_required: "网站明确显示了验证码或安全验证页面。",
    login_required: "网站要求有效的登录状态，或当前 Chrome 会话已经失效。",
    request_rejected: "网站拒绝了本次请求，但没有明确要求验证码。程序已经暂停后续队列。",
    site_processing: "网站仍在处理、审核或转码这个作品，目前还没有提供可下载的原文件。",
    content_unavailable: "网站表示作品已删除、设为私密、当前不可见，或当前账号没有访问权限。程序没有保存替代内容。",
    region_restricted: "网站表示该作品在当前地区不可用。程序没有尝试绕过地区限制。",
    site_response_changed: "网站返回了空数据、非媒体内容、字段变化或与目标不一致的数据。程序已拦截异常响应，没有下载串号或低清替代文件。",
    media_link_expired: "网站之前签发的媒体地址已经失效。程序没有继续使用旧地址。",
    site_unavailable: "网站服务器返回了 5xx 或暂时不可用。程序已暂停后续请求。",
    network_error: "网络连接、DNS、TLS 或媒体读取过程失败。程序已停止等待并保留此前完成的文件。",
    cookie_unavailable: "程序无法读取任务绑定的 Chrome Cookie，因此不能可靠确认当前登录权限或最高画质。",
    security_blocked: "媒体地址出现了未通过安全校验的跳转或响应变化。程序在读取或保存未知响应前已经拦截。",
    local_configuration: "本机缺少或无法启动下载和画质校验需要的组件。",
    unknown: "程序遇到了尚未归类的异常，暂时无法仅凭现有响应确定网站做了什么。"
  };

  const issueSolutions = {
    rate_limited: "停止连续点击重试，先等待 1–2 分钟再点击“继续任务”；如果仍被限流，请延长等待时间，并确认浏览器中的原链接也能正常访问。",
    verification_required: "点击上方“打开 Chrome 验证”，在任务原链接实际显示的页面完成验证码，再回到这里点击“我已完成，继续重试”。",
    login_required: "点击上方“打开 Chrome 登录”，使用任务绑定的 Chrome Profile 登录并确认原链接可正常浏览，然后回到这里继续重试。",
    request_rejected: "先等待几分钟，再从原链接继续任务；若反复出现，请确认账号能在浏览器访问原内容，并检查代理或 VPN 是否改写了网站请求。没有看到验证码时不需要执行验证码操作。",
    site_processing: "先在网站页面确认作品已经审核、转码完成并可以正常播放或查看，然后再点击重试。处理尚未完成时只能等待网站生成原文件。",
    content_unavailable: "用相同账号在浏览器打开原链接确认权限。如果作品已删除、私密或账号无权访问，本工具无法下载，请跳过该作品；内容恢复可见后再新建任务。",
    region_restricted: "确认当前账号和所在地区是否获得网站授权访问。本工具不会绕过地区限制；请在网站允许访问的合法环境中使用，或跳过该作品。",
    site_response_changed: "先阅读下方具体情况。只有具体情况说明这是临时空响应、限流或站点字段变化时，才等待一两分钟后点击“继续任务”或“重试”，让程序从原链接重新解析；若提示目标已变化、身份无法验证、旧任务不可恢复或要求新建任务，请不要重试旧任务，应检查原链接并重新创建任务。若持续出现，请更新到最新版，并反馈脱敏后的技术详情、版本号和 build ID。",
    media_link_expired: "点击“继续任务”或“重试”，让程序从原链接刷新当前作品的媒体地址；不要重复使用旧任务中保存的签名地址。",
    site_unavailable: "先在浏览器确认网站是否也无法打开，等待服务恢复后再继续任务；不要在 5xx 持续期间反复提交整页下载。",
    network_error: "检查网络、DNS、防火墙以及代理或 VPN，确认浏览器能打开原链接后点击继续任务。若传输持续有新字节，程序会继续等待，不需要重启。",
    cookie_unavailable: "完全退出 Chrome 后重试，并允许系统读取 Cookie；确认选中了已登录的 Chrome Profile。若只下载公开内容，也可以关闭 Cookie，但必须从原链接创建新任务。",
    security_blocked: "不要手动放行未知地址。先关闭可能改写 HTTPS 的代理或 VPN，再从原链接重试；若仍复现，只反馈界面显示的安全原因或短指纹，不要发送带签名的完整媒体链接。",
    local_configuration: "按下方技术详情安装或修复缺失组件，然后完全停止并重新启动程序。macOS 缺少 FFprobe/FFmpeg 时运行“brew install ffmpeg”；Windows 或 Linux 请按 README 安装 FFmpeg；浏览器组件缺失时重新运行启动脚本安装依赖。",
    unknown: "先从原链接重试一次；如果相同错误再次出现，请保留已完成文件，并把完整的脱敏技术详情、页面版本号和 build ID 发给开发者。"
  };

  const knownIssueCodes = new Set(Object.keys(issueTitles));

  function firstDefined(...values) {
    return values.find((value) => value !== undefined && value !== null && value !== "");
  }

  function asNumber(value) {
    if (typeof value === "number" && Number.isFinite(value)) {
      return value;
    }
    if (typeof value === "string" && value.trim() !== "") {
      const parsed = Number(value.replace(/[% ,]/g, ""));
      return Number.isFinite(parsed) ? parsed : null;
    }
    return null;
  }

  function asText(value) {
    if (typeof value === "string") {
      return value;
    }
    if (value instanceof Error) {
      return value.message;
    }
    if (value && typeof value === "object") {
      return firstDefined(value.message, value.detail, value.error, "") || "";
    }
    return value === undefined || value === null ? "" : String(value);
  }

  function getEntityId(entity) {
    return firstDefined(entity?.id, entity?.job_id, entity?.jobId, entity?.uuid);
  }

  function rawStatus(entity) {
    return String(firstDefined(entity?.status, entity?.state, entity?.phase, "pending"))
      .trim()
      .toLowerCase()
      .replace(/[ -]+/g, "_");
  }

  function canonicalStatus(entity) {
    const value = rawStatus(entity);

    if (["complete", "completed", "done", "downloaded", "partial", "skipped", "success", "succeeded"].includes(value)) {
      return "completed";
    }
    if (["failed", "failure", "error", "errored", "interrupted"].includes(value)) {
      return "failed";
    }
    if (["cancelled", "canceled", "aborted", "stopped"].includes(value)) {
      return "cancelled";
    }
    if (["needs_auth", "auth_required", "captcha_required", "verification_required", "login_required"].includes(value)) {
      return "needs_auth";
    }
    if (["downloading", "running", "in_progress", "processing", "postprocessing", "crawling", "fetching", "active"].includes(value)) {
      return "downloading";
    }
    if (["preparing", "parsing", "extracting", "discovering", "starting", "initializing"].includes(value)) {
      return "preparing";
    }
    if (["retry", "retrying", "restarting"].includes(value)) {
      return "retrying";
    }
    if (["queued", "queue", "pending", "waiting", "created"].includes(value)) {
      return "pending";
    }
    return "unknown";
  }

  function runtimeIssueMessage(entity) {
    return asText(firstDefined(
      entity?.issue_message,
      entity?.issueMessage,
      entity?.auth_message,
      entity?.authMessage,
      entity?.error_message,
      entity?.error,
      entity?.warning,
      entity?.reason,
      entity?.message,
      ""
    ));
  }

  function inferRuntimeIssueCode(entity) {
    const text = runtimeIssueMessage(entity).toLowerCase();
    if (!text) return null;
    const explicitlyNotAuth = /captcha (?:verification )?is not required|a captcha is not required|chrome verification is not required|verification (?:page )?is not required/i.test(text);
    if (!explicitlyNotAuth && /captcha required|captcha challenge|complete (?:the )?captcha|solve the captcha|verify you are human|请完成验证码|请完成验证|请拖动滑块/i.test(text)) return "verification_required";
    const reason = text.match(/reason category:\s*([a-z0-9-]+)/i)?.[1];
    if (reason === "http-429") return "rate_limited";
    if (["http-403", "signed-rejected"].includes(reason)) return "request_rejected";
    if (reason === "http-5xx") return "site_unavailable";
    if (["network-timeout", "network-error", "signer-timeout"].includes(reason)) return "network_error";
    if (reason?.startsWith("api-") || reason === "no-progress-timeout") return "site_response_changed";
    if (/http(?: error)?\s*429|too many requests|rate[- ]limit|(?:访问|请求|操作)(?:过于|太)?频繁|频繁操作/i.test(text)) return "rate_limited";
    if (/chrome cookies could not be read|cookie is disabled for this task|cookie was disabled when this task was created|cookie database|failed to (?:load|decrypt).*cookie|unsupported cookie-browser|bound chrome profile/i.test(text)) return "cookie_unavailable";
    if (/still processing|being (?:processed|transcoded)|under review|正在处理|审核中|转码中/i.test(text)) return "site_processing";
    if (/not available in your country|geo[- ]restricted|region restricted|http error 451/i.test(text)) return "region_restricted";
    if (/media endpoint returned (?:an empty response|http (?:401|404|410))|media (?:link|url) expired|signature expired|url has expired|saved access token/i.test(text)) return "media_link_expired";
    if (/http(?: error)?\s*(?:404|410)|video (?:is )?unavailable|video not available|this video is private|content is unavailable|private video|has been (?:removed|deleted)|no longer available/i.test(text)) return "content_unavailable";
    if (/untrusted|outside the trusted|unrecognized douyin cdn|redirect could not be trusted|nonstandard-port|blocked media route|tls certificate validation failed/i.test(text)) return "security_blocked";
    if (/different (?:video|author|note)|cross-wired|identity or integrity|integrity validation|incomplete verified|missing aweme|no verified media identity|response changed|did not match the verified|did not return (?:video data|an mp4 file|a verified)|returned (?:text or metadata|an empty file|no verified)|blank browser response|no trusted notes/i.test(text)) return "site_response_changed";
    if (/media endpoint returned http 425|http(?: error)?\s*403|forbidden|access denied|request rejected|temporarily rejected|风控|网络环境存在风险|(?:your )?ip (?:address )?is blocked/i.test(text)) return "request_rejected";
    if (/http(?: error)?\s*5\d\d|service unavailable|server unavailable|server error|bad gateway/i.test(text)) return "site_unavailable";
    if (/media endpoint returned http 408|secure media connection failed|incomplete media response|network error|network request failed|connection (?:failed|reset|refused)|timed out|timeout|no media progress|local dns or web filter blocked|blocked[.]dnsfilter[.]com/i.test(text)) return "network_error";
    if (/ffprobe was not found|ffprobe was found but could not be started|ffmpeg was not found/i.test(text)) return "local_configuration";
    if (canonicalStatus(entity) === "needs_auth") {
      if (!explicitlyNotAuth && /captcha|verification challenge|verify you are human|验证码|安全验证/i.test(text)) return "verification_required";
      return "login_required";
    }
    return "unknown";
  }

  function issueCode(entity) {
    const explicit = String(firstDefined(entity?.issue_code, entity?.issueCode, "")).trim().toLowerCase();
    return knownIssueCodes.has(explicit) ? explicit : inferRuntimeIssueCode(entity);
  }

  function primaryJobIssueCode(job) {
    const direct = issueCode(job);
    if (direct && direct !== "unknown") return direct;
    const itemCode = getItems(job)
      .filter(isFailed)
      .map(issueCode)
      .find((code) => code && code !== "unknown");
    return itemCode || direct;
  }

  function primaryJobIssueMessage(job) {
    const direct = runtimeIssueMessage(job);
    if (job?.issue_message || job?.issueMessage || (direct && !/^\d+ item\(s\) failed$/i.test(direct))) {
      return direct;
    }
    const code = primaryJobIssueCode(job);
    const item = getItems(job).find((candidate) => isFailed(candidate) && issueCode(candidate) === code);
    return runtimeIssueMessage(item) || direct;
  }

  function issueTitleForJob(job) {
    return issueTitles[primaryJobIssueCode(job)] || issueTitles.unknown;
  }

  function issuePresentation(code, raw = "") {
    const normalized = knownIssueCodes.has(code) ? code : "unknown";
    let description = issueDescriptions[normalized] || issueDescriptions.unknown;
    let solution = issueSolutions[normalized] || issueSolutions.unknown;
    const rawText = asText(raw);
    if (
      normalized === "cookie_unavailable"
      && /cookie is disabled for this task|cookie (?:was|is) disabled when this task was created|automatic item refresh was skipped because chrome cookie is disabled/i.test(rawText)
    ) {
      description = "这个任务创建时关闭了 Chrome Cookie。程序遵守任务原有设置，没有读取浏览器 Cookie，因此无法安全刷新受限媒体地址。";
      solution = "在下载设置中开启“自动读取 Chrome Cookie”，然后从原链接创建一个新任务；继续旧任务仍会保持 Cookie 关闭。";
    }
    return { description, solution };
  }

  function issueResolutionText(code, raw = "") {
    const { description, solution } = issuePresentation(code, raw);
    return `发生了什么：${description}\n解决办法：${solution}`;
  }

  function composeIssueMessage(code, raw, job) {
    const summary = issueResolutionText(code, raw);
    if (!raw) return summary;
    const localized = localizeRuntimeMessage(raw, job);
    return localized === raw
      ? `${summary}\n技术详情：${raw}`
      : `${summary}\n具体情况：${localized}`;
  }

  function localizedIssueMessage(entity, job = entity) {
    const raw = runtimeIssueMessage(entity);
    const code = issueCode(entity) || "unknown";
    return composeIssueMessage(code, raw, job);
  }

  function localizedPrimaryJobIssueMessage(job) {
    const raw = primaryJobIssueMessage(job);
    const code = primaryJobIssueCode(job) || "unknown";
    return composeIssueMessage(code, raw, job);
  }

  function warningPresentation(job) {
    const needsAuth = authRequired(job);
    const discoveryIncomplete = job?.discovery_complete === false;
    const nonAuthNeedsAuthState = canonicalStatus(job) === "needs_auth" && !needsAuth;
    const isFailure = !needsAuth && (
      canonicalStatus(job) === "failed"
      || rawStatus(job) === "partial"
      || nonAuthNeedsAuthState
    );
    if (isFailure) {
      return {
        title: issueTitleForJob(job),
        message: localizedPrimaryJobIssueMessage(job),
        isAlert: true
      };
    }
    if (job?.warning) {
      if (asText(job.warning).trim() === douyinLivePhotoStaticFallbackWarning) {
        return {
          title: "部分 Live Photo 已保存为静态图",
          message: "发生了什么：作品的部分图片位标记了 Live Photo，但程序没有取得完整可信且明确无水印的动态图，因此没有把低清或来源不明的视频冒充原始动态图。\n解决办法：这些位置已保存最高像素静态图；若之后仍想取得动态图，请稍后从原链接新建任务重试。",
          isAlert: false
        };
      }
      const warningEntity = { status: "failed", issue_message: job.warning };
      let code = issueCode(warningEntity) || "unknown";
      const explicitCode = String(firstDefined(job?.issue_code, job?.issueCode, "")).trim().toLowerCase();
      if (code === "unknown" && knownIssueCodes.has(explicitCode)) code = explicitCode;
      return {
        title: issueTitles[code] || "任务提示",
        message: composeIssueMessage(code, job.warning, job),
        isAlert: false
      };
    }
    if (job?.cookie_fallback_used) {
      const raw = "Chrome cookies could not be read, so anonymous access was used; profile results or restricted highest-quality media may be incomplete.";
      return {
        title: "Chrome Cookie 读取失败，当前使用未登录模式",
        message: composeIssueMessage("cookie_unavailable", raw, job),
        isAlert: false
      };
    }
    if (discoveryIncomplete && !isRunning(job)) {
      const raw = "The site did not confirm that profile discovery reached the end.";
      return {
        title: "主页发现可能不完整",
        message: composeIssueMessage("site_response_changed", raw, job),
        isAlert: false
      };
    }
    return null;
  }

  function statusTone(entity) {
    if (rawStatus(entity) === "partial") return "warning";
    const status = canonicalStatus(entity);
    if (status === "completed") return "success";
    if (status === "failed") return "failed";
    if (status === "needs_auth") return "warning";
    if (["downloading", "preparing", "retrying"].includes(status)) return "active";
    return "neutral";
  }

  function isRunning(entity) {
    return ["pending", "preparing", "downloading", "retrying"].includes(canonicalStatus(entity));
  }

  function isFailed(entity) {
    return ["failed", "needs_auth"].includes(canonicalStatus(entity));
  }

  function isRetryableItem(item) {
    return item?.retryable !== false && (isFailed(item) || canonicalStatus(item) === "cancelled");
  }

  function getItems(job) {
    const items = firstDefined(job?.items, job?.media, job?.downloads, job?.results, job?.entries, []);
    if (Array.isArray(items)) return items;
    if (items && typeof items === "object") return Object.values(items);
    return [];
  }

  function getActiveItem(job) {
    const items = getItems(job);
    const activeId = job?.active_item_id;
    const isActivePhase = (item) => ["preparing", "downloading", "retrying"].includes(canonicalStatus(item));
    if (activeId !== undefined && activeId !== null) {
      const matched = items.find((item) => {
        const itemId = firstDefined(item?.id, item?.item_id, item?.media_id);
        return String(itemId) === String(activeId);
      });
      if (matched && isActivePhase(matched)) return matched;
    }
    return items.find(isActivePhase);
  }

  function isDouyinProbeProgress(item) {
    const message = asText(item?.progress?.filename).trim();
    return (
      message.startsWith("Checking Douyin ")
      || message.startsWith("Reading Douyin quality candidate ")
      || message.startsWith("Reading the Douyin original file to verify quality")
      || message.startsWith("Waiting for another Douyin quality check")
      || message.startsWith("Retrying Douyin quality ")
      || message.startsWith("Preparing Douyin signed ")
      || message.startsWith("Retrying Douyin signed ")
      || message.startsWith("Fetching Douyin signed ")
      || message === "Fetching Douyin signing HTML"
      || message === "Loading Douyin Chrome cookies"
      || message === "Starting the Douyin signing browser"
      || message === "Starting the Douyin signing session"
      || message === "Waiting for the Douyin signed request slot"
      || message.startsWith("Starting bounded Douyin browser profile fallback")
      || message === "Launching Douyin browser fallback"
      || message === "Opening the Douyin profile in the bounded browser fallback"
      || message.startsWith("Scanning Douyin browser fallback ")
      || message.startsWith("Douyin browser fallback added ")
    );
  }

  function getCounts(job) {
    const items = getItems(job);
    const stats = job?.stats || job?.statistics || {};
    const progress = typeof job?.progress === "object" ? job.progress : {};
    const calculated = items.reduce(
      (counts, item) => {
        const status = canonicalStatus(item);
        if (status === "completed") counts.success += 1;
        if (isFailed(item)) counts.failed += 1;
        if (isRunning(item)) counts.active += 1;
        return counts;
      },
      { success: 0, failed: 0, active: 0 }
    );

    const success = asNumber(firstDefined(
      job?.success_count,
      job?.succeeded_count,
      job?.completed_count,
      job?.completed_items,
      stats.success,
      stats.succeeded,
      progress.success,
      items.length ? calculated.success : undefined,
      0
    ));
    const failed = asNumber(firstDefined(
      job?.failure_count,
      job?.failed_count,
      job?.failed_items,
      stats.failed,
      stats.failure,
      progress.failed,
      items.length ? calculated.failed : undefined,
      0
    ));
    const active = asNumber(firstDefined(
      job?.active_count,
      job?.downloading_count,
      stats.active,
      stats.downloading,
      progress.active,
      items.length ? calculated.active : undefined,
      0
    ));
    const total = asNumber(firstDefined(
      job?.total_count,
      job?.total_items,
      job?.item_count,
      stats.total,
      progress.total,
      items.length || undefined
    ));

    return {
      active: Math.max(0, active || 0),
      failed: Math.max(0, failed || 0),
      success: Math.max(0, success || 0),
      total: total === null ? null : Math.max(0, total)
    };
  }

  function activeCountLabel(job, activeCount) {
    if (activeCount <= 0) return "处理中";
    const jobStatus = canonicalStatus(job);
    if (
      rawStatus(job) === "interrupted" ||
      rawStatus(job) === "partial" ||
      jobStatus === "failed"
    ) {
      return job?.retryable === false ? "未处理" : "等待继续";
    }
    if (jobStatus === "needs_auth") return "等待验证";
    if (jobStatus === "cancelled") return "未处理";
    return "处理中";
  }

  function normalizePercent(value, fractionHint = false) {
    if (typeof value === "string" && value.includes("%")) {
      const parsed = asNumber(value);
      return parsed === null ? null : Math.min(100, Math.max(0, parsed));
    }
    const parsed = asNumber(value);
    if (parsed === null) return null;
    const scaled = fractionHint && parsed >= 0 && parsed <= 1 ? parsed * 100 : parsed;
    return Math.min(100, Math.max(0, scaled));
  }

  function getProgress(entity, isJob = false) {
    if (canonicalStatus(entity) === "completed" && rawStatus(entity) !== "partial") return 100;

    const nested = entity?.progress && typeof entity.progress === "object" ? entity.progress : {};
    const direct = firstDefined(
      entity?.progress_percent,
      entity?.progress_percentage,
      entity?.percentage,
      entity?.percent,
      nested.percent,
      nested.percentage
    );
    const directValue = normalizePercent(direct);
    if (directValue !== null) return directValue;

    if (typeof entity?.progress === "number" || typeof entity?.progress === "string") {
      const progressValue = normalizePercent(entity.progress, true);
      if (progressValue !== null) return progressValue;
    }

    const current = asNumber(firstDefined(
      entity?.current,
      entity?.downloaded,
      entity?.downloaded_bytes,
      nested.current,
      nested.completed,
      nested.downloaded_bytes
    ));
    const total = asNumber(firstDefined(entity?.total, entity?.size, entity?.total_bytes, nested.total, nested.total_bytes));
    if (current !== null && total && total > 0) {
      return Math.min(100, Math.max(0, (current / total) * 100));
    }

    if (isJob) {
      const counts = getCounts(entity);
      if (counts.total && counts.total > 0) {
        const processed = Math.min(counts.total, counts.success + counts.failed);
        const activeItem = getActiveItem(entity);
        const activeContribution = activeItem && !isDouyinProbeProgress(activeItem)
          ? getProgress(activeItem) / 100
          : 0;
        return Math.min(100, ((processed + activeContribution) / counts.total) * 100);
      }
    }
    return 0;
  }

  function formatBytes(value) {
    const bytes = asNumber(value);
    if (bytes === null || bytes < 0) return "";
    if (bytes === 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
    const result = bytes / 1024 ** index;
    return `${result >= 100 || index === 0 ? result.toFixed(0) : result.toFixed(1)} ${units[index]}`;
  }

  function formatSpeed(job) {
    const activeItem = getActiveItem(job);
    const value = firstDefined(
      job?.speed,
      job?.download_speed,
      job?.speed_bps,
      job?.bytes_per_second,
      job?.progress?.speed,
      job?.progress?.speed_bytes_per_second,
      activeItem?.progress?.speed,
      activeItem?.progress?.speed_bytes_per_second
    );
    if (typeof value === "string" && value.trim()) return value;
    const formatted = formatBytes(value);
    return formatted ? `${formatted}/秒` : "—";
  }

  function formatDuration(value) {
    if (typeof value === "string" && value.trim() && asNumber(value) === null) return value;
    const secondsValue = asNumber(value);
    if (secondsValue === null || secondsValue < 0) return "—";
    const seconds = Math.round(secondsValue);
    if (seconds < 60) return `${seconds} 秒`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    return `${hours} 小时 ${minutes} 分`;
  }

  function formatEta(job) {
    const activeItem = getActiveItem(job);
    return formatDuration(firstDefined(
      job?.eta,
      job?.eta_seconds,
      job?.remaining_seconds,
      job?.progress?.eta,
      job?.progress?.eta_seconds,
      activeItem?.progress?.eta,
      activeItem?.progress?.eta_seconds
    ));
  }

  function toDate(value) {
    if (!value) return null;
    if (value instanceof Date) return value;
    if (typeof value === "number") {
      return new Date(value < 10_000_000_000 ? value * 1000 : value);
    }
    if (typeof value === "string" && /^\d{8}$/.test(value)) {
      const year = Number(value.slice(0, 4));
      const month = Number(value.slice(4, 6)) - 1;
      const day = Number(value.slice(6, 8));
      return new Date(year, month, day);
    }
    if (typeof value === "string" && /^\d{10,13}$/.test(value)) {
      const timestamp = Number(value);
      return new Date(value.length === 10 ? timestamp * 1000 : timestamp);
    }
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function formatRelativeTime(value) {
    const date = toDate(value);
    if (!date) return "刚刚";
    const difference = Date.now() - date.getTime();
    if (difference < 60_000) return "刚刚";
    if (difference < 3_600_000) return `${Math.max(1, Math.floor(difference / 60_000))} 分钟前`;
    if (difference < 86_400_000) return `${Math.max(1, Math.floor(difference / 3_600_000))} 小时前`;
    if (difference < 604_800_000) return `${Math.max(1, Math.floor(difference / 86_400_000))} 天前`;
    return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit" }).format(date);
  }

  function formatDate(value) {
    const date = toDate(value);
    if (!date) return "";
    return new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false
    }).format(date);
  }

  function getPlatform(job) {
    const explicit = String(firstDefined(job?.platform, job?.site, job?.source, "")).toLowerCase();
    const url = String(firstDefined(job?.url, job?.source_url, job?.profile_url, "")).toLowerCase();
    const combined = `${explicit} ${url}`;
    if (combined.includes("xiaohongshu") || combined.includes("xhs") || combined.includes("rednote")) return "xiaohongshu";
    if (combined.includes("bilibili") || combined.includes("b23.tv") || combined.includes("b站")) return "bilibili";
    if (combined.includes("youtube") || combined.includes("youtu.be")) return "youtube";
    if (combined.includes("douyin") || combined.includes("抖音")) return "douyin";
    return "unknown";
  }

  function platformMeta(job) {
    const key = getPlatform(job);
    const metadata = {
      bilibili: { glyph: "B", label: "B站" },
      douyin: { glyph: "抖", label: "抖音" },
      unknown: { glyph: "链", label: "正在识别平台" },
      xiaohongshu: { glyph: "红", label: "小红书" },
      youtube: { glyph: "YT", label: "YouTube" }
    };
    return { key, ...metadata[key] };
  }

  function isProfileJob(job) {
    const kind = String(firstDefined(job?.source_kind, job?.sourceKind, job?.kind, "")).toLowerCase();
    if (["profile", "channel", "user"].includes(kind)) return true;
    if (["item", "video", "note"].includes(kind)) return false;
    const url = String(firstDefined(job?.source_url, job?.url, job?.profile_url, ""));
    return /\/user\/[^/?#]+(?:[/?#]|$)/i.test(url);
  }

  function isXiaohongshuVerificationItem(job) {
    if (platformMeta(job).key !== "xiaohongshu") return false;
    const url = String(firstDefined(job?.verification_url, job?.verificationUrl, ""));
    return /\/(?:explore|discovery\/item)\/[0-9a-f]+(?:[/?#]|$)/i.test(url);
  }

  function isXiaohongshuLoginSessionIssue(job) {
    const message = String(firstDefined(job?.auth_message, job?.authMessage, job?.error, ""));
    return platformMeta(job).key === "xiaohongshu" && message.includes("login session");
  }

  function verificationTarget(job) {
    if (isXiaohongshuVerificationItem(job)) return "触发验证的作品";
    if (isProfileJob(job)) return "原主页";
    return platformMeta(job).key === "xiaohongshu" ? "原作品" : "原视频";
  }

  function getAuthor(job) {
    const author = firstDefined(job?.author_name, job?.author, job?.creator, job?.uploader, job?.username);
    if (author && typeof author === "object") {
      return firstDefined(author.name, author.nickname, author.username, author.id, "正在识别作者…");
    }
    return author || "正在识别作者…";
  }

  function getCreatedAt(job) {
    return firstDefined(job?.created_at, job?.createdAt, job?.started_at, job?.startedAt, job?.timestamp);
  }

  function statusLabel(entity, job = null) {
    if (["failed", "needs_auth"].includes(canonicalStatus(entity)) || rawStatus(entity) === "interrupted") {
      const code = job && entity !== job ? issueCode(entity) : primaryJobIssueCode(entity);
      if (code && code !== "unknown" && issueStatusLabels[code]) return issueStatusLabels[code];
    }
    const exactLabels = {
      discovering: "解析中",
      interrupted: "已中断",
      partial: "部分完成",
      postprocessing: "处理中",
      queued: "排队中",
      skipped: "已跳过"
    };
    if (exactLabels[rawStatus(entity)]) return exactLabels[rawStatus(entity)];
    const canonical = canonicalStatus(entity);
    if (canonical !== "unknown") return statusLabels[canonical];
    const original = rawStatus(entity);
    return original && original !== "unknown" ? original : statusLabels.unknown;
  }

  function itemTitle(item, index) {
    const path = firstDefined(item?.filename, item?.file_name, item?.output_path, item?.path, item?.output_paths?.[0]);
    const pathName = typeof path === "string" ? path.split(/[\\/]/).pop() : "";
    const title = firstDefined(item?.title, item?.name, pathName, item?.description, `作品 ${String(index + 1).padStart(2, "0")}`);
    const untitledLabels = {
      "Untitled Douyin video": "无标题抖音视频",
      "Untitled Douyin image": "无标题抖音图文",
      "Untitled Xiaohongshu note": "小红书作品（标题解析中）"
    };
    if (typeof title === "string" && title.startsWith("Recovered Douyin files ")) {
      return `已保留的旧抖音文件 ${title.slice("Recovered Douyin files ".length)}`;
    }
    return untitledLabels[title] || title;
  }

  function itemOutputPaths(item) {
    const paths = firstDefined(item?.output_paths, item?.files, item?.saved_files, []);
    if (Array.isArray(paths)) {
      return paths.filter((value) => typeof value === "string" && value.trim());
    }
    const single = firstDefined(item?.output_path, item?.path, item?.filename);
    return typeof single === "string" && single.trim() ? [single] : [];
  }

  function itemType(item) {
    const outputPaths = itemOutputPaths(item);
    const hasVideoOutput = outputPaths.some((value) => /\.(?:m4v|mov|mp4|webm)$/i.test(value));
    const hasImageOutput = outputPaths.some((value) => /\.(?:avif|gif|jpe?g|png|webp)$/i.test(value));
    const selectedFormat = String(firstDefined(item?.selected_format, item?.format, "")).toLowerCase();
    if (selectedFormat.includes("live-photo") && hasVideoOutput) {
      return hasImageOutput ? "动态图 + 图片" : "动态图";
    }
    const type = String(firstDefined(item?.type, item?.media_type, item?.kind, item?.extension, "文件")).toLowerCase();
    if (["image", "photo", "picture", "jpg", "jpeg", "png", "webp"].some((value) => type.includes(value))) return "图片";
    if (["video", "mp4", "webm", "mov"].some((value) => type.includes(value))) return "视频";
    return type === "文件" ? type : type.toUpperCase();
  }

  function itemMetadata(item) {
    const parts = [itemType(item)];
    const resolution = firstDefined(
      item?.resolution,
      item?.quality,
      item?.format,
      item?.selected_format,
      item?.width && item?.height ? `${item.width}×${item.height}` : undefined
    );
    if (resolution) parts.push(String(resolution));
    const size = formatBytes(firstDefined(
      item?.file_size,
      item?.size_bytes,
      item?.total_bytes,
      item?.filesize,
      item?.progress?.total_bytes
    ));
    if (size) parts.push(size);
    const date = formatDate(firstDefined(item?.published_at, item?.publish_time, item?.upload_date, item?.date, item?.created_at));
    if (date) parts.push(date);
    return parts.join(" · ");
  }

  function itemError(item, job) {
    const status = canonicalStatus(item);
    if (!isFailed(item) && status !== "cancelled" && rawStatus(item) !== "skipped") return "";
    const fallback = status === "cancelled"
      ? "下载已取消，可以重新尝试"
      : item?.retryable === false
        ? "该记录不能自动重试，请查看具体原因"
        : "下载失败，请重试";
    const errorEntity = runtimeIssueMessage(item)
      ? item
      : { ...item, issue_message: fallback };
    return localizedIssueMessage(errorEntity, job);
  }

  function localizedRenditionMismatch(text) {
    return text.replace(
      /verified media was below the author-feed (\d+)x(\d+) rendition(?: \(measured (\d+)x(\d+)\))?/g,
      (_, width, height, actualWidth, actualHeight) => actualWidth
        ? `该地址声明 ${width}×${height}，实际文件只有 ${actualWidth}×${actualHeight}`
        : `该地址实际文件低于声明的 ${width}×${height}`
    );
  }

  function localizedDurationMismatch(text) {
    return text.replace(
      /media duration did not match the requested Douyin item \(expected ([\d.]+)s, measured ([\d.]+)s, tolerance ([\d.]+)s\)/g,
      "作品声明时长 $1 秒，完整媒体实测 $2 秒，允许差值 $3 秒，仍不一致"
    ).replaceAll("media duration did not match the requested Douyin item", "媒体时长与目标抖音作品不匹配");
  }

  function localizedProbeDetails(text) {
    const marker = "Probe details:";
    if (!text.includes(marker)) return "";
    return localizedDurationMismatch(localizedRenditionMismatch(text))
      .split(marker, 2)[1]
      .trim()
      .replace(/[.。]+$/, "")
      .replaceAll("author-feed", "作者直连")
      .replaceAll("default", "原始档")
      .replaceAll("media request or FFprobe timed out", "媒体请求或 FFprobe 超时")
      .replaceAll("media endpoint network request failed", "媒体端点网络请求失败")
      .replaceAll("media endpoint redirected to an unrecognized Douyin CDN host", "媒体端点跳转到尚未识别的抖音地域 CDN 主机")
      .replaceAll("media endpoint returned HTTP", "媒体端点返回 HTTP")
      .replaceAll("media metadata could not be parsed", "无法解析媒体信息")
      .replaceAll("media endpoint did not return video data", "媒体端点没有返回视频")
      .replaceAll("media endpoint did not return an MP4 file", "媒体端点没有返回 MP4")
      .replaceAll("media size changed between the range probe and local probe", "媒体文件大小在两次校验之间发生变化")
      .replaceAll("media content changed between the range probe and local probe", "媒体文件内容在两次校验之间发生变化")
      .replaceAll("media file exceeded the safe probe size limit", "媒体文件超过安全探测大小上限")
      .replaceAll("FFprobe could not parse the media stream", "FFprobe 无法解析媒体流");
  }

  const douyinRedirectReasonLabels = Object.freeze({
    "malformed-url": "跳转地址格式异常",
    "embedded-credentials": "跳转地址包含不应出现的账号信息",
    "missing-host": "跳转地址缺少主机名",
    "hostname-too-long": "主机名长度异常",
    "non-ascii-host": "主机名不是可验证的 ASCII 域名",
    "ip-literal": "跳转目标是 IP 地址，不是可验证的官方 CDN 域名",
    "local-or-special-use-host": "跳转目标是本地、内网或保留用途域名",
    "single-label-host": "跳转目标不是完整域名",
    "invalid-hostname": "主机名格式不符合 DNS 规则",
    "non-https-scheme": "媒体地址被降级为非 HTTPS",
    "nonstandard-port": "媒体地址使用了非标准 HTTPS 端口",
    "too-many-redirects": "媒体地址的连续跳转次数超过安全上限",
    "unverified-source-binding": "域名属于已知 CDN，但当前任务缺少把它绑定到该作品最高画质的完整校验指纹",
    "unrecognized-host": "该域名尚未列入可信抖音媒体 CDN",
  });

  const douyinCdnFamilies = Object.freeze([
    "douyin.com",
    "douyinvod.com",
    "amemv.com",
    "zjcdn.com",
    "douyincdn.com",
    "idouyinvod.com",
    "pstatp.com",
  ]);

  function douyinCdnFamily(host) {
    return douyinCdnFamilies.find((family) => (
      host === family || host.endsWith(`.${family}`)
    )) || "";
  }

  function douyinRedirectDiagnostic(text) {
    const rawHost = (text.match(/(?:Redirect host:\s*|\(host:\s*)([a-z0-9.-]+)/i)?.[1] || "")
      .toLowerCase()
      .replace(/\.+$/, "");
    const fingerprint = (
      text.match(/(?:Redirect host fingerprint:\s*|host-fingerprint:\s*)([0-9a-f]{12})/i)?.[1] || ""
    ).toLowerCase();
    const rawPort = text.match(/(?:Redirect port:\s*|\bport:\s*)(\d{1,5})/i)?.[1] || "";
    const parsedPort = Number(rawPort);
    const port = Number.isInteger(parsedPort) && parsedPort >= 1 && parsedPort <= 65535
      ? parsedPort
      : null;
    const reason = text.match(/(?:Redirect reason:\s*|reason:\s*)([a-z0-9-]+)/i)?.[1]?.toLowerCase() || "";
    const labels = rawHost.split(".");
    const isSafeHostname = (
      rawHost.length <= 253 &&
      labels.length >= 2 &&
      !labels.every((label) => /^\d+$/.test(label)) &&
      labels.every((label) => /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label))
    );
    return {
      host: isSafeHostname ? rawHost : "",
      fingerprint,
      port,
      legacyInvalidHost: rawHost === "invalid-host" && !reason,
      reason,
      reasonText: douyinRedirectReasonLabels[reason] || "具体类别未知",
    };
  }

  function douyinRedirectMessage(text, phase) {
    const diagnostic = douyinRedirectDiagnostic(text);
    const isDefaultProbe = /Probe details:\s*default:/i.test(text);
    const isAuthorFeedProbe = /Probe details:\s*author-feed-\d+:/i.test(text);
    const fallbackText = isDefaultProbe
      ? "程序已自动尝试四条官方同画质路由，仍未完成验证。"
      : isAuthorFeedProbe
        ? "程序已自动尝试该同档的作者直连镜像，仍未完成验证。"
        : phase === "原文件下载"
          ? "程序已自动尝试本作品其余已验证的同画质入口，仍未能完成传输。"
          : "";
    if (diagnostic.legacyInvalidHost) {
      return `这是旧版本保存的抖音${phase}地址校验结果：“invalid-host”不是实际域名，旧记录没有保留具体失败类别。这个被拦截的媒体响应没有保存，此前已完成的文件会保留，并暂停了后续队列；请从原链接重试，若再次被拦截会显示具体原因。请先检查代理或 VPN，不需要打开 Chrome 验证。`;
    }
    if (!diagnostic.reason && diagnostic.host) {
      return `这是旧版本保存的抖音${phase}地址校验结果。旧记录没有保留具体失败类别，而且主机名可能包含敏感标识，因此新版不会回显或要求你发送它。这个被拦截的媒体响应没有保存，此前已完成的文件会保留，并暂停了后续队列；请从原链接重试，若再次被拦截会显示可安全反馈的具体原因或校验指纹，不需要打开 Chrome 验证。`;
    }
    if (diagnostic.reason === "unverified-source-binding") {
      const family = douyinCdnFamily(diagnostic.host);
      const hostText = family ? `（CDN 域名族：${family}）` : "";
      return `抖音${phase}跳转到了已知媒体 CDN${hostText}，但当前任务缺少把该地址绑定到这条作品最高画质所需的完整校验指纹。这个媒体响应没有保存，此前已完成的文件会保留，并暂停了后续队列；请从原主页或原视频链接继续或重试，让程序重新解析最高画质，不需要打开 Chrome 验证。`;
    }
    if (diagnostic.reason === "unrecognized-host") {
      const fingerprintText = diagnostic.fingerprint ? `（校验指纹：${diagnostic.fingerprint}）` : "";
      const feedbackText = diagnostic.fingerprint
        ? "若关闭代理后仍出现，只需把这个校验指纹发给开发者"
        : diagnostic.reason
          ? "若关闭代理后仍出现，请从原链接重试；新版再次拦截时会显示可反馈的校验指纹"
          : "若关闭代理后仍出现，请从原链接重试";
      return `抖音${phase}跳转到了尚未识别的媒体 CDN${fingerprintText}。${fallbackText}程序在读取文件前已拦截，这个媒体响应没有保存；此前已完成的文件会保留，并暂停了后续队列。请先检查代理或 VPN 后重试；${feedbackText}，不要发送带签名的完整媒体链接，也不需要打开 Chrome 验证。`;
    }
    if (diagnostic.reason === "nonstandard-port") {
      const family = douyinCdnFamily(diagnostic.host);
      const familyText = family ? `CDN 域名族：${family}` : "CDN 域名族：未识别";
      const portText = diagnostic.port ? `端口：${diagnostic.port}` : "端口：旧记录未保存";
      const fingerprintText = diagnostic.fingerprint
        ? `，校验指纹：${diagnostic.fingerprint}`
        : "";
      return `抖音${phase}地址使用了尚未验证的非标准 HTTPS 端口（${familyText}，${portText}${fingerprintText}）。${fallbackText}程序在读取文件前已拦截，没有保存这个媒体响应；此前已完成的文件会保留，并暂停后续队列。仅凭这个结果无法判断是已失效的旧媒体地址、尚未确认的 CDN，还是代理或 VPN 改写。当前版本会在任务执行时从原任务链接自动刷新当前作品一次；如果这是升级前保留的历史错误，请点击继续任务以执行刷新。刷新后仍出现时，再检查代理或 VPN 并稍后重试。只需反馈这里显示的域名族、端口或校验指纹，不要发送带签名的完整媒体链接，也不需要打开 Chrome 验证。`;
    }
    return `抖音${phase}的重定向地址未通过安全校验。原因：${diagnostic.reasonText}。程序在读取文件前已拦截，这个媒体响应没有保存；此前已完成的文件会保留，并暂停了后续队列。请检查代理或 VPN 是否改写了媒体地址，稍后从原链接重试，不需要打开 Chrome 验证。`;
  }

  function douyinSigningValidationMessage(text) {
    const prefixes = [
      "Douyin signed discovery failed before a verified response",
      "Douyin signed data failed identity or integrity validation",
      "Douyin author-feed data failed identity or integrity validation",
      "Douyin automatic item refresh did not pass identity or integrity validation",
      "Douyin automatic media refresh did not pass identity or integrity validation"
    ];
    if (!prefixes.some((prefix) => text.includes(prefix))) return null;

    const identityAdvice = "请先核对原链接对应的作品，再从正确链接新建任务尝试一次；若仍出现，请停止反复重试并反馈诊断码、版本号和 build ID。";
    const refreshAdvice = "请更新到最新版后从原链接重新解析一次；若仍出现，请反馈诊断码、版本号和 build ID，不要连续重试。";
    const labels = {
      "ssr-redirect": ["原作品页面返回了跳转指令，当前响应无法直接用于作品校验；这不等于已确认跳到了其他视频", refreshAdvice],
      "ssr-unknown-item": ["原作品页面没有提供可确认的目标作品身份", identityAdvice],
      "ssr-item-mismatch": ["原作品页面返回的作品 ID 与目标作品不一致", identityAdvice],
      "ssr-author-mismatch": ["原作品页面返回的作者身份与任务绑定的作者不一致", identityAdvice],
      "ssr-conflicting-items": ["原作品页面返回了相互冲突的作品信息，无法确认唯一目标", identityAdvice],
      "detail-item-mismatch": ["详情接口返回的作品 ID 与目标作品不一致", identityAdvice],
      "detail-author-mismatch": ["详情接口返回的作者身份与任务绑定的作者不一致", identityAdvice],
      "detail-response-redirect": ["详情接口的响应地址与已验证的请求目标不一致", refreshAdvice],
      "detail-metadata-incomplete": ["详情接口缺少完成作品校验所需的媒体信息，或字段格式无法识别", refreshAdvice],
      "ssr-metadata-incomplete": ["原作品页面缺少完成作品校验所需的媒体信息，或字段格式无法识别", refreshAdvice],
      "signer-html-invalid": ["签名初始化页面未通过格式或完整性校验", refreshAdvice],
      "signer-script-invalid": ["签名初始化脚本未通过来源或完整性校验", refreshAdvice],
      "signing-validation-failed": ["当前作品未能完成校验，现有诊断不足以确定更具体的原因", "请反馈原作品链接、诊断码、版本号和 build ID，不要反复更新或重试。"],
      "signing-runtime-error": ["签名组件运行异常，未取得可验证的作品响应", "请更新程序并重启后从原链接尝试一次；若仍出现，请反馈诊断码、版本号和 build ID，不要连续重试。"]
    };
    // Only a single, exact allowlisted suffix may be displayed, never exception text.
    const match = text.match(/ Diagnostic code: ([a-z][a-z0-9-]{0,63})\.$/);
    const code = match && match.index + match[0].length === text.length ? match[1] : null;
    if (
      text.split("Diagnostic code:").length !== 2 ||
      !code ||
      !Object.prototype.hasOwnProperty.call(labels, code)
    ) {
      return "抖音响应未通过作品身份或完整性校验，程序已停止处理。本次记录没有可识别的诊断码，无法据此确定失败原因。请反馈原作品链接、版本号和 build ID；如果已更新并新建任务仍出现，不要反复更新或重试。不需要打开 Chrome 验证。";
    }
    const [detail, advice] = labels[code];
    return `抖音解析已停止：${detail}。程序没有接受该响应或下载替代内容。${advice}不需要打开 Chrome 验证。诊断码：${code}。`;
  }

  function localizeRuntimeMessage(value, job = null) {
    let text = asText(value);
    const signingValidation = douyinSigningValidationMessage(text);
    if (signingValidation) return signingValidation;
    if (text.startsWith("Could not save settings. Check free disk space and write permissions for the project's data folder")) {
      return "设置未能保存，程序仍在使用之前的设置。请检查磁盘剩余空间，以及项目 data 文件夹是否有写入权限，处理后再次点击“保存设置”。";
    }
    if (text.includes("Some Douyin image positions reported Live Photo data")) {
      const localizedWarning = "部分抖音图片位带有 Live Photo 数据，但程序没有取得完整可信且明确无水印的动态图版本；这些位置会保存最高像素静态图。若之后仍想重试动态图，请稍后从原链接新建任务。";
      const withoutCurrentWarning = text.replace(douyinLivePhotoStaticFallbackWarning, "");
      const markerIndex = text.indexOf("Some Douyin image positions reported Live Photo data");
      const remaining = (
        withoutCurrentWarning === text
          ? text.slice(0, markerIndex)
          : withoutCurrentWarning
      ).trim();
      return remaining
        ? `${localizeRuntimeMessage(remaining, job)}\n${localizedWarning}`
        : localizedWarning;
    }
    if (text.includes("A local DNS or web filter blocked Douyin before the site loaded")) {
      return "本机的 DNS 或网页过滤器在抖音页面加载前拦截了访问。请在过滤器中放行抖音，或关闭 DNS 过滤、切换网络，然后从原链接重试；这不是抖音验证码，不需要打开 Chrome 验证。";
    }
    if (text.includes("Douyin quality verification made no media progress for 120 seconds")) {
      return "抖音最高画质校验已在连续 120 秒没有收到新的媒体字节或有效探测结果后自动停止。任务没有假死，也没有改下低清版本；此前已完成的文件会保留，请稍等后点击继续任务。";
    }
    if (text.includes("Douyin media transfer made no media progress for 120 seconds")) {
      return "抖音原文件传输已在连续 120 秒没有收到任何新字节后自动停止。任务没有假死；此前已完成的文件会保留，请稍等后点击继续任务。";
    }
    if (text.includes("Refreshing this Douyin item from the original task link")) {
      return "检测到抖音媒体路由异常，正在从原任务链接只刷新当前作品并自动重试……";
    }
    if (text.includes("Chrome cookies could not be read") && text.includes("Fully quit Chrome")) {
      return "无法读取 Chrome Cookie。请完全退出 Chrome（包括所有窗口）后直接重试，或关闭“自动读取 Chrome Cookie”后重新创建任务；这不是验证码，不需要打开验证页面。";
    }
    if (text.includes("Chrome cookies could not be read, so anonymous access was used")) {
      return "无法读取 Chrome Cookie，本次已明确回退到未登录模式；作品列表或受限的最高画质可能不完整。请检查 Chrome 登录状态后重新创建任务。";
    }
    if (text.includes("legacy Douyin task contains an unverified numeric queue")) {
      return "这是旧版本留下的未验证数字队列，原视频身份已经无法安全恢复。程序已停止继续下载，也不会再打开错误的博主主页。请重新粘贴原始主页或视频链接创建新任务；已有文件不会被删除。";
    }
    if (text.includes("legacy Douyin item task must be rediscovered")) {
      return "这是旧版本错误展开的抖音单视频任务。程序已移除未下载的数字条目；请点击继续任务，从原视频链接重新解析。已有文件不会被删除。";
    }
    if (text.includes("preserved legacy entry was not returned by the refreshed Douyin profile")) {
      return "这条旧记录在重新解析主页时已不存在或不可见，程序已跳过且不会反复下载；以前保存的文件仍保留在磁盘中。";
    }
    if (text.includes("partially downloaded Douyin profile entry was not returned by a complete verified profile refresh")) {
      return "这条作品在完整刷新主页后已不存在或当前不可见，程序不会再使用它的旧媒体地址。此前成功保存的图片或视频仍保留；该记录已设为不可自动重试，余下可见作品会继续下载。";
    }
    if (text.includes("Douyin profile task contains legacy entries without complete verified author and media metadata")) {
      return "这是旧版本留下的不完整抖音主页队列。程序已移除未保存的数字占位项；点击继续任务会从原主页重新解析，已有文件不会被删除。";
    }
    if (text.includes("This task was paused by an older version after a generic Douyin signing failure")) {
      return "旧版本把普通抖音签名失败暂停成了验证码状态。请直接从原链接继续任务；只有抖音明确显示验证码或登录页面时才需要打开 Chrome。";
    }
    if (text.includes("legacy Douyin profile result must be manually reviewed")) {
      return "这个旧版抖音主页结果缺少可靠的作者归属证据，已停止自动重试。请人工检查已有文件，并用原主页链接创建新任务。";
    }
    if (text.includes("legacy Douyin item task no longer has a verifiable original video URL")) {
      return "这个旧版抖音单作品任务已经丢失可验证的原视频地址，程序不会猜测目标或继续下载。请重新粘贴原视频链接创建任务。";
    }
    if (
      text.includes("Douyin profile discovery temporarily returned incomplete verified media metadata") ||
      text.includes("Douyin returned profile media without complete verified metadata") ||
      text.includes("Douyin returned the requested profile item without complete, verified media metadata") ||
      text.includes("Douyin browser discovery returned media without complete verified metadata") ||
      text.includes("Douyin returned incomplete profile media metadata") ||
      text.includes("Douyin profile media metadata is incomplete") ||
      text.includes("Douyin returned incomplete profile titles or ownership metadata") ||
      text.includes("Douyin profile retry returned only a partial author feed") ||
      text.includes("Douyin profile discovery temporarily returned no verified media items") ||
      text.includes("Douyin profile discovery temporarily timed out or was rate-limited") ||
      text.includes("Douyin profile discovery temporarily returned no verified profile-owned media") ||
      text.includes("Douyin profile discovery temporarily returned a blank browser response") ||
      text.includes("Douyin profile discovery was temporarily rate-limited")
    ) {
      return "抖音主页本次临时没有返回完整、可验证的作品信息。程序没有生成数字占位项，也没有下载低清文件；请稍等一两分钟后直接重试，这种临时响应不需要打开 Chrome 验证。";
    }
    if (text.includes("Douyin profile discovery stopped after 120 seconds without new verified profile media")) {
      return "抖音主页解析已在连续 120 秒没有发现新的、可验证作品后自动停止。任务没有假死，也没有把空响应或其他作者的内容当成完成结果；请稍等一两分钟后从原主页继续，不需要打开 Chrome 验证。";
    }
    if (text.includes("Douyin stopped returning new videos before the profile reported completion") || text.includes("Douyin reached the discovery safety limit before confirming the end of the profile")) {
      return "抖音主页在确认列表结束前停止返回新作品。当前结果可能不完整，请稍后点击继续发现；已有成功记录不会重复下载。";
    }
    if (text.includes("Douyin returned an uploader profile instead of the requested video")) {
      return "抖音把目标视频错误返回成了博主主页，程序已拦截这些非目标作品。请直接从原视频链接重试；没有出现明确验证码或登录页面时，不需要打开 Chrome 验证。";
    }
    if (text.includes("Douyin could not create a verified signed request")) {
      return "这是旧版本把通用签名失败误标成了验证码。请直接从原链接重试；只有抖音明确显示验证码或登录页面时才需要打开 Chrome。";
    }
    if (text.includes("No current Douyin session cookies were found")) {
      return `任务绑定的 Chrome Profile 中没有找到当前可用的抖音会话 Cookie。请用该 Profile 打开${verificationTarget(job)}建立站点会话；只有抖音页面明确要求时才登录，然后回来重试。本次没有检测到验证码。`;
    }
    if (text.includes("Douyin explicitly requested a current login session") || text.includes("Douyin requires a current Chrome login session") || text.includes("The site requires a current Chrome login session")) {
      return `网站明确要求有效的 Chrome 登录状态。请在 Chrome 打开${verificationTarget(job)}并登录，然后回到这里重试；本次没有检测到验证码。`;
    }
    if (text.includes("displayed an explicit CAPTCHA or verification")) {
      return `网站明确显示了验证码或安全验证页面。请在 Chrome 打开${verificationTarget(job)}，完成页面上实际出现的验证后重试。`;
    }
    if (text.includes("Douyin requires current Chrome cookies or an explicit verification")) {
      return `抖音明确要求最新 Chrome Cookie、登录或验证码。请在 Chrome 打开${verificationTarget(job)}完成验证后再重试。`;
    }
    if (text.includes("Reason category: api-filtered-images-base")) {
      return "抖音的简化详情接口只返回了图文/Live Photo 过滤结果。程序已经尝试从原作品页读取完整详情，但本次仍未取得可验证的动态图与图片数据，因此没有把背景音频误当成视频，也没有下载串号或低清替代文件。请等待一两分钟后点击“继续任务”或“重试”，让程序从原作品链接重新解析；没有明确验证码或登录页面时，不需要打开 Chrome 验证。";
    }
    if (text.includes("Douyin signed discovery stopped after 120 seconds without verified progress")) {
      const reason = text.match(/Reason category:\s*([a-z0-9-]+)/i)?.[1];
      const detail = douyinSignedReasonLabel(reason);
      return `抖音解析已在 120 秒无有效进展后自动停止${detail ? `（${detail}）` : ""}。任务没有假死，也没有下载低清或串号文件；请稍等一两分钟后从原链接重试，不需要打开 Chrome 验证。`;
    }
    if (text.includes("Douyin temporarily limited the verified author-feed request") || text.includes("Douyin temporarily limited a signed request")) {
      const reason = text.match(/Reason category:\s*([a-z0-9-]+)/i)?.[1];
      const detail = douyinSignedReasonLabel(reason);
      return `抖音作者接口正在短时限流或临时拒绝请求${detail ? `（${detail}）` : ""}，程序自动退避重试后仍未恢复。请等待一两分钟后重试；程序没有改下低清版本，也不需要先打开作者主页验证。`;
    }
    if (text.includes("Douyin automatic item refresh was skipped because Chrome Cookie is disabled")) {
      return "检测到抖音媒体路由异常，但这个任务创建时已关闭 Chrome Cookie，程序遵守该任务设置，没有读取 Chrome Cookie，也没有复用旧媒体地址。请开启 Chrome Cookie 后，从原链接创建一个新任务；继续旧任务仍会保持 Cookie 关闭。";
    }
    if (text.includes("Douyin automatic item refresh returned media below the previously verified quality floor")) {
      return "抖音为当前作品返回了新的媒体地址，但其分辨率、编码或码率低于任务此前已验证的质量档。程序没有覆盖旧证据，也没有下载降档文件；请稍后继续任务。";
    }
    if (
      text.includes("Douyin automatic item refresh") ||
      text.includes("Douyin automatic media refresh") ||
      text.includes("Douyin item identity changed during automatic media refresh")
    ) {
      return "抖音媒体路由异常后，程序已从原任务链接定向刷新当前作品，但新响应没有通过作品身份或完整性校验。旧媒体地址没有被复用，也没有下载低清或串号内容；请稍后继续任务，不需要打开 Chrome 验证。";
    }
    if (text.includes("Douyin media transfer was temporarily unavailable")) {
      return "抖音原文件传输遇到临时网络错误或限流。程序已保留完成的文件并暂停后续队列，避免整页连续失败；请稍等后点击继续任务，不需要打开其他作品或作者主页验证。";
    }
    if (
      text.includes("media endpoint redirected to an unrecognized Douyin CDN host") &&
      text.includes("Probe details:")
    ) {
      return douyinRedirectMessage(text, "画质探测");
    }
    if (text.includes("Douyin media redirect could not be trusted")) {
      return douyinRedirectMessage(text, "原文件下载");
    }
    if (text.includes("Untrusted Douyin media URL was blocked before reading")) {
      return douyinRedirectMessage(text, "原文件下载");
    }
    if (text.includes("media endpoint redirected to an unrecognized Douyin CDN host")) {
      return douyinRedirectMessage(text, "画质探测");
    }
    if (text.includes("The site temporarily rate-limited the request")) {
      return "网站正在短时限流。请等待一两分钟后直接重试；没有明确验证码或登录页面时，不需要打开 Chrome 验证。";
    }
    if (text.includes("Douyin signed discovery temporarily failed before a verified response")) {
      return "抖音签名解析在拿到可验证响应前遇到临时网络或超时错误。请稍等后从原链接重试，不需要打开 Chrome 验证。";
    }
    if (text.includes("Douyin returned data for a different video while requesting") || text.includes("Douyin returned data from a different author while requesting") || text.includes("Douyin author-feed enrichment returned a different media identity")) {
      return "抖音返回了其他视频或其他作者的数据，程序已拦截，未下载串号内容。请从原链接直接重试，不需要打开 Chrome 验证。";
    }
    if (text.includes("Douyin profile discovery redirected outside the trusted Douyin origin")) {
      return "抖音主页意外跳转到了非抖音地址，程序已停止处理。请检查原链接后重试，不要在该跳转页面输入账号或验证码。";
    }
    if (text.includes("Douyin redirected to an explicit login or verification page")) {
      return `抖音已明确跳转到登录或验证页面。请在 Chrome 打开${verificationTarget(job)}完成登录或验证码后重试。`;
    }
    if (text.includes("The site requires login, fresh browser cookies, or a CAPTCHA")) {
      return "该网站需要登录、最新的 Chrome Cookie 或验证码。请在 Chrome 完成验证后重试。";
    }
    if (text.includes("Douyin did not return a verified media identity")) {
      return "抖音没有返回可验证的目标视频身份。请稍等一两分钟后从原视频链接重试；没有出现明确验证码或登录页面时，不需要打开 Chrome 验证。";
    }
    if (text.includes("Douyin could not find the requested video in its verified author feed")) {
      return "抖音作者接口本次没有返回目标视频，因此无法确认最高画质。请稍等一两分钟后直接重试；程序没有改下低清版本，也不需要先打开作者主页验证。";
    }
    if (text.includes("Douyin could not verify the author's highest-quality renditions")) {
      return "抖音暂时无法从该作者的已验证作品数据中确认最高画质。请在 Chrome 打开原视频完成验证码或登录后重试。";
    }
    if (
      text.includes("Douyin author-feed quality renditions were unavailable") ||
      text.includes("Douyin author-feed data did not include a verified direct highest-quality rendition") ||
      text.includes("Douyin item discovery returned no verified author-feed direct rendition")
    ) {
      return "抖音作者接口本次没有返回可验证的最高画质直连。程序没有改下可能较低的原始档；请等待一两分钟后直接继续当前任务，不需要打开 Chrome 验证。";
    }
    if (
      text.includes("Douyin highest-quality verification requires the verified author feed") ||
      text.includes("This saved Douyin task predates author-feed highest-quality verification") ||
      text.includes("This saved Douyin Live Photo task predates author-feed highest-quality verification") ||
      text.includes("Douyin Live Photo has no structured author-feed quality renditions")
    ) {
      return "当前任务缺少作者接口返回的最高画质直连，程序已暂停并拒绝只下载可能较低的原始档。请开启“自动读取 Chrome Cookie”，再从原始主页或视频链接创建一个新任务；已有文件不会删除。";
    }
    if (
      text.includes("Downloaded video content did not match the verified Douyin media endpoint") ||
      text.includes("Downloaded video codec did not match its verified media endpoint") ||
      text.includes("Downloaded audio codec did not match its verified media endpoint") ||
      text.includes("Downloaded video duration did not match its verified media metadata")
    ) {
      return "抖音最终返回的文件与刚才验证的目标视频指纹不一致，疑似链接过期、换档或串号，程序已丢弃临时文件。请从原链接重试；不会保留错误视频。";
    }
    if (
      text.includes("Douyin authoritative author-feed quality source was temporarily unavailable") ||
      text.includes("Douyin authoritative default quality source was temporarily unavailable")
    ) {
      const details = localizedProbeDetails(text);
      return `抖音视频的作者直连或原始档遇到临时网络、限流或未知 CDN。程序已暂停后续队列，避免把全部作品连续标失败，也没有改下低清版本；请稍等后点击继续任务${details ? `。本次原因：${details}` : ""}。`;
    }
    if (text.includes("Douyin Live Photo authoritative quality source was temporarily unavailable")) {
      const details = localizedProbeDetails(text);
      return `抖音 Live Photo 的作者直连或原始档遇到临时网络、限流或未知 CDN。程序已暂停后续队列，避免把全部作品连续标失败，也没有改下低清版本；请稍等后点击继续任务${details ? `。本次原因：${details}` : ""}。`;
    }
    if (
      text.includes("Douyin Live Photo author-feed quality source could not be verified") ||
      text.includes("Douyin Live Photo default original-quality source could not be verified")
    ) {
      const details = localizedProbeDetails(text);
      const advice = text.includes("media duration did not match the requested Douyin item")
        ? "请更新程序后从原链接重新解析该作品并重试；若仍不符，请反馈失败作品链接及上述时长。不需要打开 Chrome 验证。"
        : "";
      return `抖音 Live Photo 的作者直连或原始档没有通过完整性校验，程序没有下载可能降级的动态图${details ? `。本次原因：${details}` : ""}。${advice}`;
    }
    if (text.includes("Douyin Live Photo highest quality could not be verified")) {
      const details = localizedProbeDetails(text);
      return `旧版策略中至少一个 Live Photo 派生档位未能验证，静态原图可能已经保存；更新并继续任务后会改用作者直连和原始档校验${details ? `。原失败原因：${details}` : ""}。`;
    }
    if (
      text.includes("Xiaohongshu profile discovery temporarily returned a blank browser response") ||
      text.includes("Xiaohongshu profile discovery was temporarily rate-limited") ||
      text.includes("Xiaohongshu profile discovery temporarily timed out or was rate-limited") ||
      text.includes("Xiaohongshu temporarily rate-limited the note request") ||
      text.includes("Xiaohongshu returned no note data for the saved access token")
    ) {
      return "小红书本次遇到临时限流、超时或作品访问令牌失效。请稍等后直接重试；主页任务会重新解析令牌，没有明确验证码或登录页面时不需要打开 Chrome。";
    }
    if (text.includes("No current authenticated Xiaohongshu session was found in the selected Chrome profile")) {
      return "没有在 Chrome 中找到有效的小红书登录会话。请用任意普通 Chrome Profile 登录小红书后重试；这是 Chrome 登录状态问题，不是验证码，程序不会改用未登录请求。";
    }
    if (text.includes("The Xiaohongshu task has an unsupported cookie-browser setting")) {
      return "这个旧任务的浏览器 Cookie 设置无效。请在设置中启用 Chrome Cookie，或明确关闭浏览器 Cookie 后重新创建任务；程序不会偷偷改用其他浏览器身份。";
    }
    if (text.includes("Xiaohongshu did not accept the selected Chrome profile's login session")) {
      return `小红书没有接受该任务绑定的 Chrome Profile 登录状态。请在同一个 Profile 打开${verificationTarget(job)}并重新登录或刷新后再重试；这不是验证码，除非 Chrome 页面确实显示验证码。`;
    }
    if (text.includes("Xiaohongshu displayed an explicit verification challenge")) {
      return `小红书页面已明确显示验证码。请在该任务绑定的 Chrome Profile 打开${verificationTarget(job)}完成验证后再重试。`;
    }
    if (text.includes("Xiaohongshu identified this work as a video but returned no trusted video stream")) {
      return "小红书明确把这个作品标记为视频，但本次没有返回可信的视频流。程序没有把封面图片冒充视频保存；请从原作品链接重试。";
    }
    if (text.includes("This Xiaohongshu profile item was completed by an older version without verified media-type metadata")) {
      return "这个小红书作品曾被旧版在未验证媒体类型时标记为完成。已有文件会保留；请点击继续任务，程序会从原主页重新识别它是图片还是视频，并补下真实视频。";
    }
    if (text.includes("Xiaohongshu stopped returning new notes before the profile reported completion") || text.includes("Xiaohongshu reached the discovery safety limit before confirming the end of the profile")) {
      return "小红书主页在确认列表结束前停止返回新作品。当前结果可能不完整，请稍后点击继续发现；已有成功记录不会重复下载。";
    }
    if (text.includes("Xiaohongshu profile discovery redirected outside the trusted Xiaohongshu origin")) {
      return "小红书主页意外跳转到了非小红书地址，程序已停止处理。请检查原链接后重试，不要在该跳转页面输入账号或验证码。";
    }
    if (text.includes("Xiaohongshu item identity or profile membership could not be verified")) {
      return "小红书任务中的作品身份或主页归属无法验证，疑似串号内容已在下载前拦截。请点击重试，程序会从你最初粘贴的链接重新解析；这不是验证码，不需要打开 Chrome。";
    }
    if (text.includes("Xiaohongshu discovery temporarily returned no trusted notes")) {
      return "小红书本次没有返回可信作品，程序没有创建占位队列。请稍等后从原链接直接重试；没有明确验证码或登录页面时不需要打开 Chrome。";
    }
    if (text.includes("Xiaohongshu short-link retry could not verify the original resolved target")) {
      return "这个小红书短链接的旧任务没有保存可验证的原目标。为防止短链变化后串号，程序已停止下载；请用最初的短链接创建新任务。";
    }
    if (text.includes("Xiaohongshu short-link retry resolved to a different note or profile")) {
      return "小红书短链接这次跳到了与首次解析不同的作品或主页，程序已在下载前拦截。请检查原链接，不要重试这个已变化的目标。";
    }
    if (text.includes("Xiaohongshu discovery returned an untrusted, duplicate, or cross-wired note URL")) {
      return "小红书解析结果包含不可信、重复或串号的作品地址，程序已在下载前全部拦截。请稍后从原链接重新解析。";
    }
    if (text.includes("Xiaohongshu returned a different note from the requested item") || text.includes("Xiaohongshu profile note belongs to a different or unverifiable author")) {
      return "小红书返回了其他作品或其他作者的数据，程序已在请求媒体文件前拦截，没有下载串号内容。请从原链接直接重试，不需要打开 Chrome 验证。";
    }
    if (text.includes("Xiaohongshu note request redirected outside the trusted note origin") || text.includes("Untrusted Xiaohongshu media URL was blocked") || text.includes("Xiaohongshu media request redirected to an untrusted URL")) {
      return "小红书页面或媒体地址跳转到了非可信站点，程序已在读取内容前拦截。请检查原链接后重试，不要在异常页面输入账号或验证码。";
    }
    if (text.includes("Xiaohongshu redirected to an explicit login or verification page") || text.includes("Xiaohongshu requires verification") || text.includes("Xiaohongshu requires a CAPTCHA or login") || text.includes("Xiaohongshu interrupted discovery with a verification challenge")) {
      return `小红书已明确显示登录或验证码。请在 Chrome 打开${verificationTarget(job)}完成验证后重试。`;
    }
    if (text.includes("Highest-available image dimensions could not be verified") || text.includes("image below its declared")) {
      return "小红书图片的实际分辨率低于作品声明值或无法验证。程序已拒绝保存低清占位图，并会尝试下一条原图地址；全部候选失败时请稍后重试。";
    }
    if (text.includes("FFprobe was found but could not be started")) {
      return "已找到 FFprobe，但程序无法启动它。请重新安装包含 FFprobe 的 FFmpeg，然后完全停止并重启程序。";
    }
    if (text.includes("FFprobe was not found")) {
      return "未找到 FFprobe，无法验证抖音或小红书视频的最高画质。请安装包含 FFprobe 的 FFmpeg，将其 bin 目录加入 PATH，然后完全停止并重启程序；macOS Homebrew 可运行 brew install ffmpeg。";
    }
    if (text.includes("Douyin media request redirected to an untrusted URL")) {
      return "这是旧版本保存的抖音媒体跳转错误；旧版没有记录实际跳转主机，因此不能据此安全放行。请确认页面底部是当前版本，然后从原主页或原视频链接重新创建任务；新版会重新解析，并在再次拦截时显示具体安全校验原因，只在安全时显示 CDN 域名。";
    }
    if (text.includes("This task contains a Douyin media redirect failure recorded by an older version")) {
      return "这是旧版本保存的抖音媒体跳转错误，旧版没有记录实际 CDN 主机。已下载文件均已保留；点击重试会从原链接重新解析，新版若再次拦截会显示具体安全校验原因，并在安全时显示主机名，也不需要打开 Chrome 验证。";
    }
    if (text.includes("This saved Douyin short-link task contains a media redirect failure from an older version")) {
      return "这是旧版抖音短链任务保存的跳转错误，但旧版没有保存短链当时解析到的目标，无法安全自动重试。已下载文件均已保留；请把原短链重新粘贴并新建任务，新版会先绑定目标再下载。";
    }
    if (text.includes("Media server returned a video below its declared") || text.includes("Downloaded video bitrate was below its verified highest-quality media endpoint") || text.includes("Downloaded video size did not match its verified highest-quality media endpoint") || text.includes("Downloaded video duration did not match its verified media metadata") || text.includes("verified highest-quality video has no duration fingerprint") || text.includes("verified highest-quality video has no bitrate or complete size fingerprint") || text.includes("Media response changed after quality verification")) {
      return "最终下载文件与已验证的最高画质不一致，可能被替换成低清流。程序已删除临时文件且不会覆盖已有文件；请稍后重试。";
    }
    if (!text.includes("Douyin media was discovered") && !text.includes("Douyin profile media was discovered")) return text;

    const renditionMismatch = text.includes("verified media was below the author-feed");
    const durationMismatch = text.includes("media duration did not match the requested Douyin item");
    text = localizedDurationMismatch(localizedRenditionMismatch(text))
      .replace(
        /Douyin (?:profile )?media was discovered, but its highest quality could not be verified\.[\s\S]*?Probe details:\s*/,
        "已发现该抖音作品，但无法验证其最高画质；为避免下错低清版本，本次没有下载。探测详情："
      )
      .replaceAll("media request or FFprobe timed out", "媒体请求或 FFprobe 探测超时")
      .replaceAll("secure media connection failed", "安全媒体连接失败")
      .replaceAll("media endpoint network request failed", "媒体地址网络请求失败")
      .replaceAll("media endpoint returned an HTTP error", "媒体地址返回 HTTP 错误")
      .replaceAll("media endpoint did not return video data", "媒体地址未返回视频数据")
      .replaceAll("media endpoint did not return an MP4 file", "媒体地址未返回有效 MP4 文件")
      .replaceAll("FFprobe could not parse the media stream", "FFprobe 无法解析媒体流")
      .replaceAll("FFprobe returned no video dimensions", "FFprobe 未返回视频分辨率")
      .replaceAll("FFprobe returned no media duration", "FFprobe 未返回媒体时长")
      .replaceAll("FFprobe returned no bitrate or complete media size", "FFprobe 未返回码率或完整媒体大小")
      .replaceAll("media duration did not match the requested Douyin item", "媒体时长与目标抖音作品不匹配")
      .replaceAll("no verified media identity was available", "没有可验证的媒体身份")
      .replaceAll("no playable candidate was returned", "未返回可播放的候选媒体")
      .replaceAll("media metadata could not be parsed", "无法解析媒体元数据")
      .replaceAll("best verified candidate was", "已验证的最高候选为")
      .replaceAll("below the discovered minimum", "低于解析阶段确认的最低画质")
      .replaceAll("highest candidate uses unsupported video codec", "最高画质使用当前不支持的视频编码")
      .replaceAll("default", "原始档");
    if (durationMismatch) return `${text}。请更新程序后从原链接重新解析该作品并重试；若仍不符，请反馈失败作品链接及上述时长，程序不会跳过校验保存可能串号的文件。不需要打开 Chrome 验证。`;
    return renditionMismatch
      ? `${text}。请更新程序后重试，让程序刷新同一作品的媒体地址并检查备用源；若仍失败，请反馈以上声明尺寸与实际尺寸。不需要打开 Chrome 验证。`
      : text;
  }

  function authRequired(job) {
    const status = canonicalStatus(job);
    if (status === "needs_auth") {
      const code = primaryJobIssueCode(job);
      return !code || code === "unknown" || code === "login_required" || code === "verification_required";
    }
    if (status !== "unknown") return false;
    return getItems(job).some((item) => canonicalStatus(item) === "needs_auth");
  }

  function extractJob(payload) {
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
    if (payload.job && typeof payload.job === "object") return payload.job;
    if (payload.data && typeof payload.data === "object" && !Array.isArray(payload.data)) {
      const nested = extractJob(payload.data);
      if (nested) return nested;
    }
    return getEntityId(payload) !== undefined ? payload : null;
  }

  function extractJobs(payload) {
    if (Array.isArray(payload)) return payload;
    if (Array.isArray(payload?.jobs)) return payload.jobs;
    if (Array.isArray(payload?.data)) return payload.data;
    if (Array.isArray(payload?.data?.jobs)) return payload.data.jobs;
    const single = extractJob(payload);
    return single ? [single] : [];
  }

  function upsertJob(job) {
    const idValue = getEntityId(job);
    if (idValue === undefined || idValue === null) return null;
    const id = String(idValue);
    const existing = state.jobs.get(id) || {};
    const incomingRevision = asNumber(job?.revision);
    const existingRevision = asNumber(existing?.revision);
    if (incomingRevision !== null && existingRevision !== null && incomingRevision < existingRevision) {
      return existing;
    }
    const merged = { ...existing, ...job, _id: id };

    const itemFields = ["items", "media", "downloads", "results", "entries"];
    const incomingHasItems = itemFields.some((key) => Object.hasOwn(job, key) && job[key] !== undefined);
    if (!incomingHasItems) {
      itemFields.forEach((key) => {
        if (existing[key] !== undefined) merged[key] = existing[key];
      });
    }
    state.jobs.set(id, merged);
    return merged;
  }

  async function api(path, options = {}) {
    const method = String(options.method || "GET").toUpperCase();
    if (!["GET", "HEAD"].includes(method) && (!state.backendReady || state.versionBlocked || !state.initialized)) {
      throw new Error(state.versionBlocked
        ? "页面与后台版本不一致，请重新启动程序并刷新页面"
        : !state.backendReady
          ? "暂时无法连接后台，正在自动重连；连接恢复后再试，已下载文件会保留"
          : "正在读取后台设置，请稍后再试；若持续失败，请确认后台仍在运行");
    }
    const requestOptions = {
      ...options,
      headers: {
        Accept: "application/json",
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...(options.headers || {})
      }
    };
    const response = await fetch(path, requestOptions);
    if (!response.ok) {
      let detail = "";
      try {
        const data = await response.json();
        detail = asText(firstDefined(data?.detail, data?.message, data?.error));
      } catch {
        detail = await response.text().catch(() => "");
      }
      throw new Error(detail || `请求失败（${response.status}）`);
    }
    if (response.status === 204) return null;
    const contentType = response.headers.get("content-type") || "";
    return contentType.includes("application/json") ? response.json() : null;
  }

  function showToast(message, type = "success", durationMs = 4200) {
    const toast = document.createElement("div");
    toast.className = `toast ${type === "error" ? "error" : ""}`;
    toast.setAttribute("role", type === "error" ? "alert" : "status");
    toast.textContent = message;
    elements.toastRegion.append(toast);
    window.setTimeout(() => {
      toast.classList.add("leaving");
      window.setTimeout(() => toast.remove(), 220);
    }, durationMs);
  }

  function issueToastMessage(job, lead = "") {
    const code = primaryJobIssueCode(job) || "unknown";
    const raw = primaryJobIssueMessage(job);
    const author = getAuthor(job);
    const prefix = lead ? `${author}：${lead}\n` : `${author}\n`;
    return `${prefix}${issueResolutionText(code, raw)}`;
  }

  function showIssueToast(job, lead = "") {
    showToast(issueToastMessage(job, lead), "error", 12000);
  }

  function warningToastMessage(job, warning = warningPresentation(job)) {
    if (!warning) return "";
    return `${getAuthor(job)}\n${warning.message}`;
  }

  function setButtonLoading(button, loading, loadingText) {
    if (loading) {
      button.dataset.originalText = button.querySelector("span")?.textContent || button.textContent;
      if (button.querySelector("span") && loadingText) button.querySelector("span").textContent = loadingText;
      button.classList.add("loading");
      button.disabled = true;
    } else {
      if (button.querySelector("span") && button.dataset.originalText) {
        button.querySelector("span").textContent = button.dataset.originalText;
      }
      button.classList.remove("loading");
      button.disabled = false;
    }
  }

  function setConnection(mode, text) {
    if (!elements.connectionStatus) return;
    elements.connectionStatus.classList.remove("connected", "disconnected");
    if (mode) elements.connectionStatus.classList.add(mode);
    const label = elements.connectionStatus.querySelector("span:last-child");
    if (label) label.textContent = text;
  }

  function metaContent(name) {
    return document.querySelector(`meta[name="${name}"]`)?.content || "";
  }

  async function verifyBackendBuild() {
    if (state.versionBlocked) return false;
    const expectedAppId = metaContent("app-id");
    const expectedVersion = metaContent("app-version");
    const expectedBuild = metaContent("app-build");
    let health = null;
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 5000);
    try {
      const response = await fetch("/api/health", {
        cache: "no-store",
        signal: controller.signal,
        headers: { Accept: "application/json", "Cache-Control": "no-cache" }
      });
      if (response.ok) health = await response.json();
      else if (response.status === 404) health = {};
    } catch {
      health = null;
    } finally {
      window.clearTimeout(timeout);
    }
    if (health === null) {
      state.backendReady = false;
      setConnection("disconnected", "后台暂时不可用，正在自动重连");
      return false;
    }

    const rendered = expectedBuild && !expectedBuild.startsWith("__");
    const compatible = Boolean(
      rendered
      && health?.status === "ok"
      && health?.app_id === expectedAppId
      && health?.version === expectedVersion
      && health?.build_id === expectedBuild
      && health?.source_build_id === expectedBuild
      && health?.restart_required === false
    );
    if (compatible) {
      const recovered = !state.backendReady && state.initialized;
      state.backendReady = true;
      elements.buildInfo.textContent = `v${health.version} · ${health.build_id}`;
      if (recovered) {
        setConnection("connected", state.eventSource?.readyState === 1 ? "实时连接" : "连接已恢复");
      }
      return true;
    }

    state.backendReady = false;
    state.versionBlocked = true;
    document.body.classList.add("version-blocked");
    if (state.eventSource) {
      state.eventSource.close();
      state.eventSource = null;
    }
    document.querySelectorAll("button, input").forEach((control) => {
      control.disabled = true;
    });
    if (!elements.versionAlert) {
      const alert = document.createElement("section");
      const heading = document.createElement("strong");
      const detail = document.createElement("p");
      alert.id = "version-alert";
      alert.className = "version-alert";
      alert.setAttribute("role", "alert");
      heading.textContent = "当前页面连接的还是旧后台，已停止创建和重试任务";
      detail.id = "version-alert-detail";
      alert.append(heading, detail);
      (document.querySelector("#main-content") || document.body).prepend(alert);
      elements.versionAlert = alert;
      elements.versionAlertDetail = detail;
    }
    elements.versionAlert.hidden = false;
    const mismatchDetail = health?.app_id
      ? `页面版本 ${expectedVersion || "未知"} / ${expectedBuild || "未知"}，后台加载版本 ${health.version || "未知"} / ${health.build_id || "未知"}，磁盘源码版本 ${health.source_build_id || "未知"}。请关闭旧 Terminal 后重新运行 start.command，再按 Command+Shift+R 强制刷新。`
      : "旧 Python 后台没有返回版本标识。请关闭之前启动下载器的 Terminal，重新运行 start.command，再按 Command+Shift+R 强制刷新。";
    if (elements.versionAlertDetail) {
      elements.versionAlertDetail.textContent = mismatchDetail;
    }
    if (elements.buildInfo) elements.buildInfo.textContent = "版本冲突";
    setConnection("disconnected", "后台未重启");
    return false;
  }

  function progressDescription(job, counts) {
    if (job?.cancel_requested) return "正在等待当前网络请求或合并步骤安全停止…";
    const status = canonicalStatus(job);
    const processed = counts.total !== null && counts.total > 0
      ? `已处理 ${Math.min(counts.total, counts.success + counts.failed)} / ${counts.total} 个作品`
      : "";
    if (rawStatus(job) === "interrupted" || rawStatus(job) === "partial" || status === "failed") {
      if (job?.retryable === false) {
        return processed ? `任务已停止；${processed}` : "任务已停止，请查看失败原因";
      }
      return processed ? `已暂停，等待继续；${processed}` : "任务暂时失败，可查看提示后重试";
    }
    if (status === "needs_auth") return "等待验证，完成后即可继续";
    if (status === "cancelled") return "任务已取消，已下载文件会保留";
    const discoveryActivity = discoveryActivityDescription(job);
    if (discoveryActivity) return discoveryActivity;
    const current = firstDefined(job?.current_item, job?.current_title, job?.message, job?.detail);
    if (typeof current === "string" && current.trim()) return current;
    const activeItem = getActiveItem(job);
    if (activeItem) {
      const phaseMessage = activeItem?.progress?.filename;
      if (typeof phaseMessage === "string") {
        const qualityWaitMatch = phaseMessage.match(
          /^Waiting for another Douyin quality check \((\d+)s\)$/
        );
        if (qualityWaitMatch) {
          return `正在等待前一个抖音画质校验完成（已等待 ${qualityWaitMatch[1]} 秒）`;
        }
        const originalReadMatch = phaseMessage.match(
          /^Reading the Douyin original file to verify quality \(([^)]+)\)$/
        );
        if (originalReadMatch) {
          const ratio = originalReadMatch[1] === "default" ? "原始档" : originalReadMatch[1];
          return `正在读取抖音原文件以完成画质校验（${ratio}）`;
        }
        if (phaseMessage.startsWith("Starting Douyin original media transfer")) {
          return "正在开始传输抖音原文件";
        }
        const retryMatch = phaseMessage.match(
          /^Retrying Douyin quality (\S+) after a temporary network error \((\d+\/\d+)\)$/
        );
        if (retryMatch) {
          const ratio = retryMatch[1] === "default" ? "原始档" : retryMatch[1];
          return `抖音 ${ratio} 画质探测遇到临时网络错误，正在重试（${retryMatch[2]}）`;
        }
        const transferRetryMatch = phaseMessage.match(
          /^Retrying Douyin media transfer after a temporary network error \((\d+\/\d+)\)$/
        );
        if (transferRetryMatch) {
          return `抖音原文件传输遇到临时网络错误，正在重试（${transferRetryMatch[1]}）`;
        }
        const mediaCandidateMatch = phaseMessage.match(
          /^Checking Douyin media candidate (\d+)\/(\d+) \(([^)]+)\)$/
        );
        if (mediaCandidateMatch) {
          const ratio = mediaCandidateMatch[3] === "default"
            ? "原始档"
            : mediaCandidateMatch[3];
          return `正在检测抖音媒体候选 ${mediaCandidateMatch[1]}/${mediaCandidateMatch[2]}（${ratio}）`;
        }
        const qualityCandidateReadMatch = phaseMessage.match(
          /^Reading Douyin quality candidate (\d+)\/(\d+) \(([^)]+)\)$/
        );
        if (qualityCandidateReadMatch) {
          const ratio = qualityCandidateReadMatch[3] === "default"
            ? "原始档"
            : qualityCandidateReadMatch[3];
          return `正在读取抖音画质候选 ${qualityCandidateReadMatch[1]}/${qualityCandidateReadMatch[2]}（${ratio}）`;
        }
        const phasePrefixes = [
          ["Checking Douyin Live Photo quality", "正在检测抖音 Live Photo 最高画质"],
          ["Checking Douyin author-feed quality", "正在检测抖音作者直连画质"],
          ["Checking Douyin direct quality", "正在检测抖音直连候选画质"],
          ["Checking Douyin quality", "正在检测抖音最高画质"]
        ];
        const phasePrefix = phasePrefixes.find(([prefix]) => phaseMessage.startsWith(prefix));
        if (phasePrefix) {
          return phaseMessage
            .replace(phasePrefix[0], phasePrefix[1])
            .replace(": default", ": 原始档")
            .replaceAll("x", "×");
        }
        if (isDouyinProbeProgress(activeItem)) {
          return localizeDiscoveryActivity(phaseMessage, job);
        }
      }
      const index = Math.max(0, getItems(job).indexOf(activeItem));
      return `正在处理：${itemTitle(activeItem, index)}`;
    }
    if (counts.total !== null && counts.total > 0) {
      return processed;
    }
    if (status === "completed") return "所有作品处理完成";
    return "正在解析主页内容…";
  }

  function localizeDiscoveryActivity(value, job) {
    const message = asText(value).trim();
    const platform = platformMeta(job).label;
    const target = isProfileJob(job) ? `${platform}主页` : `${platform}作品`;
    if (!message || message === "Starting media discovery") {
      return `正在准备解析${target}`;
    }

    const browserStart = message.match(
      /^Starting bounded Douyin browser profile fallback \(reason: ([a-z0-9-]+); (\d+)s remaining\)$/i
    );
    if (browserStart) {
      const reasonLabels = {
        "direct-browser-mode": "直接浏览器模式",
        "authentication-confirmation": "正在确认是否确实需要验证",
        "signed-integrity": "签名响应未通过完整性校验"
      };
      const reason = reasonLabels[browserStart[1]] || "签名接口暂不可用";
      return `正在切换到有时限的抖音主页浏览器解析（${reason}，剩余 ${browserStart[2]} 秒）`;
    }
    if (message === "Launching Douyin browser fallback") {
      return "正在启动抖音主页浏览器解析";
    }
    if (message === "Opening the Douyin profile in the bounded browser fallback") {
      return "正在浏览器中打开原抖音主页";
    }
    const browserScan = message.match(
      /^Scanning Douyin browser fallback round (\d+)\/(\d+) \((\d+) verified item\(s\); (\d+)s remaining\)$/
    );
    if (browserScan) {
      return `正在滚动读取抖音主页（第 ${browserScan[1]}/${browserScan[2]} 轮，已验证 ${browserScan[3]} 个作品，剩余 ${browserScan[4]} 秒）`;
    }
    const browserAdded = message.match(
      /^Douyin browser fallback added (\d+) verified item\(s\) \((\d+) total; (\d+)s remaining\)$/
    );
    if (browserAdded) {
      return `浏览器解析新增 ${browserAdded[1]} 个抖音作品（共 ${browserAdded[2]} 个，剩余 ${browserAdded[3]} 秒）`;
    }

    const profileRetry = message.match(
      /^Retrying Douyin signed profile page (\d+)\/(\d+) request (\d+)\/(\d+)(?: \(reason: ([a-z0-9-]+)\))?$/i
    );
    if (profileRetry) {
      const reason = douyinSignedReasonLabel(profileRetry[5]);
      return `抖音主页第 ${profileRetry[1]} 页请求暂时失败${reason ? `（${reason}）` : ""}，正在重试（${profileRetry[3]}/${profileRetry[4]}）`;
    }
    const profilePage = message.match(
      /^Fetching Douyin signed profile page (\d+)\/(\d+)$/
    );
    if (profilePage) return `正在读取抖音主页第 ${profilePage[1]} 页作品`;
    const verifiedPage = message.match(
      /^Verified Douyin signed profile page (\d+) \((\d+) items\)$/
    );
    if (verifiedPage) {
      return `已验证抖音主页第 ${verifiedPage[1]} 页（${verifiedPage[2]} 个作品）`;
    }
    const profileSession = message.match(
      /^Preparing Douyin signed profile session (\d+)\/(\d+)$/
    );
    if (profileSession) {
      return `正在准备抖音主页签名会话（${profileSession[1]}/${profileSession[2]}）`;
    }
    const profileResume = message.match(
      /^Resuming Douyin signed profile at page (\d+)\/(\d+) \((\d+) verified items\)$/
    );
    if (profileResume) {
      return `正在从抖音主页第 ${profileResume[1]} 页续跑（已验证 ${profileResume[3]} 个作品）`;
    }
    const detailRetry = message.match(
      /^Retrying Douyin signed detail request (\d+)\/(\d+)(?: \(reason: ([a-z0-9-]+)\))?$/i
    );
    if (detailRetry) {
      const reason = douyinSignedReasonLabel(detailRetry[3]);
      return `抖音作品详情请求暂时失败${reason ? `（${reason}）` : ""}，正在重试（${detailRetry[1]}/${detailRetry[2]}）`;
    }
    const detailSession = message.match(
      /^Preparing Douyin signed detail session (\d+)\/(\d+)$/
    );
    if (detailSession) {
      return `正在准备抖音作品签名会话（${detailSession[1]}/${detailSession[2]}）`;
    }
    if (message === "Opening the original Douyin item page") {
      return "正在打开原抖音作品页，读取完整图文/Live Photo 详情";
    }
    if (message === "Fetching Douyin signed detail") return "正在读取抖音作品详情";
    if (message === "Fetching Douyin signing HTML") return "正在读取抖音签名页面";
    if (message === "Waiting for the Douyin signed request slot") return "正在等待抖音签名请求通道";
    if (message === "Loading Douyin Chrome cookies") return "正在读取 Chrome 中的抖音登录状态";
    if (message === "Starting the Douyin signing browser") return "正在启动抖音签名浏览器";
    if (message === "Starting the Douyin signing session") return "正在建立抖音签名会话";
    const freshProfileSession = message.match(
      /^Retrying Douyin signed profile with a fresh signing session(?: \(reason: ([a-z0-9-]+)\))?$/i
    );
    if (freshProfileSession) {
      const reason = douyinSignedReasonLabel(freshProfileSession[1]);
      return `抖音主页签名会话暂时失败${reason ? `（${reason}）` : ""}，正在重新建立`;
    }
    const freshDetailSession = message.match(
      /^Retrying Douyin signed detail with a fresh signing session(?: \(reason: ([a-z0-9-]+)\))?$/i
    );
    if (freshDetailSession) {
      const reason = douyinSignedReasonLabel(freshDetailSession[1]);
      return `抖音作品签名会话暂时失败${reason ? `（${reason}）` : ""}，正在重新建立`;
    }

    const retryCount = message.match(/\((\d+\s*\/\s*\d+)\)/)?.[1]?.replaceAll(" ", "");
    if (/retry|rate.?limit|temporary/i.test(message)) {
      const suffix = retryCount ? `（${retryCount}）` : "";
      return `${target}接口暂时繁忙，正在自动重试${suffix}`;
    }

    const page = message.match(/(?:page|batch)\s*(\d+)/i)?.[1];
    if (page) return `正在读取${target}第 ${page} 页内容`;
    if (/scroll/i.test(message)) return `正在滚动加载${target}`;
    if (/browser|playwright|chrome/i.test(message)) return `正在通过浏览器读取${target}`;
    if (/signed|signature|author.?feed|api/i.test(message)) return `正在读取${target}列表`;
    if (/collect|parse|discover|profile|item|media/i.test(message)) return `正在解析${target}`;
    return `正在解析${target}`;
  }

  function douyinSignedReasonLabel(value) {
    const labels = {
      "http-403": "HTTP 403 临时拒绝",
      "http-429": "HTTP 429 限流",
      "http-5xx": "抖音服务器 5xx 异常",
      "api-status-nonzero": "接口业务状态异常",
      "api-incomplete": "接口返回不完整",
      "api-missing-aweme-list": "接口没有返回作品列表",
      "api-unbound-empty-page": "空终页没有绑定当前作者",
      "api-incomplete-media": "接口返回的作品媒体信息不完整",
      "api-filtered-images-base": "简化接口过滤了图文/Live Photo 详情",
      "network-timeout": "网络超时",
      "network-error": "网络连接异常",
      "signer-timeout": "签名组件超时",
      "signed-rejected": "签名请求被临时拒绝",
      "no-progress-timeout": "没有拿到可验证的新结果"
    };
    return labels[asText(value).trim().toLowerCase()] || "";
  }

  function discoveryActivityDescription(job) {
    if (rawStatus(job) !== "discovering" || !job?.activity_message) return "";
    const phase = localizeDiscoveryActivity(job.activity_message, job);
    const startedAt = toDate(job.activity_started_at);
    if (!startedAt) return phase;
    const elapsedSeconds = Math.max(0, (Date.now() - startedAt.getTime()) / 1000);
    return `${phase}（已等待 ${formatDuration(elapsedSeconds)}）`;
  }

  function renderItems(job) {
    const items = getItems(job);
    const matchingItems = items
      .map((item, index) => ({ item, index }))
      .filter(({ item }) => {
        if (state.filter === "success") return canonicalStatus(item) === "completed";
        if (state.filter === "failed") return isFailed(item);
        return true;
      });

    elements.itemsList.replaceChildren();
    elements.itemsSummary.textContent = items.length ? `共 ${items.length} 个作品，可展开查看每个已保存文件` : "任务开始后会显示每个作品的状态";

    if (!matchingItems.length) {
      const empty = document.createElement("p");
      empty.className = "items-empty";
      empty.textContent = items.length ? "这个筛选条件下还没有作品" : "正在等待解析结果…";
      elements.itemsList.append(empty);
      return;
    }

    const fragment = document.createDocumentFragment();
    matchingItems.forEach(({ item, index }) => {
      const node = elements.itemTemplate.content.firstElementChild.cloneNode(true);
      const tone = statusTone(item);
      const percent = getProgress(item);
      const id = firstDefined(item?.id, item?.item_id, item?.media_id, item?.entry_id);
      node.classList.add(tone);
      node.querySelector(".item-title").textContent = itemTitle(item, index);
      node.querySelector(".item-meta").textContent = itemMetadata(item);
      const status = node.querySelector(".item-status");
      status.classList.add(tone);
      status.textContent = statusLabel(item, job);
      node.querySelector(".item-progress span").style.width = `${percent}%`;
      node.querySelector(".item-error").textContent = itemError(item, job);

      const outputPaths = itemOutputPaths(item);
      const files = node.querySelector(".item-files");
      if (outputPaths.length) {
        files.hidden = false;
        files.querySelector("summary").textContent = `已保存 ${outputPaths.length} 个文件`;
        const fileList = files.querySelector("ul");
        outputPaths.forEach((path) => {
          const entry = document.createElement("li");
          entry.textContent = path.split(/[\\/]/).pop() || path;
          entry.title = path;
          fileList.append(entry);
        });
      }

      const retryButton = node.querySelector(".item-retry-button");
      if (!isRunning(job) && isRetryableItem(item) && id !== undefined && id !== null) {
        retryButton.hidden = false;
        retryButton.setAttribute("aria-label", `重试：${itemTitle(item, index)}`);
        retryButton.addEventListener("click", () => retryJob(job._id, id, retryButton));
      }
      fragment.append(node);
    });
    elements.itemsList.append(fragment);
  }

  function renderHistory() {
    const jobs = [...state.jobs.values()].sort((a, b) => {
      const right = toDate(getCreatedAt(b))?.getTime() || 0;
      const left = toDate(getCreatedAt(a))?.getTime() || 0;
      return right - left;
    });
    elements.historyList.replaceChildren();

    if (!jobs.length) {
      const empty = document.createElement("p");
      empty.className = "history-empty";
      empty.textContent = "还没有下载任务";
      elements.historyList.append(empty);
      return;
    }

    jobs.forEach((job) => {
      const platform = platformMeta(job);
      const button = document.createElement("button");
      button.type = "button";
      button.className = `history-item ${job._id === state.selectedJobId ? "selected" : ""}`;
      button.setAttribute("aria-label", `查看 ${getAuthor(job)} 的任务，${statusLabel(job)}`);

      const platformElement = document.createElement("span");
      platformElement.className = "history-platform";
      platformElement.textContent = platform.glyph;

      const copy = document.createElement("span");
      copy.className = "history-copy";
      const title = document.createElement("strong");
      title.textContent = getAuthor(job);
      const detail = document.createElement("small");
      detail.textContent = `${platform.label} · ${formatRelativeTime(getCreatedAt(job))}`;
      copy.append(title, detail);

      const indicator = document.createElement("span");
      indicator.className = `history-state ${statusTone(job)}`;
      indicator.setAttribute("aria-hidden", "true");
      button.append(platformElement, copy, indicator);
      button.addEventListener("click", () => selectJob(job._id));
      elements.historyList.append(button);
    });
  }

  function renderSelectedJob() {
    const job = state.selectedJobId ? state.jobs.get(state.selectedJobId) : null;
    elements.emptyState.hidden = Boolean(job);
    elements.jobView.hidden = !job;
    renderHistory();
    if (!job) return;

    const platform = platformMeta(job);
    const counts = getCounts(job);
    const progress = getProgress(job, true);
    const tone = statusTone(job);
    const needsAuth = authRequired(job);

    elements.platformMark.className = `platform-mark ${platform.key}`;
    elements.platformMark.textContent = platform.glyph;
    elements.jobPlatform.textContent = platform.label;
    elements.jobHeading.textContent = getAuthor(job);
    elements.jobHeading.title = getAuthor(job);
    elements.jobTime.textContent = formatRelativeTime(getCreatedAt(job));
    elements.jobTime.dateTime = toDate(getCreatedAt(job))?.toISOString() || "";

    elements.jobStatus.className = `status-pill ${tone}`;
    elements.jobStatus.textContent = statusLabel(job);
    elements.progressPercent.textContent = `${Math.round(progress)}%`;
    elements.progressBar.style.width = `${progress}%`;
    elements.progressTrack.setAttribute("aria-valuenow", String(Math.round(progress)));
    elements.progressLabel.textContent = progressDescription(job, counts);
    elements.jobSpeed.textContent = formatSpeed(job);
    elements.jobEta.textContent = formatEta(job);
    elements.successCount.textContent = String(counts.success);
    elements.failureCount.textContent = String(counts.failed);
    elements.activeCount.textContent = String(counts.active);
    if (elements.activeCountLabel) {
      elements.activeCountLabel.textContent = activeCountLabel(job, counts.active);
    }
    elements.totalCount.textContent = counts.total === null ? "—" : String(counts.total);

    elements.authAlert.hidden = !needsAuth;
    elements.authRetryButton.hidden = job?.retryable === false;
    if (needsAuth) {
      const authCode = primaryJobIssueCode(job);
      if (elements.authTitle) {
        elements.authTitle.textContent = issueTitles[authCode] || "网站要求登录或验证";
      }
      elements.authMessage.textContent = localizedPrimaryJobIssueMessage(job);
      const loginRequired = authCode === "login_required";
      const xiaohongshuLogin = loginRequired || isXiaohongshuLoginSessionIssue(job);
      elements.authOpenButton.textContent = isXiaohongshuVerificationItem(job)
        ? xiaohongshuLogin
          ? "打开 Chrome 登录作品"
          : "打开 Chrome 验证作品"
        : isProfileJob(job)
          ? xiaohongshuLogin
            ? "打开 Chrome 登录主页"
            : "打开 Chrome 验证主页"
          : platform.key === "xiaohongshu"
            ? loginRequired ? "打开 Chrome 登录作品" : "打开 Chrome 验证作品"
            : loginRequired ? "打开 Chrome 登录视频" : "打开 Chrome 验证视频";
    }
    const items = getItems(job);
    const discoveryIncomplete = job?.discovery_complete === false;
    const nonAuthNeedsAuthState = canonicalStatus(job) === "needs_auth" && !needsAuth;
    const warning = warningPresentation(job);
    elements.warningAlert.hidden = !warning;
    if (warning) {
      elements.warningAlert.setAttribute("role", warning.isAlert ? "alert" : "status");
      elements.warningTitle.textContent = warning.title;
      elements.warningMessage.textContent = warning.message;
    }
    const failedItemHasCurrentIssue = items.some((item) => (
      isFailed(item)
      && issueCode(item)
      && issueCode(item) === primaryJobIssueCode(job)
    ));
    const continueDiscovery = discoveryIncomplete && !failedItemHasCurrentIssue;
    const hasUnprocessedItems = counts.total !== null && counts.success + counts.failed < counts.total;
    const resumable = ["cancelled", "interrupted"].includes(rawStatus(job)) || discoveryIncomplete || (rawStatus(job) === "partial" && hasUnprocessedItems);
    const hasRetryableItems = items.some(isRetryableItem);
    const canRetryDiscovery = items.length === 0 && (needsAuth || nonAuthNeedsAuthState || canonicalStatus(job) === "failed" || resumable);
    elements.retryAllButton.hidden = job?.retryable === false || isRunning(job) || (!hasRetryableItems && !resumable && !canRetryDiscovery);
    elements.retryAllLabel.textContent = continueDiscovery
      ? "继续发现"
      : resumable
        ? "继续任务"
        : canRetryDiscovery
          ? "重试任务"
        : "重试全部失败项";
    elements.cancelButton.hidden = !isRunning(job) && !needsAuth;
    elements.cancelButton.disabled = Boolean(job?.cancel_requested);
    elements.cancelButton.title = job?.cancel_requested ? "当前步骤结束后会停止" : "取消任务";
    renderItems(job);
  }

  async function fetchJob(id, silent = true) {
    if (!id) return null;
    try {
      const payload = await api(`/api/jobs/${encodeURIComponent(id)}`);
      const job = extractJob(payload);
      if (job) upsertJob(job);
      renderSelectedJob();
      return job;
    } catch (error) {
      if (!silent) showToast(`读取任务失败：${error.message}`, "error");
      return null;
    }
  }

  async function fetchJobs(silent = true) {
    elements.refreshButton.classList.add("loading");
    elements.refreshButton.disabled = true;
    try {
      const payload = await api("/api/jobs");
      const jobs = extractJobs(payload);
      jobs.forEach(upsertJob);
      if (!state.selectedJobId && jobs.length) {
        const running = jobs.find((job) => isRunning(job) || authRequired(job));
        const first = running || jobs[0];
        state.selectedJobId = String(getEntityId(first));
      }
      renderSelectedJob();
      if (state.selectedJobId) await fetchJob(state.selectedJobId, true);
    } catch (error) {
      if (!silent) showToast(`刷新任务失败：${error.message}`, "error");
    } finally {
      elements.refreshButton.classList.remove("loading");
      elements.refreshButton.disabled = false;
    }
  }

  async function selectJob(id) {
    state.selectedJobId = String(id);
    renderSelectedJob();
    await fetchJob(state.selectedJobId, false);
  }

  function scheduleDetailRefresh(job) {
    const id = String(getEntityId(job));
    if (id !== state.selectedJobId) return;
    window.clearTimeout(state.refreshTimer);
    state.refreshTimer = window.setTimeout(() => fetchJob(id, true), 180);
  }

  function handleEvent(event) {
    if (!event?.data) return;
    let payload;
    try {
      payload = JSON.parse(event.data);
    } catch {
      return;
    }
    const incoming = extractJob(payload);
    if (!incoming) return;

    const id = String(getEntityId(incoming));
    const previous = state.jobs.get(id);
    const previousStatus = previous ? rawStatus(previous) : null;
    const job = upsertJob(incoming);
    if (!job) return;
    if (!state.selectedJobId) state.selectedJobId = id;
    renderSelectedJob();

    const nextRawStatus = rawStatus(job);
    const nextStatus = canonicalStatus(job);
    if (previousStatus && previousStatus !== nextRawStatus) {
      if (nextRawStatus === "partial") showIssueToast(job, issueTitleForJob(job));
      else if (nextStatus === "completed") {
        const completedWarning = warningPresentation(job);
        if (completedWarning) {
          showToast(warningToastMessage(job, completedWarning), "error", 12000);
        } else {
          showToast(`${getAuthor(job)} 的任务已完成`);
        }
      }
      if (nextRawStatus === "interrupted") showIssueToast(job, "任务已暂停");
      else if (nextStatus === "failed") showIssueToast(job, issueTitleForJob(job));
      if (nextStatus === "needs_auth") showIssueToast(job, issueTitleForJob(job));
    }

    const hasItemData = ["items", "media", "downloads", "results", "entries"].some((key) => Object.hasOwn(incoming, key));
    if (!hasItemData) scheduleDetailRefresh(job);
  }

  function connectEvents() {
    if (!("EventSource" in window)) {
      setConnection("", "定时刷新");
      return;
    }
    if (state.eventSource) state.eventSource.close();
    const source = new EventSource("/api/events");
    state.eventSource = source;
    source.onopen = () => {
      if (state.backendReady) setConnection("connected", "实时连接");
    };
    source.onerror = () => setConnection("disconnected", "正在重连");
    source.onmessage = handleEvent;
    ["job", "progress", "status", "update"].forEach((eventName) => source.addEventListener(eventName, handleEvent));
  }

  async function retryJob(jobId, itemId, button) {
    if (state.versionBlocked || !jobId || button?.disabled) return;
    if (button) {
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
    }
    try {
      const body = itemId === undefined || itemId === null ? {} : { item_id: itemId };
      const payload = await api(`/api/jobs/${encodeURIComponent(jobId)}/retry`, {
        method: "POST",
        body: JSON.stringify(body)
      });
      const job = extractJob(payload);
      if (job) upsertJob(job);
      await fetchJob(jobId, true);
      showToast(itemId === undefined || itemId === null ? "已开始继续任务" : "已开始重试这个作品");
    } catch (error) {
      showToast(`重试失败：${error.message}`, "error");
    } finally {
      if (button) {
        button.disabled = false;
        button.removeAttribute("aria-busy");
      }
    }
  }

  async function openVerification() {
    const id = state.selectedJobId;
    if (state.versionBlocked || !id || elements.authOpenButton.disabled) return;
    const job = state.jobs.get(String(id));
    elements.authOpenButton.disabled = true;
    try {
      await api(`/api/jobs/${encodeURIComponent(id)}/verify`, { method: "POST" });
      showToast(`已在 Chrome 打开${verificationTarget(job)}；完成后回到这里继续重试`);
    } catch (error) {
      showToast(`无法打开 Chrome：${error.message}`, "error");
    } finally {
      elements.authOpenButton.disabled = false;
    }
  }

  async function cancelSelectedJob() {
    const id = state.selectedJobId;
    if (state.versionBlocked || !id) return;
    const confirmed = window.confirm("确定要取消这个下载任务吗？已经下载完成的文件会保留。");
    if (!confirmed) return;
    elements.cancelButton.disabled = true;
    try {
      const payload = await api(`/api/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST" });
      const job = extractJob(payload);
      if (job) upsertJob(job);
      const refreshed = await fetchJob(id, true);
      showToast(
        canonicalStatus(refreshed || job || {}) === "cancelled"
          ? "任务已取消"
          : "已请求取消；当前网络请求或合并步骤结束后会安全停止"
      );
    } catch (error) {
      showToast(`取消失败：${error.message}`, "error");
    } finally {
      const current = state.jobs.get(id);
      elements.cancelButton.disabled = Boolean(current?.cancel_requested);
    }
  }

  async function createJob(event) {
    event.preventDefault();
    if (state.versionBlocked) return;
    elements.formError.textContent = "";
    const url = elements.urlInput.value.trim();
    if (!url || !/https?:\/\//i.test(url)) {
      elements.formError.textContent = "请输入链接，或粘贴包含链接的分享文案";
      elements.urlInput.focus();
      return;
    }

    setButtonLoading(elements.downloadButton, true, "正在创建");
    try {
      const payload = await api("/api/jobs", {
        method: "POST",
        body: JSON.stringify({ url })
      });
      const job = extractJob(payload);
      if (job) {
        const merged = upsertJob(job);
        state.selectedJobId = merged._id;
        renderSelectedJob();
        await fetchJob(merged._id, true);
      } else {
        await fetchJobs(true);
      }
      elements.urlInput.value = "";
      showToast("下载任务已创建，正在解析链接");
      document.querySelector(".job-panel")?.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) {
      const message = localizeRuntimeMessage(error.message);
      elements.formError.textContent = message;
      showToast(`创建任务失败：${message}`, "error");
    } finally {
      setButtonLoading(elements.downloadButton, false);
    }
  }

  async function loadConfig() {
    try {
      const payload = await api("/api/config");
      const config = payload?.config || payload?.data || payload || {};
      const cookieKeys = ["use_chrome_cookies", "chrome_cookies", "cookies_from_browser", "cookie_browser"];
      const cookieKey = cookieKeys.find((key) => Object.hasOwn(config, key));
      const cookieValue = cookieKey ? config[cookieKey] : true;
      elements.chromeCookies.checked = Boolean(cookieValue);
      state.chromeProfile = firstDefined(config.chrome_profile, config.browser_profile) ?? null;
      elements.downloadDir.value = firstDefined(config.download_dir, config.output_dir, config.download_path, "downloads");
      return true;
    } catch (error) {
      showToast(`读取设置失败：${error.message}`, "error");
      return false;
    }
  }

  async function saveConfig(event) {
    event.preventDefault();
    if (state.versionBlocked) return;
    const directory = elements.downloadDir.value.trim();
    if (!directory) {
      showToast("请填写下载目录", "error");
      elements.downloadDir.focus();
      return;
    }
    elements.saveSettingsButton.disabled = true;
    elements.saveSettingsButton.textContent = "保存中…";
    elements.settingsSaved.textContent = "";
    try {
      await api("/api/config", {
        method: "PUT",
        body: JSON.stringify({
          download_dir: directory,
          use_chrome_cookies: elements.chromeCookies.checked,
          chrome_profile: state.chromeProfile
        })
      });
      elements.settingsSaved.textContent = "已保存";
      showToast("下载设置已保存");
      window.setTimeout(() => {
        elements.settingsSaved.textContent = "";
      }, 3000);
    } catch (error) {
      showToast(`保存设置失败：${localizeRuntimeMessage(error.message)}`, "error");
    } finally {
      elements.saveSettingsButton.disabled = false;
      elements.saveSettingsButton.textContent = "保存设置";
    }
  }

  async function poll() {
    if (state.polling || state.versionBlocked || document.hidden) return;
    state.polling = true;
    state.pollingTick += 1;
    try {
      if (!(await verifyBackendBuild())) return;
      if (!state.initialized) {
        if (!state.eventSource) connectEvents();
        const [configLoaded] = await Promise.all([loadConfig(), fetchJobs(true)]);
        state.initialized = configLoaded;
        return;
      }
      if (state.selectedJobId) await fetchJob(state.selectedJobId, true);
      if (state.pollingTick % 3 === 0) await fetchJobs(true);
    } finally {
      state.polling = false;
    }
  }

  function refreshActivityClock() {
    const job = state.selectedJobId ? state.jobs.get(state.selectedJobId) : null;
    if (
      !job
      || rawStatus(job) !== "discovering"
      || !job.activity_started_at
      || !elements.progressLabel
    ) {
      return;
    }
    elements.progressLabel.textContent = progressDescription(job, getCounts(job));
  }

  function bindEvents() {
    elements.downloadForm.addEventListener("submit", createJob);
    elements.settingsForm.addEventListener("submit", saveConfig);
    elements.refreshButton.addEventListener("click", () => fetchJobs(false));
    elements.retryAllButton.addEventListener("click", () => retryJob(state.selectedJobId, undefined, elements.retryAllButton));
    elements.authOpenButton.addEventListener("click", openVerification);
    elements.authRetryButton.addEventListener("click", () => retryJob(state.selectedJobId, undefined, elements.authRetryButton));
    elements.cancelButton.addEventListener("click", cancelSelectedJob);
    elements.urlInput.addEventListener("input", () => {
      elements.formError.textContent = "";
    });
    elements.filterTabs.addEventListener("click", (event) => {
      const button = event.target.closest("[data-filter]");
      if (!button) return;
      state.filter = button.dataset.filter;
      elements.filterTabs.querySelectorAll("[data-filter]").forEach((tab) => {
        const active = tab === button;
        tab.classList.toggle("active", active);
        tab.setAttribute("aria-pressed", String(active));
      });
      renderSelectedJob();
    });
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) poll();
    });
    window.addEventListener("beforeunload", () => {
      if (state.eventSource) state.eventSource.close();
    });
  }

  async function initialize() {
    bindEvents();
    window.setInterval(refreshActivityClock, 1000);
    window.setInterval(poll, 4000);
    await poll();
  }

  initialize();
})();
