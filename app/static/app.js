(() => {
  "use strict";

  const elements = {
    authAlert: document.querySelector("#auth-alert"),
    authMessage: document.querySelector("#auth-message"),
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
    chromeProfile: null,
    eventSource: null,
    filter: "all",
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
        return Math.min(100, ((counts.success + counts.failed) / counts.total) * 100);
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
    const activeItem = getItems(job).find((item) => isRunning(item));
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
    const activeItem = getItems(job).find((item) => isRunning(item));
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

  function verificationTarget(job) {
    return isProfileJob(job) ? "原主页" : "原视频";
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

  function statusLabel(entity) {
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
    return localizeRuntimeMessage(firstDefined(item?.error_message, item?.error, item?.reason, item?.message, fallback), job);
  }

  function localizedProbeDetails(text) {
    const marker = "Probe details:";
    if (!text.includes(marker)) return "";
    return text
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
      return `抖音${phase}跳转到了尚未识别的媒体 CDN${fingerprintText}。程序在读取文件前已拦截，这个媒体响应没有保存；此前已完成的文件会保留，并暂停了后续队列。请先检查代理或 VPN 后重试；${feedbackText}，不要发送带签名的完整媒体链接，也不需要打开 Chrome 验证。`;
    }
    if (diagnostic.reason === "nonstandard-port") {
      const family = douyinCdnFamily(diagnostic.host);
      const familyText = family ? `CDN 域名族：${family}` : "CDN 域名族：未识别";
      const portText = diagnostic.port ? `端口：${diagnostic.port}` : "端口：旧记录未保存";
      const fingerprintText = diagnostic.fingerprint
        ? `，校验指纹：${diagnostic.fingerprint}`
        : "";
      return `抖音${phase}地址使用了尚未验证的非标准 HTTPS 端口（${familyText}，${portText}${fingerprintText}）。程序在读取文件前已拦截，没有保存这个媒体响应；此前已完成的文件会保留，并暂停后续队列。仅凭这个结果无法判断是已失效的旧媒体地址、尚未确认的 CDN，还是代理或 VPN 改写。请从原主页或原视频链接继续或重试以刷新作品；若关闭代理后仍复现，只需反馈这里显示的域名族、端口或校验指纹，不要发送带签名的完整媒体链接，也不需要打开 Chrome 验证。`;
    }
    return `抖音${phase}的重定向地址未通过安全校验。原因：${diagnostic.reasonText}。程序在读取文件前已拦截，这个媒体响应没有保存；此前已完成的文件会保留，并暂停了后续队列。请检查代理或 VPN 是否改写了媒体地址，稍后从原链接重试，不需要打开 Chrome 验证。`;
  }

  function localizeRuntimeMessage(value, job = null) {
    let text = asText(value);
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
    if (text.includes("Douyin stopped returning new videos before the profile reported completion") || text.includes("Douyin reached the discovery safety limit before confirming the end of the profile")) {
      return "抖音主页在确认列表结束前停止返回新作品。当前结果可能不完整，请稍后点击继续发现；已有成功记录不会重复下载。";
    }
    if (text.includes("Douyin returned an uploader profile instead of the requested video")) {
      return "抖音把目标视频错误返回成了博主主页，程序已拦截这些非目标作品。请直接从原视频链接重试；没有出现明确验证码或登录页面时，不需要打开 Chrome 验证。";
    }
    if (text.includes("Douyin could not create a verified signed request")) {
      return "这是旧版本把通用签名失败误标成了验证码。请直接从原链接重试；只有抖音明确显示验证码或登录页面时才需要打开 Chrome。";
    }
    if (text.includes("Douyin requires current Chrome cookies or an explicit verification")) {
      return `抖音明确要求最新 Chrome Cookie、登录或验证码。请在 Chrome 打开${verificationTarget(job)}完成验证后再重试。`;
    }
    if (text.includes("Douyin temporarily limited the verified author-feed request") || text.includes("Douyin temporarily limited a signed request")) {
      return "抖音作者接口正在短时限流，程序已自动退避重试，仍未恢复。请等待一两分钟后重试；程序没有改下低清版本，也不需要先打开作者主页验证。";
    }
    if (text.includes("Douyin media transfer was temporarily unavailable")) {
      return "抖音原文件传输遇到临时网络错误或限流。程序已保留完成的文件并暂停后续队列，避免整页连续失败；请稍等后点击继续任务，不需要打开其他作品或作者主页验证。";
    }
    if (text.includes("Douyin media redirect could not be trusted")) {
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
    if (text.includes("Douyin signed discovery failed before a verified response") || text.includes("Douyin signed data failed identity or integrity validation") || text.includes("Douyin author-feed data failed identity or integrity validation")) {
      return "抖音响应未通过作品身份或完整性校验，程序已停止处理。请从原链接重试；没有明确验证码或登录页面时，不需要打开 Chrome 验证。";
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
      text.includes("Douyin highest-quality verification requires the verified author feed") ||
      text.includes("This saved Douyin task predates author-feed highest-quality verification") ||
      text.includes("This saved Douyin Live Photo task predates author-feed highest-quality verification") ||
      text.includes("Douyin Live Photo has no structured author-feed quality renditions") ||
      text.includes("Douyin item discovery returned no verified author-feed direct rendition")
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
      return `抖音 Live Photo 的作者直连或原始档没有通过完整性校验，程序没有下载可能降级的动态图${details ? `。本次原因：${details}` : ""}。`;
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

    text = text
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
    return text;
  }

  function authRequired(job) {
    return Boolean(job?.needs_auth || job?.auth_required || canonicalStatus(job) === "needs_auth" || getItems(job).some((item) => canonicalStatus(item) === "needs_auth"));
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

  function showToast(message, type = "success") {
    const toast = document.createElement("div");
    toast.className = `toast ${type === "error" ? "error" : ""}`;
    toast.setAttribute("role", type === "error" ? "alert" : "status");
    toast.textContent = message;
    elements.toastRegion.append(toast);
    window.setTimeout(() => {
      toast.classList.add("leaving");
      window.setTimeout(() => toast.remove(), 220);
    }, 4200);
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
    try {
      const response = await fetch("/api/health", {
        cache: "no-store",
        headers: { Accept: "application/json", "Cache-Control": "no-cache" }
      });
      if (response.ok) health = await response.json();
    } catch {
      health = null;
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
      elements.buildInfo.textContent = `v${health.version} · ${health.build_id}`;
      return true;
    }

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
    const current = firstDefined(job?.current_item, job?.current_title, job?.message, job?.detail);
    if (typeof current === "string" && current.trim()) return current;
    const activeItem = getItems(job).find((item) => {
      const itemId = firstDefined(item?.id, item?.item_id, item?.media_id);
      return job?.active_item_id ? String(itemId) === String(job.active_item_id) : isRunning(item);
    });
    if (activeItem) {
      const phaseMessage = activeItem?.progress?.filename;
      if (typeof phaseMessage === "string") {
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
      }
      const index = Math.max(0, getItems(job).indexOf(activeItem));
      return `正在处理：${itemTitle(activeItem, index)}`;
    }
    if (counts.total !== null && counts.total > 0) {
      return `已处理 ${Math.min(counts.total, counts.success + counts.failed)} / ${counts.total} 个作品`;
    }
    const status = canonicalStatus(job);
    if (status === "completed") return "所有作品处理完成";
    if (status === "failed") return "任务未能完成，可查看失败明细";
    if (status === "needs_auth") return "验证完成后即可继续";
    if (status === "cancelled") return "任务已取消，已下载文件会保留";
    return "正在解析主页内容…";
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
      status.textContent = statusLabel(item);
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
      if (isRetryableItem(item) && id !== undefined && id !== null) {
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
    elements.totalCount.textContent = counts.total === null ? "—" : String(counts.total);

    elements.authAlert.hidden = !needsAuth;
    elements.authRetryButton.hidden = job?.retryable === false;
    if (needsAuth) {
      elements.authMessage.textContent = localizeRuntimeMessage(firstDefined(
        job?.auth_message,
        job?.action_message,
        job?.error_message,
        job?.error,
        "请打开对应网站，完成登录或验证码后回到这里继续。"
      ), job);
      elements.authOpenButton.textContent = isProfileJob(job)
        ? "打开 Chrome 验证主页"
        : "打开 Chrome 验证视频";
    }
    const items = getItems(job);
    const discoveryFailureMessage = canonicalStatus(job) === "failed" && (items.length === 0 || job?.discovery_complete === false)
      ? localizeRuntimeMessage(firstDefined(job?.error_message, job?.error, job?.message), job)
      : "";
    const discoveryIncomplete = job?.discovery_complete === false;
    const cookieFallback = Boolean(job?.cookie_fallback_used);
    const warningMessage = discoveryFailureMessage || localizeRuntimeMessage(job?.warning, job);
    const hasWarning = (discoveryIncomplete && (!isRunning(job) || Boolean(warningMessage))) || cookieFallback || Boolean(warningMessage);
    elements.warningAlert.hidden = !hasWarning;
    if (hasWarning) {
      elements.warningTitle.textContent = discoveryFailureMessage
        ? "任务暂时失败"
        : discoveryIncomplete
        ? "主页发现可能不完整"
        : cookieFallback
          ? "Chrome Cookie 读取失败，当前使用未登录模式"
          : "任务提示";
      elements.warningMessage.textContent = warningMessage || (
        cookieFallback
          ? "作品列表可能不完整，受限的最高画质也可能缺失。请检查 Chrome 登录状态后重新创建任务。"
          : "站点未确认已经发现主页全部作品，请稍后继续发现。"
      );
    }
    const hasUnprocessedItems = counts.total !== null && counts.success + counts.failed < counts.total;
    const resumable = ["cancelled", "interrupted"].includes(rawStatus(job)) || discoveryIncomplete || (rawStatus(job) === "partial" && hasUnprocessedItems);
    const hasRetryableItems = items.some(isRetryableItem);
    const canRetryDiscovery = items.length === 0 && (needsAuth || canonicalStatus(job) === "failed" || resumable);
    elements.retryAllButton.hidden = job?.retryable === false || isRunning(job) || (!hasRetryableItems && !resumable && !canRetryDiscovery);
    elements.retryAllLabel.textContent = discoveryIncomplete
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
      if (nextRawStatus === "partial") showToast(`${getAuthor(job)} 的任务部分完成，请查看明细`, "error");
      else if (nextStatus === "completed") showToast(`${getAuthor(job)} 的任务已完成`);
      if (nextStatus === "failed") showToast(`${getAuthor(job)} 的任务失败，请查看明细`, "error");
      if (nextStatus === "needs_auth") showToast("任务需要登录或验证码验证", "error");
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
    source.onopen = () => setConnection("connected", "实时连接");
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
      elements.formError.textContent = error.message;
      showToast(`创建任务失败：${error.message}`, "error");
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
    } catch (error) {
      showToast(`读取设置失败：${error.message}`, "error");
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
      showToast(`保存设置失败：${error.message}`, "error");
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
      if (state.selectedJobId) await fetchJob(state.selectedJobId, true);
      if (state.pollingTick % 3 === 0) await fetchJobs(true);
    } finally {
      state.polling = false;
    }
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
    if (!(await verifyBackendBuild())) return;
    connectEvents();
    await Promise.all([loadConfig(), fetchJobs(true)]);
    window.setInterval(poll, 4000);
  }

  initialize();
})();
