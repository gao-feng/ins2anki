# ins2anki

An [opencode](https://opencode.ai) skill that turns a short-video post or reel into Anki vocabulary flashcards. It downloads the media, transcribes spoken English (or reads the caption), surfaces genuinely challenging words, and — after you confirm the selection — imports them through AnkiConnect as Basic notes with detailed Chinese meanings, IPA, local TTS pronunciation, and the original source media.

Supported sources: **Instagram** posts/reels and saved collections, **Xiaohongshu (小红书)** notes, and **Douyin (抖音)** videos. All three share one incremental sync engine.

## Workflow

1. **Collect** — `scripts/download_media.py` fetches the post with `yt-dlp` and writes `manifest.json` (media list + caption + metadata). `scripts/download_instagram.py` is a thin Instagram-pinned wrapper kept for compatibility. Pass `--cookies` / `--cookies-from-browser` for login-gated content only with the user's authorization.
2. **Transcribe & extract** — Whisper transcribes the audio with timestamps (OCR/caption for images and carousels). Challenging B2–C2 words and fixed expressions are selected with Chinese meanings, IPA, a faithful source excerpt, and a difficulty note.
3. **Confirm** — a numbered candidate list is presented; the user confirms (1) which words, (2) the meanings, (3) media attachment, and (4) whether to add TTS pronunciation. Nothing is written to Anki until the user replies.
4. **Save** — `scripts/tts_word.py` generates per-word pronunciation MP3s with local TTS; a UTF-8 `selection.json` is written per `references/selection-schema.md`; then `scripts/anki_import.py` creates the deck, uploads each media file once, and adds Basic notes — word plus optional `[sound:...]` on the front; meaning, context, position, source link, and optional media on the back.
5. **Report** — deck, note count, attached media, and any skipped duplicates or failures.

## Repository layout

```
instagram-to-anki/
  SKILL.md                    # skill definition (workflow the agent follows)
  agents/openai.yaml          # agent interface metadata
  scripts/
    platforms.py              # URL detection/normalization for all 3 platforms
    sync_common.py            # shared incremental engine + state/validation
    download_media.py         # multi-platform downloader + manifest writer
    sync_saved.py             # incremental sync CLI (--platform)
    sync_collections.py       # multi-collection inventory coordinator
    browser/
      export_collection.js    # console script: export a 收藏夹 to inventory JSON
    download_instagram.py     # thin Instagram wrapper (compatibility)
    sync_instagram_saved.py   # thin Instagram wrapper (compatibility)
    sync_instagram_collections.py # thin Instagram wrapper (compatibility)
    tts_word.py               # local TTS pronunciation (Windows SAPI + ffmpeg)
    anki_import.py            # AnkiConnect importer (notes + media)
  references/
    selection-schema.md       # confirmed-selection JSON schema
    tooling.md                # download / transcription / TTS / Anki notes
```

`downloads/` (fetched media, transcripts, `selection.json`) and `__pycache__/` are gitignored — downloaded media is never committed or redistributed.

## Prerequisites

- [opencode](https://opencode.ai) — optional; only the agent-driven workflow needs it
- A Chromium-based browser (Edge/Chrome/Chromium/Brave) — only for the browser-session sync below
- Python 3.10+
- [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) — Instagram download
- [`ffmpeg`](https://ffmpeg.org/) — audio extraction for transcription and MP3 encoding for TTS
- A local Whisper-compatible STT (e.g. `pip install -U openai-whisper`)
- Anki Desktop with the [AnkiConnect](https://ankiweb.net/shared/info/2055492159) add-on running at `http://127.0.0.1:8765`
- For pronunciation: an English SAPI voice on Windows (Settings → Time & Language → Speech → Add voices), or adapt `tts_word.py` to `say` (macOS) / `espeak-ng` (Linux)

```powershell
pip install -U yt-dlp openai-whisper
winget install --id=Gyan.FFmpeg -e
```

Xiaohongshu and Douyin reject anonymous extraction, so their syncs need an
already logged-in browser profile (`--cookies-from-browser chrome`) or an
exported Netscape cookies file. Cookies are read directly by `yt-dlp`; the sync
state never stores them.

## Usage

Inside an opencode session, give an Instagram URL and ask to study its English:

> https://www.instagram.com/p/DYaN8HxT-pt/

The agent runs the workflow, presents candidates, and waits for your confirmation before touching Anki. The default deck is `ins`.

### 一键同步整个 Instagram 收藏夹（推荐，不需要 cookie）

双击仓库根目录的 [同步收藏夹.command](<同步收藏夹.command>) 即可。首次运行会打开一个
**专用浏览器窗口**（profile 在 `~/.ins2anki/browser-profile`，与你平时的 Edge/Chrome 互不影响），
在里面登录一次 Instagram；之后每次双击都只做增量同步，已下载的自动跳过，文件写到
`instagram-saved/<收藏夹名>/`。

这条路不导出、不读取 cookie，因此**不会触发 macOS 钥匙串的"访问机密信息"授权弹窗**：
登录态留在浏览器里，脚本只让页面自己调用 Instagram 的网页接口，拿到带签名的媒体直链后由
Python 直接下载（[browser_sync.py](<instagram-to-anki/scripts/browser_sync.py>)）。
整条链路是确定性的，不需要 AI 或 skill 参与。

命令行等价形式：

```bash
python3 instagram-to-anki/scripts/browser_sync.py launch   # 一次性：打开专用浏览器并登录
python3 instagram-to-anki/scripts/browser_sync.py check    # 确认登录状态与收藏夹数量
python3 instagram-to-anki/scripts/browser_sync.py sync \
  --all-collections --output-root instagram-saved
```

只同步一个收藏夹：

```bash
python3 instagram-to-anki/scripts/browser_sync.py sync \
  --collection 自然 --output-dir instagram-saved/自然
```

产物与 yt-dlp 路径完全一致（`<短码>/<短码>_<作者>.mp4|.jpg`、`manifest.json`、
`sync-state.json`），所以下游选片与 Anki 导入脚本无需改动。补充开关：

- `--jobs N`：并行下载数（默认 4，启动器用 6）。签名直链彼此独立，并发是安全的
- `--dom-fallback`：网页接口返回不了时，改为滚动页面抓取帖子链接（此时媒体回退到 yt-dlp）
- `--no-yt-dlp-fallback`：没有直链的条目直接记为失败，而不是悄悄回退到慢路径
- `--prefer-yt-dlp`：把同一批条目交回旧的 yt-dlp 路径，用于对照排查
- `--no-include-photos`：只要视频，跳过图片帖
- `--limit N`：每个收藏夹本次最多下载 N 条（想先试水就用 `--limit 5`）

#### 已经下过的不会重下

`--all-collections` 会先看 `--output-root` 里已有的文件夹：每个 `sync-state.json` 都记录了它来自哪个收藏夹，
所以**旧的目录树本身就是一份本地清单**，不依赖任何接口。已完成的条目直接跳过，只补新条目与失败项，
并且继续写在原来的文件夹里（例如 `instagram-saved/自然/`）。

#### QuickTime 打不开某些 mp4？

那些是 **VP9 编码** 的视频（装在 mp4 容器里）。macOS 自己的解码器（QuickTime Player、预览、照片、Quick Look/缩略图）
只支持 H.264 与 HEVC，**不支持 VP9/AV1**；VLC、IINA 可以放。旧流程走 yt-dlp，而 yt-dlp 默认偏好 VP9（同码率更清晰），
所以早期下载的文件大量是这种。

现在的会话路径拿到的是 Instagram 自己的渐进式 `video_versions`（H.264 + AAC），双击就能播；
yt-dlp 回退路径也已固定为 `--format 'bv*[vcodec^=avc1]+ba[acodec^=mp4a]/…' --merge-output-format mp4`。

刷新历史文件（重新从 Instagram 取 H.264，无损，比本地转码快）：

```bash
python3 instagram-to-anki/scripts/browser_sync.py repair --output-root instagram-saved           # 只报告
python3 instagram-to-anki/scripts/browser_sync.py repair --output-root instagram-saved --forget  # 记为待重下
# 然后双击 同步收藏夹.command（或跑一次 sync --all-collections）
python3 instagram-to-anki/scripts/browser_sync.py repair --output-root instagram-saved           # 期望 unplayable: 0
python3 instagram-to-anki/scripts/browser_sync.py repair --output-root instagram-saved --clean    # 删掉备份
```

`--forget` **不删除**原文件：它把条目目录改名为 `<短码>.unplayable/` 并从 sync state 中移除，
所以即使那条帖子已被删除，原件仍在磁盘上可恢复。

#### 网络抖动会自动重试

Instagram 的 CDN 经常在传输中途掐断连接（`SSL: UNEXPECTED_EOF_WHILE_READING`、`IncompleteRead`、5xx）。
下载会重试 4 次（指数退避），并用 `Range` 从断点续传，所以轮播图里某一张断线不会让整条帖子失败；
重试的条目也不会重复下载已经落盘的那几张图/视频。仍然失败的条目会在日志末尾列出 `<短码>: 原因`，
同一份信息也在汇总 JSON 的 `failures` 和 `sync-state.json` 里；再跑一次只会重试这些失败项。

#### 如果 Instagram 改了接口

`/api/v1/collections/list/` 这类 REST 路径随时可能被下线（现在就会返回 **404 + SPA 外壳**）。
工具会自动降级到**读取页面自己发出的请求**：让收藏页自己加载，再从 CDP 的 `Network` 域读它拿到的
JSON（REST 或 GraphQL 都行），所以接口改名不影响使用。日志里会看到：

```
note: page JavaScript failed: Error: HTTP 404 for /api/v1/collections/list/: <!DOCTYPE html>...
reading the collections the saved page loads for itself ...
capture: 3 item(s) from 2 response(s) the page fetched
```

需要排查时用 `diagnose`，它会列出页面**实际**请求了哪些 API 路径、抓到几个响应、能解析出多少条目：

```bash
python3 instagram-to-anki/scripts/browser_sync.py diagnose --launch
```

- `"json_responses": 0`：页面还没加载完，加大 `--scroll-rounds`
- 有响应但 `"media_items": 0`：解析规则没覆盖这种返回，把 `api_paths_called` 发我即可

#### 为什么这条路快很多

| | 会话路径（本工具） | yt-dlp 路径 |
| --- | --- | --- |
| 每条帖子的开销 | 0（枚举时一次性拿到全部直链） | 一次完整 extractor：多次 API 往返 + 限速退避 |
| 传输 | 带签名的 CDN 直链，普通 HTTP GET，可 `--jobs` 并行 | 串行，且要与解析交错 |
| 登录 | 复用浏览器会话，不导出 cookie、不读钥匙串 | 需要 cookie 文件或 `--cookies-from-browser` |

所以"7 小时"几乎总是 yt-dlp 的解析开销，而不是带宽。每次运行结束的 JSON 里会给出
`via_session` / `via_ytdlp` 两个计数：如果 `via_ytdlp` 不为 0，慢的就是它（常见原因是被限流或
专用浏览器窗口没登录），用 `--no-yt-dlp-fallback` 可以让这些条目直接失败暴露出来。
每条日志里有单文件 `MiB/s` 和整体 `items/s + eta`，先跑 `--limit 5` 就能估出全量时间。
`sync-state.json` 里还记录了这次运行的 `jobs` 与 `elapsed_seconds`。

### Incrementally sync a saved collection

Use the saved collection URL and an already logged-in browser profile. Cookies are
read directly by `yt-dlp`; the sync state does not store them:

```bash
python instagram-to-anki/scripts/sync_saved.py \
  --platform instagram \
  --collection-url 'https://www.instagram.com/USER/saved/_/COLLECTION_ID/' \
  --cookies-from-browser chrome \
  --output-dir instagram-saved
```

The command writes `instagram-saved/sync-state.json`. A later run discovers the
collection again, skips posts with a valid manifest and media file, downloads only
new posts, and retries prior failures. Use `--no-retry-failed` to skip failures or
`--limit 20` to cap one run.

Instagram may temporarily prevent `yt-dlp` from enumerating a saved collection.
In that case, export one post/reel URL per line and use the same incremental engine:

```bash
python instagram-to-anki/scripts/sync_saved.py \
  --urls-file saved-urls.txt \
  --cookies-from-browser chrome \
  --output-dir instagram-saved
```

The older entry points `sync_instagram_saved.py` and
`sync_instagram_collections.py` still work as Instagram-pinned aliases of
`sync_saved.py` and `sync_collections.py`.

Use `--dry-run` to update discovery state and display pending URLs without
downloading. Do not share cookie files or downloaded private media.

### Incrementally sync Xiaohongshu and Douyin favorites (收藏夹)

`yt-dlp` only addresses single items on these platforms — `XiaoHongShu` matches
`/explore/<id>` and `/discovery/item/<id>`, `Douyin` matches `/video/<id>` — so a
收藏夹 cannot be enumerated from the command line. Export it from the browser
you are already logged into.

1. Open the favorites page:

   | Platform | Page |
   | --- | --- |
   | Xiaohongshu | `https://www.xiaohongshu.com/user/profile/<uid>` → **收藏** |
   | Douyin | `https://www.douyin.com/user/self?showTab=collection` |
   | Instagram | `https://www.instagram.com/<user>/saved/<collection-id>/` |

2. Open the browser console and paste [`scripts/browser/export_collection.js`](instagram-to-anki/scripts/browser/export_collection.js).
   It auto-scrolls until no new links appear, merges the result into
   `localStorage`, then logs, copies and downloads `favorites-inventory.json`.
   Run it once per 收藏夹 — results accumulate. To name a collection explicitly,
   set `window.__FAVORITES_NAME = "英语"` before running.

3. Sync one collection incrementally:

   ```bash
   python instagram-to-anki/scripts/sync_saved.py \
     --urls-file collection-urls.txt \
     --platform xiaohongshu \
     --cookies-from-browser chrome \
     --output-dir xhs-saved/英语
   ```

   `--urls-file` takes one post URL per line (`jq -r '.collections[0].posts[]'`
   extracts a list). `--platform` may be omitted to detect each URL, but both
   platforms require cookies. Short links (`xhslink.com`, `v.douyin.com`) are
   resolved automatically.

4. Or sync every collection in the exported inventory at once — each gets its
   own directory and its own incremental state:

   ```bash
   python instagram-to-anki/scripts/sync_collections.py \
     --inventory favorites-inventory.json \
     --cookies-from-browser chrome \
     --output-dir saved
   ```

   Re-export the inventory and re-run to download only new posts.

### Mirror multiple saved collections

The local coordinator creates one safe directory per collection and gives every
directory its own incremental `sync-state.json`:

```bash
python instagram-to-anki/scripts/sync_collections.py \
  --inventory collections.json \
  --cookies-from-browser chrome \
  --output-dir instagram-saved
```

Inventory format (`platform` and `url` are optional; `platform` overrides
`--platform` for that entry):

```json
{
  "collections": [
    {
      "name": "英语",
      "platform": "instagram",
      "url": "https://www.instagram.com/USER/saved/_/COLLECTION_ID/",
      "posts": ["https://www.instagram.com/reel/POST_ID/"]
    }
  ]
}
```

Directory names preserve Unicode names, replace filesystem-reserved characters,
and add a collection-ID suffix when two collections share a name. Run the same
command again after refreshing the browser inventory to download only new or
missing posts.

## Notes

- Download only content you are authorized to access; do not bypass access controls or redistribute fetched media.
- The importer never reports success unless AnkiConnect returns `addedNoteIds` for every requested note; duplicate or partial failures are reported verbatim, not silently retried.
- Xiaohongshu image notes (图文) are downloaded as their full image list. Douyin image notes (`/note/<id>`) are **not** supported: `yt-dlp` exposes only a video cover for them, so they are marked `failed` with an explicit reason instead of being saved as a single cover image.
- Item state (`sync-state.json`) is per output directory. An item counts as completed only while its manifest still references a non-empty media file; if the media is deleted, the next run re-downloads it.
- After editing any skill file, restart opencode so the change takes effect.
