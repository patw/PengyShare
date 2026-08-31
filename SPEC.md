# PengyShare Specification

**Version:** 1.0.0
**Status:** Implemented & deployed
**Last updated:** 2026-08-31

---

## 1. Purpose

PengyShare is a small, self-hosted media hosting service that accepts uploads of
**images** and **videos** and returns short, shareable URLs. It exists to give the
operator a private, low-friction way to share photos and video clips — the primary
use-case being GoPro footage clipped elsewhere (VidClip) and pasted as a direct
link into Discord, Google Chat, or SMS without depending on third-party hosting
limits.

## 2. Goals & non-goals

### Goals
- One codebase serving both images and videos with a single metadata store.
- Videos are served **as-is** (no re-encode) with HTTP Range/206 for seeking and
  inline previews.
- Rich link previews (OpenGraph) so a pasted URL renders as a card with a poster.
- Safe to expose publicly: writes are gated by API key and/or IP CIDR allowlist.
- Item size limit large enough for real clips (default 512 MB).

### Non-goals
- Transcoding / multi-codec delivery (the consumer supplies H.264 `.mp4` for
  maximum compatibility; MOV/HEVC is downloaded but may not preview inline).
- Accounts, quotas per user, or content moderation.
- Video streaming beyond simple HTTP file serving (no DASH/HLS).
- A production multi-worker WSGI deployment (single Flask process is adequate).

## 3. Architecture

```
Client ──HTTPS──▶ nginx (img.catbee.ca) ──proxy──▶ Flask app (127.0.0.1:5003)
                     │                                   │
                     │ client_max_body_size 600M         ├─ images/  <slug>.<ext>
                     │ proxy_buffering off               ├─ images/  <slug>.thumb.webp
                     │                                   └─ images/  images.bson (moofile)
```

- **Flask** exposes both a web UI (Jinja2 templates) and a JSON API.
- **moofile** is the embedded metadata store (BSON collection `images.bson`).
- **Pillow** renders image thumbnails; **ffmpeg** extracts video poster frames.
- Uploads stream to disk in 1 MB chunks — the whole file is never held in RAM.

## 4. Data model

Each media item is a moofile document keyed by unique `slug`:

| Field | Type | Notes |
|---|---|---|
| `slug` | str | 5 lowercase hex chars, unique, url-safe |
| `filename` | str | Original client filename |
| `extension` | str | Lowercased suffix incl. dot, e.g. `.mp4` |
| `mime_type` | str | Guessed MIME (`video/mp4`, `image/png`, …) |
| `file_size` | int | Bytes |
| `file_hash` | str | SHA-256 hex |
| `kind` | str | `image` \| `video` |
| `user` | str | `X-User` header or `anon` |
| `created_at` | str | ISO-8601 UTC |
| `unlisted` | bool | Hidden from gallery, still URL-addressable |
| `has_thumb` | bool | Whether a thumbnail/poster was generated |

Storage layout under `IMAGES_DIR`:

```
images.bson / images.bson.meta   — moofile store
<slug>.<ext>                     — original media bytes
<slug>.thumb.webp                — 300px thumbnail or ffmpeg poster
```

## 5. Media handling pipeline

1. `_allowed_file()` — extension allowlist **or** MIME starts with `image/`/`video/`.
2. Stream to `<slug>.<ext>` in 1 MB chunks, computing SHA-256 and enforcing
   `MAX_IMAGE_SIZE` mid-stream (aborts + unlinks on overflow; empty file rejected).
3. Determine `kind` from MIME/extension (`video` if `video/*` or a known video ext).
4. Thumbnail:
   - **image:** Pillow → 300×300 WebP (LANCZOS).
   - **video:** ffmpeg → `-ss 1 -frames:v 1` scaled poster WebP. Failure is silent
     (gallery falls back to the raw media URL via `onerror`).
5. Insert metadata record, return `slug`.

## 6. API

Auth (any of): `X-API-Key: <key>`, `Authorization: Bearer <key>`, `api_key=<key>`.

### `POST /api/upload`
Multipart form; file field named `file`, `image`, or `video`; optional
`unlisted=1|true|yes|on`.
- `201` on success with `{ok, slug, kind, url, direct_url, thumb_url, filename}`.
- `401` bad/missing key · `400` no file / bad type / empty · `413` too large.

### `GET /api/info/<slug>`
- `200` with full metadata + direct/thumb/download URLs; `404` unknown slug.

### `POST /api/delete/<slug>`
- `200 {ok:true, slug, deleted:true}`. Requires key. Removes file + thumb + record.

### CORS
`Access-Control-Allow-Origin: *` on all `/api/*` responses.

## 7. Web routes

| Route | Method | Behavior / auth |
|---|---|---|
| `/` | GET | Gallery, latest 50 public items |
| `/upload` | GET | Upload form |
| `/upload` | POST | Upload (IP allowlist) |
| `/<slug>` | GET | View page (video player / image + embed codes) |
| `/img/<slug>` | GET | Direct bytes, Range/206, `conditional=True` |
| `/thumb/<slug>` | GET | WebP thumbnail/poster |
| `/<slug>/download` | GET | `as_attachment=True` original |
| `/<slug>/delete` | POST | Delete (IP allowlist) |
| `/health` | GET | Status |

## 8. Configuration

All via env / `.env` — see README table. Key ordering/precedence: process env wins
over `.env`. `create_app(config)` accepts an explicit dict for tests.

## 9. Security

- Web UI writes restricted to `ALLOWED_POST_CIDR` (default localhost).
- API writes require a valid API key.
- Slugs: 5-char hex, validated strictly before DB/file access (`_valid_slug`).
- The gallery is read-only; only authenticated writes.
- Uploaded files are stored under a slug-named file with the original extension;
  `send_file` sets the Content-Type from stored MIME (no MIME sniffing of contents).
- Secrets (`.env`, `API_KEY`) are never committed (see `.gitignore`).

## 10. Limits & constraints

| Parameter | Value |
|---|---|
| Max upload | 512 MB (configurable; nginx `client_max_body_size` must exceed it) |
| Url slug length | 5 hex chars |
| Gallery page | 50 items (configurable) |
| Thumbnail | 300px WebP |
| RAM per upload | bounded (streams to disk, ~1 MB chunk) |

## 11. Deployment

- systemd unit `pengyshare.service` runs `pengyshare.sh`. Included in repo.
- nginx vhost `img.catbee.ca.nginx` shows the proxy config for `img.catbee.ca`
  (set `client_max_body_size` ≥ upload cap; `proxy_buffering off` for streaming).

## 12. Testing

Manual end-to-end verified 2026-08-31:
- H.264 mp4 upload → `kind: video`, WebP poster, `/img` serves `video/mp4` with
  `Accept-Ranges: bytes`, `Range: bytes=0-1023` → `206`.
- Image upload regression → `kind: image`, WebP thumb, PNG direct.
- Gallery renders video badges/play overlays alongside images.
