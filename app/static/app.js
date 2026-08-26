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
    selectedJobId: null
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
    return firstDefined(item?.title, item?.name, pathName, item?.description, `作品 ${String(index + 1).padStart(2, "0")}`);
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

  function itemError(item) {
    if (!isRetryableItem(item)) return "";
    const fallback = canonicalStatus(item) === "cancelled" ? "下载已取消，可以重新尝试" : "下载失败，请重试";
    return localizeRuntimeMessage(firstDefined(item?.error_message, item?.error, item?.reason, item?.message, fallback));
  }

  function localizeRuntimeMessage(value) {
    let text = asText(value);
    if (text.includes("FFprobe was found but could not be started")) {
      return "已找到 FFprobe，但程序无法启动它。请重新安装包含 FFprobe 的 FFmpeg，然后完全停止并重启程序。";
    }
    if (text.includes("FFprobe was not found")) {
      return "未找到 FFprobe，无法验证抖音最高画质。请安装包含 FFprobe 的 FFmpeg，将其 bin 目录加入 PATH，然后完全停止并重启程序；macOS Homebrew 可运行 brew install ffmpeg。";
    }
    if (!text.includes("Douyin profile media was discovered")) return text;

    text = text
      .replace(
        /Douyin profile media was discovered, but its highest quality could not be verified\.[\s\S]*?Probe details:\s*/,
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
      .replaceAll("media duration did not match the requested Douyin item", "媒体时长与目标抖音作品不匹配")
      .replaceAll("no verified media identity was available", "没有可验证的媒体身份")
      .replaceAll("no playable candidate was returned", "未返回可播放的候选媒体")
      .replaceAll("media metadata could not be parsed", "无法解析媒体元数据")
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
    elements.connectionStatus.classList.remove("connected", "disconnected");
    if (mode) elements.connectionStatus.classList.add(mode);
    elements.connectionStatus.querySelector("span:last-child").textContent = text;
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
      if (typeof phaseMessage === "string" && phaseMessage.startsWith("Checking Douyin quality")) {
        return phaseMessage
          .replace("Checking Douyin quality", "正在检测抖音最高画质")
          .replace(": default", ": 原始档");
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
        if (state.filter === "failed") return isRetryableItem(item);
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
      node.querySelector(".item-error").textContent = itemError(item);

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
    if (needsAuth) {
      elements.authMessage.textContent = asText(firstDefined(
        job?.auth_message,
        job?.action_message,
        job?.error_message,
        job?.error,
        "请打开对应网站，完成登录或验证码后回到这里继续。"
      ));
    }
    const discoveryIncomplete = job?.discovery_complete === false;
    const cookieFallback = Boolean(job?.cookie_fallback_used);
    const warningMessage = asText(job?.warning);
    const hasWarning = (discoveryIncomplete && (!isRunning(job) || Boolean(warningMessage))) || cookieFallback || Boolean(warningMessage);
    elements.warningAlert.hidden = !hasWarning;
    if (hasWarning) {
      elements.warningTitle.textContent = discoveryIncomplete
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
    elements.retryAllButton.hidden = isRunning(job) || (counts.failed === 0 && !needsAuth && canonicalStatus(job) !== "failed" && !resumable);
    elements.retryAllLabel.textContent = discoveryIncomplete
      ? "继续发现"
      : resumable
        ? "继续任务"
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
    if (!jobId || button?.disabled) return;
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
    if (!id || elements.authOpenButton.disabled) return;
    elements.authOpenButton.disabled = true;
    try {
      await api(`/api/jobs/${encodeURIComponent(id)}/verify`, { method: "POST" });
      showToast("已在 Chrome 打开验证页面；完成后回到这里继续重试");
    } catch (error) {
      showToast(`无法打开 Chrome：${error.message}`, "error");
    } finally {
      elements.authOpenButton.disabled = false;
    }
  }

  async function cancelSelectedJob() {
    const id = state.selectedJobId;
    if (!id) return;
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
    if (state.polling || document.hidden) return;
    state.polling = true;
    state.pollingTick += 1;
    try {
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
    connectEvents();
    await Promise.all([loadConfig(), fetchJobs(true)]);
    window.setInterval(poll, 4000);
  }

  initialize();
})();
