# 原迹下载器

一个在本机运行的 Web 下载工具：粘贴小红书、抖音、Bilibili 或 YouTube 的主页、播放列表或单条内容链接，自动发现作品，并保存平台当前可提供的最高质量媒体。

它提供实时进度、成功/失败明细、单项重试、全部失败项重试、任务取消和验证码提示。默认按作者创建文件夹，文件名以发布日期开头。

> 请只下载你本人拥有、已获授权或法律允许保存的内容。本项目不绕过 DRM、付费权限、地区限制或私密内容访问控制。

## 主要能力

- 本地 Web GUI，默认只监听 `127.0.0.1:8765`
- 小红书主页滚动发现，以及图片、视频、Live Photo 下载
- 抖音主页与单视频下载；逐条校验作者、作品 ID 和媒体 ID，避免主页内容串号
- Bilibili 主页、播放列表和单视频下载
- YouTube 频道、播放列表、普通视频与 Shorts 下载
- 视频不设置 720p、1080p、2K 或 4K 上限，自动探测并选择平台实际可提供的最高质量
- 小红书优先使用 direct CDN 原图和 `originVideoKey` 原视频地址
- 不压缩、不重编码；分离的视频流与音频流仅由 FFmpeg 合并封装
- 默认读取 Chrome Cookie，支持登录内容和常见风控场景
- 验证码或登录失效时显示 `等待验证`，完成验证后可重试
- 每个作品显示下载进度、成功/失败状态和失败原因；平台可提供时还会显示速度、ETA 与最终分辨率，多图与 Live Photo 作品可展开查看每个已保存文件
- 失败项可以逐个重试，也可以一键全部重试
- 任务状态保存在本机，程序异常退出后可继续处理失败项

## 支持矩阵

| 平台 | 主页/频道 | 播放列表 | 单条内容 | 下载内容与说明 |
|---|---:|---:|---:|---|
| 小红书 | ✅ | — | ✅ | 图片、视频、Live Photo；主页由 Playwright 滚动发现，媒体使用自定义原始资源解析 |
| 抖音 | ⚠️ 实验性 | — | ✅ | 视频；优先使用当前网页 SecSDK 获取并校验主页数据，失败时安全回退到 Playwright；下载前探测媒体流的实际分辨率 |
| Bilibili | ✅ | ✅ | ✅ | 视频；由 `yt-dlp` 处理，部分清晰度、会员或登录内容需要有效 Cookie 和合法账号权限 |
| YouTube | ✅ | ✅ | ✅ | 视频与 Shorts；由 `yt-dlp` 处理，高画质通常需要 FFmpeg 合并视频流和音频流 |

当前不下载 Bilibili/YouTube 的封面图，也不支持抖音图文作品。删除、私密、仅好友可见、付费、会员专享、地区受限或账号无权访问的内容不会被绕过。

小红书与抖音的主页并不是 `yt-dlp` 2026.08.19 原生支持的 playlist URL。小红书使用 Playwright 加载主页并滚动发现作品；抖音优先从当前网页加载的官方 SecSDK 发起签名请求，并在失败时回退到经过作者过滤的 Playwright 发现。网站页面、私有接口和风控规则经常变化，因此主页发现比单条链接更容易需要 Cookie、验证码或后续适配。

## “最高质量”和“原图”的含义

本项目不会人为限制分辨率：有 4K 就选 4K，有 2K 就选 2K，否则继续选择平台返回的下一档最高质量。

- YouTube 和 Bilibili 使用 `bestvideo*+bestaudio/best`，不添加最大高度条件。
- 抖音不会只相信网页的“2K/4K”标签或 `yt-dlp` 的默认格式排序：工具从已经校验作者和作品 ID 的数据中取得媒体 ID，优先探测抖音高质量播放端点的 `default` 原始档，再探测 4K、2K、1080p 和 720p 档，并用 FFprobe 检查媒体流的实际像素、编码、码率和时长，然后选择实际质量最高的有效流。
- 小红书视频优先使用 `originVideoKey`，再按像素、码率和文件大小选择可用流。
- 小红书图片优先重建 direct CDN 地址，网页中的 DFT/PRV 图片只作为最后回退。
- JPEG、PNG、WebP、AVIF、GIF 和 HEIC 根据文件 magic bytes 保存，不转换格式。原图是 HEIC 时会保留 `.heic`，部分系统图片查看器可能无法直接预览。
- FFmpeg 合并音视频流属于 remux，不会重编码或压缩画面。

这里的“原图/原视频”是平台当前向该账号和网络环境提供的最高质量文件，不代表作者上传前、尚未经过平台处理的本地母版。会员等级、登录状态、地区、视频编码和平台接口返回结果都可能影响最高可用清晰度。尤其是抖音，界面显示的“2K”属于质量档位名称，实际文件可能仍是 1080×1920；工具显示和选择时以媒体流实测像素为准。

## 运行前准备

需要：

- [Python](https://www.python.org/downloads/) 3.10 或更高版本
- [Google Chrome](https://www.google.com/chrome/)
- [FFmpeg](https://ffmpeg.org/download.html)
- 首次启动时可访问 PyPI，以安装 Python 依赖；其中会自动包含 YouTube 完整格式发现需要的 Deno runtime

先确认：

```bash
python3 --version
ffmpeg -version
```

macOS 可使用 Homebrew 安装 FFmpeg：

```bash
brew install ffmpeg
```

Ubuntu/Debian 可使用：

```bash
sudo apt update
sudo apt install ffmpeg
```

Windows 请安装 FFmpeg，并确保 `ffmpeg.exe` 所在目录已经加入 `PATH`。

## 傻瓜式启动

### macOS

双击 `start.command`。首次运行会创建 `.venv`、安装依赖、启动服务，并自动使用 Chrome 打开：

```text
http://127.0.0.1:8765
```

如果系统没有给脚本执行权限，在项目目录运行一次：

```bash
chmod +x start.command start.sh
./start.command
```

### Windows

双击 `start.bat`。脚本使用 Windows Python Launcher `py` 创建本地虚拟环境，然后自动启动 Web 工具。

### Linux

```bash
chmod +x start.sh
./start.sh
```

停止程序时，在启动它的终端中按 `Ctrl+C`。

## 手动启动

macOS/Linux：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python run.py
```

Windows PowerShell：

```powershell
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python run.py
```

不自动打开浏览器：

```bash
python run.py --no-browser
```

修改端口：

```bash
python run.py --port 9000
```

服务固定只监听本机 `127.0.0.1`，没有远程访问认证，也不会直接暴露到局域网或公网。

## 使用方法

1. 在 Chrome 中登录你需要使用的平台；小红书和抖音尤其建议先访问一次目标主页。
2. 启动工具，确认“自动读取 Chrome Cookie”处于开启状态。
3. 如有需要，修改下载目录并保存设置。
4. 粘贴主页、播放列表或单条内容链接，点击“开始下载”。
5. 在“下载明细”中查看每一项的进度、清晰度、成功状态或错误原因。
6. 单个作品失败时点击该行的“重试”；多个作品失败时点击“重试全部失败项”。已经保存成功的多图或 Live Photo 文件会保留并列在作品下方。

取消会在当前网络请求或 FFmpeg 合并步骤结束后安全停止，因此极端情况下按钮状态可能需要等待几十秒；界面会明确显示“正在等待当前步骤停止”，已经完成的文件不会删除。

默认测试案例：

```text
https://www.xiaohongshu.com/user/profile/5c99d4b30000000011015e6d
```

## Chrome Cookie 与验证码

Cookie 默认从本机 Chrome 读取，不会导出为 `cookies.txt`，也不会上传到远程服务器。应用配置和任务状态分别保存在 `data/config.json` 与 `data/state/`；其中不保存 Cookie 值。

在 macOS 上，系统可能询问是否允许访问 Chrome 的加密 Cookie，请按你的安全判断授权。如果提示 Cookie 数据库被占用、复制失败或解密失败，可以先完全退出 Chrome，再重新启动工具。为避免把匿名访问得到的较低清晰度误当成最终结果，Cookie 开关开启时不会静默回退到匿名下载；如果你明确希望不登录下载，可以在设置中关闭 Chrome Cookie 后重新创建任务。

出现 `等待验证` 时：

1. 点击“打开 Chrome 验证”，或使用普通 Chrome 打开任务的原始主页或视频链接。工具始终使用你提交的原始任务地址，不会用接口响应中的其他作品地址替换它。
2. 完成登录、滑块、短信验证或图形验证码。
3. 确认页面已经能够正常播放或浏览作品。
4. 回到 Web 工具，点击“我已完成，继续重试”或重试对应失败项。

应用不会自动破解验证码。抖音出现空白页面或页面加载超时时，也可能是风控表现；请先在 Chrome 中访问并验证，再重试。频繁连续抓取可能再次触发验证。

## 文件结构与命名

默认下载到项目内的 `downloads/`，并按作者分文件夹：

```text
downloads/
└── 作者名称/
    ├── 2025-11-14-视频标题 [media-id].mp4
    ├── 2025-11-14-图文标题 [note-id]-001.webp
    └── 2025-11-14-图文标题 [note-id]-002.heic
```

命名规则：

- 日期固定放在最前面，格式为 `YYYY-MM-DD`。
- 日期后是作品标题，尾部保留媒体 ID，避免同名覆盖。
- 多图笔记追加 `-001`、`-002` 等顺序号。
- 平台没有返回日期时使用 `Unknown-Date`。
- 文件夹名和文件名会清理 Windows/macOS/Linux 不允许的字符，但保留中文。
- 重复创建同一任务时会重新获取平台当前提供的最高质量，并替换同名媒体，避免已有低清文件阻止后续升级；写入中的临时文件不会作为完成结果。

早期版本创建的抖音主页任务没有保存足够的作者归属证据。升级后，这类历史“已完成”项目会被标记为需要人工检查且不可直接重试，旧文件不会被自动删除。请检查旧文件，并用当前版本重新创建主页任务；确认旧文件下载错误后再手动删除。

## 已验证的公开案例

以下结果来自 2026-08-26 至 2026-08-27、本机 `yt-dlp 2026.08.19`。大型主页没有整批下载；清晰度测试使用有限 Range 探测或单条样本下载：

| 平台 | 公开 URL | 结果 |
|---|---|---|
| 小红书 | `https://www.xiaohongshu.com/user/profile/5c99d4b30000000011015e6d` | 默认完整滚动发现约 243–245 篇笔记；数量会随页面实时内容与响应变化 |
| 小红书 | 上述主页中的公开图文 | direct CDN 原始 WebP 验证为 1920×2560、968,002 bytes；同图网页 DFT 版本为 156,405 bytes |
| 小红书 | 上述主页中的公开视频 | 原视频资源验证为 HEVC、3840×2160、54,360,962 bytes |
| YouTube | `https://www.youtube.com/@BlenderOfficial/videos` | `YoutubeTab` 成功发现频道视频 |
| YouTube | `https://www.youtube.com/watch?v=LXb3EKWsInQ` | 默认选择 `701+251`，3840×2160、60fps、HDR |
| Bilibili | `https://space.bilibili.com/946974/video` | Chrome Cookie 下成功发现空间视频；无 Cookie 时本机收到 HTTP 412 |
| Bilibili | `https://www.bilibili.com/video/BV1rp4y1e745` | 默认选择 3840×1920 HDR/4K 视频流与最高可用音频流 |
| 抖音 | `https://www.douyin.com/user/MS4wLjABAAAAyjrP-yPP2JYTBFC6qw6lsg-7EU6jI-UJFhhJqludJSo` | 签名主页请求发现 26/26 条视频，全部作者 `sec_uid` 与目标主页一致；不会把页面或网络中的其他作者作品加入任务 |
| 抖音 | 上述主页中的作品 `7671259887394052209` | 普通格式仅到 720×1280；质量端点有限 Range 实测最高有效流为 H.264、1080×1920，随后由完整单条下载再次核验；网页元数据中的 1440×2560 不作为实际下载分辨率 |
| 抖音 | `https://www.douyin.com/video/7664225419386607205` | `4k`、`2k` 和 `1080p` 请求实际都只有 H.264 1080×1920；`default` 原始档实测为 HEVC 1440×2560、59,093,472 bytes，工具会选择后者 |

`yt-dlp` 官方也说明，支持列表不代表网站在任何时刻都保证可用，最可靠的检查方式仍是实际尝试。可参考 [支持站点列表](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md)、[格式选择说明](https://github.com/yt-dlp/yt-dlp#format-selection) 和 [Cookie FAQ](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp)。

## 常见问题

### 为什么只有 1080p，没有 2K/4K？

工具没有设置分辨率上限。通常原因是平台本次只向当前账号返回到 1080p、账号没有对应权限、Cookie 失效、地区限制，或原作品本身没有更高版本。Bilibili 的部分 4K/HDR/高码率格式需要登录或会员权限。抖音的“2K”标签不保证文件像素达到 1440p，而且真正的最高画质有时位于 `default` 原始档而不是 `2k` 参数：工具会同时探测并以实际字节为准，不会把标签伪报成 2K，也不会漏掉独立的 1440×2560 原始档。

### 为什么抖音旧任务可能显示需要人工检查？

早期版本从整个网页收集视频链接，极端情况下可能混入页面推荐的其他作品。当前版本只接受目标主页接口中作者 `sec_uid` 一致的作品，并在下载前再次核对作品 ID、作者 ID 和媒体 ID。由于旧状态缺少这组证据，程序不会把旧结果自动视为可信；请新建任务重新下载并人工处理旧文件。

### 为什么 YouTube 下载时报 FFmpeg 错误？

YouTube 的最高画质经常把视频与音频分开提供。请安装 FFmpeg，确认终端执行 `ffmpeg -version` 成功，然后重试失败项。

### 为什么 Bilibili 报 412？

这通常是请求被站点风控拦截。先在 Chrome 登录并正常打开目标页面，保持 Cookie 开关开启，稍后重试；不要高频重复创建相同任务。

### 为什么小红书发现的数量每次略有不同？

主页会实时更新、置顶内容可能变化，滚动接口也可能受网络和风控影响。工具会持续滚动，直到连续多轮没有新作品；如明显少于浏览器中可见数量，请完成验证后重试。

### 为什么 HEIC 图片打不开？

这是为了保留平台提供的原始文件，工具不会把 HEIC 转成 JPEG。请使用支持 HEIC 的系统或查看器；如需转换，请在下载完成后另行制作副本，避免修改原始文件。

### 任务失败后，已经成功的文件会丢失吗？

不会。取消任务或重试失败项不会删除已完成文件。批量重试只重新处理失败、等待验证或已取消的项目。

## 测试

离线测试使用 mock `yt-dlp` 与 mock 下载引擎，不依赖外部网站：

```bash
python -m pip install -r requirements-dev.txt
pytest
```

覆盖范围包括：

- 四个平台 URL 识别、分享文本解析和相似恶意域名拒绝
- 作者目录与跨平台文件名清理
- 日期前缀、多图顺序号和媒体 magic bytes 扩展名识别
- 未限高的最高格式参数与 4K 进度事件
- 抖音主页作者隔离、作品 ID/媒体 ID 防串号、SecSDK 签名响应校验、真实分辨率探测和旧任务安全迁移
- Chrome Cookie epoch 转换、读取失败提示与显式匿名模式
- 小红书原图候选、原视频、Live Photo 与验证码页解析
- 实时进度、任务状态持久化、单项重试和全部失败项重试

## 隐私、安全与免责声明

- 服务默认只监听本机回环地址，媒体和任务数据不经过第三方中转服务器。
- 下载链接、标题和本地输出路径会写入 `data/state/`，Cookie 值不会写入项目状态文件。
- 不要提交 `data/`、`downloads/`、Cookie 文件或含私人链接的日志；这些目录已加入 `.gitignore`。
- 你需要自行遵守各平台服务条款、版权法、隐私权、肖像权和所在地区法律。
- 不要批量抓取你无权保存的内容，不要将下载内容用于骚扰、监控、盗版或未经授权的再分发。
- 本项目按现状提供；网站改版、风控升级或上游 `yt-dlp` 变化都可能暂时导致功能失效。

## 技术栈

- FastAPI + Uvicorn：本地 API、静态页面与 SSE 实时事件
- `yt-dlp` + Deno runtime：YouTube、Bilibili、抖音单视频及完整媒体格式选择
- Playwright + Chrome：小红书/抖音主页发现
- FFmpeg：最高质量分离音视频流的无重编码合并
- Pydantic + JSON：任务模型与本地状态持久化

## License

[MIT](LICENSE)
