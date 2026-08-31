# Changelog

## [1.0.0] — 2026-08-31

Initial public release of PengyShare as a combined **image + video** hosting service.

### Added
- **Video hosting** alongside images: `mp4`, `mov`, `m4v`, `webm`, `mkv`.
- **Streaming uploads** — files write to disk in 1 MB chunks (no whole-file RAM load),
  with size enforcement mid-stream.
- **ffmpeg poster thumbnails** — videos get a 300px WebP frame extracted ~1s in.
- **HTTP Range / 206** serving via `send_file(conditional=True)` — inline previews and
  seeking work for raw `.mp4` URLs in Discord / Google Chat / browsers.
- **OpenGraph video/image meta** on view pages for rich link cards.
- **Mixed gallery** with video badges and play overlays.
- `kind` field (`image` | `video`) in the moofile store and API responses.
- API accepts the file field as `file` | `image` | `video`.
- `FFMPEG_PATH` config option.

### Changed
- `MAX_IMAGE_SIZE` default 20 MB → **512 MB**.
- `ALLOWED_EXTENSIONS` default extended with video formats.
- Upload path rewritten from whole-file read to streaming save.

### Fixed / hardening
- Large uploads no longer spike server memory.
- File type allowlist expanded to accept both `image/*` and `video/*` MIME.

### Notes
- Videos are hosted **as-is** (no transcode). For guaranteed inline previews keep clips
  as H.264 `.mp4` (GoPro `.mov`/HEVC downloads but may not preview in every client).
- nginx `client_max_body_size` must be raised above the upload cap to accept large files
  through a reverse proxy (see README).
