#!/usr/bin/env python3
"""PengyShare — a tiny self-hosted image **and video** hosting service with API."""

import hashlib
import ipaddress
import mimetypes
import os
import secrets
import subprocess
from datetime import datetime, timezone
from pathlib import Path

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, abort, jsonify, send_file
from moofile import Collection

# Video extensions we treat as video regardless of mime guess (handles .mov/.m4v quirks)
VIDEO_EXTS = {"mov", "mp4", "m4v", "webm", "mkv", "avi"}

# ---------------------------------------------------------------------------
# Config helpers (read from env / .env at import time for the default app)
# ---------------------------------------------------------------------------

def _read_config():
    """Return a dict of configuration from environment variables."""
    return {
        "images_dir": Path(os.getenv("IMAGES_DIR", "")).resolve()
                       or None,
        "max_image_size": int(os.getenv("MAX_IMAGE_SIZE", 512 * 1024 * 1024)),
        "allowed_extensions": os.getenv(
            "ALLOWED_EXTENSIONS",
            "jpg,jpeg,png,gif,webp,bmp,tiff,svg,mov,mp4,m4v,webm,mkv",
        ).lower().split(","),
        "base_url": os.getenv("BASE_URL", "http://localhost:5001").rstrip("/"),
        "images_per_page": int(os.getenv("IMAGES_PER_PAGE", "50")),
        "api_key": os.getenv("API_KEY", ""),
        "allowed_post_cidr": os.getenv("ALLOWED_POST_CIDR", "127.0.0.1/32"),
        "ffmpeg": os.getenv("FFMPEG_PATH", "ffmpeg"),
    }


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(config: dict | None = None):
    """Create and return a PengyShare Flask app."""
    if config is None:
        load_dotenv()
        cfg = _read_config()
    else:
        cfg = config

    app = Flask(__name__)
    app.config["PENGYSHARE"] = cfg

    images_dir: Path = Path(cfg["images_dir"]) if cfg.get("images_dir") else Path(__file__).resolve().parent / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    db = Collection(str(images_dir / "images.bson"), indexes=["slug", "unlisted"])

    max_image_size = cfg["max_image_size"]
    allowed_extensions = cfg["allowed_extensions"]
    base_url = cfg["base_url"]
    images_per_page = cfg["images_per_page"]
    api_key = cfg["api_key"]
    ffmpeg = cfg["ffmpeg"]

    # Parse allowed CIDRs
    parsed_cidrs: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for cidr in cfg["allowed_post_cidr"].split(","):
        cidr = cidr.strip()
        if cidr:
            parsed_cidrs.append(ipaddress.ip_network(cidr))

    # --- exceptions --------------------------------------------------------

    class UploadTooLarge(Exception):
        pass

    class EmptyUpload(Exception):
        pass

    # --- helpers ----------------------------------------------------------

    def _allowed_file(filename: str) -> bool:
        ext = Path(filename).suffix.lower().lstrip(".")
        if ext in (e.lstrip(".") for e in allowed_extensions):
            return True
        mime, _ = mimetypes.guess_type(filename)
        if mime and (mime.startswith("image/") or mime.startswith("video/")):
            return True
        return False

    def _get_upload_file():
        """Return the first uploaded file, whatever the form field is named."""
        for key in ("file", "image", "video", "media"):
            f = request.files.get(key)
            if f is not None:
                return f
        return None

    def _client_ip() -> str:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip.strip()
        return request.remote_addr or "127.0.0.1"

    def _ip_allowed(ip_str: str) -> bool:
        if not parsed_cidrs:
            return True
        try:
            addr = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        return any(addr in net for net in parsed_cidrs)

    def _api_key_valid() -> bool:
        if not api_key:
            return False
        header_key = request.headers.get("X-API-Key", "").strip()
        query_key = request.args.get("api_key", "").strip()
        auth_header = request.headers.get("Authorization", "").strip()
        bearer_key = ""
        if auth_header.lower().startswith("bearer "):
            bearer_key = auth_header[7:].strip()
        return header_key == api_key or query_key == api_key or bearer_key == api_key

    def _generate_slug() -> str:
        while True:
            slug = secrets.token_hex(3)[:5]
            if db.find_one({"slug": slug}) is None:
                return slug

    def _guess_kind(filename: str, ext: str, mime: str | None) -> str:
        if mime and mime.startswith("video/"):
            return "video"
        if mime and mime.startswith("image/"):
            return "image"
        if ext.lower().lstrip(".") in VIDEO_EXTS:
            return "video"
        return "image"

    def _make_thumb(src_path: Path, slug: str, kind: str, ext: str) -> bool:
        """Generate a 300px thumbnail/poster. Images use Pillow; videos use ffmpeg."""
        thumb_path = images_dir / f"{slug}.thumb.webp"
        if kind == "image":
            if HAS_PIL and ext.lower() not in (".svg",):
                try:
                    with Image.open(src_path) as img:
                        img.thumbnail((300, 300), Image.LANCZOS)
                        if img.mode in ("RGBA", "P"):
                            img = img.convert("RGB")
                        img.save(thumb_path, "WEBP", quality=80)
                    return thumb_path.exists()
                except Exception:
                    return False
            return False
        # video: extract a poster frame ~1s in via ffmpeg
        try:
            cmd = [ffmpeg, "-y", "-v", "error", "-ss", "1", "-i", str(src_path),
                   "-frames:v", "1", "-vf", "scale='min(300,iw)':-2", str(thumb_path)]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)
            return thumb_path.exists()
        except Exception:
            return False

    def _save_media(filename: str, stream, unlisted: bool = False) -> str:
        """Stream an uploaded file to disk as <slug><ext>, enforcing max size.

        Returns the slug. Raises UploadTooLarge / EmptyUpload on failure.
        """
        ext = Path(filename).suffix.lower() or ""
        if not ext:
            mime, _ = mimetypes.guess_type(filename)
            ext = ".mp4" if (mime and mime.startswith("video/")) else ".png"

        slug = _generate_slug()
        orig_path = images_dir / f"{slug}{ext}"

        hasher = hashlib.sha256()
        size = 0
        with orig_path.open("wb") as out:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_image_size:
                    raise UploadTooLarge()
                hasher.update(chunk)
                out.write(chunk)

        if size == 0:
            orig_path.unlink(missing_ok=True)
            raise EmptyUpload()

        file_hash = hasher.hexdigest()
        mime, _ = mimetypes.guess_type(filename)
        if not mime:
            mime = "application/octet-stream"
        kind = _guess_kind(filename, ext, mime)
        thumb_generated = _make_thumb(orig_path, slug, kind, ext)

        db.insert({
            "slug": slug,
            "filename": filename,
            "extension": ext,
            "mime_type": mime,
            "file_size": size,
            "file_hash": file_hash,
            "kind": kind,
            "user": request.headers.get("X-User", "anon"),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "unlisted": unlisted,
            "has_thumb": thumb_generated,
        })
        return slug

    def _load_meta(slug: str) -> dict | None:
        return db.find_one({"slug": slug})

    def _recent_media(limit: int = 50) -> list[dict]:
        return (
            db.find({"unlisted": False})
            .sort("created_at", descending=True)
            .limit(limit)
            .to_list()
        )

    def _format_size(size_bytes: int) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if size_bytes < 1024:
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024
        return f"{size_bytes:.1f} TB"

    def _valid_slug(slug: str) -> bool:
        return len(slug) <= 20 and all(c in "0123456789abcdef" for c in slug)

    # --- CORS & health ----------------------------------------------------

    @app.after_request
    def _add_cors_headers(response):
        if request.path.startswith("/api/"):
            response.headers["Access-Control-Allow-Origin"] = "*"
            response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key, Authorization"
        return response

    @app.route("/api/upload", methods=["OPTIONS"])
    def api_upload_options():
        return "", 204

    @app.route("/health")
    def health():
        return jsonify({
            "ok": True,
            "service": "PengyShare",
            "images_dir": str(images_dir),
            "images_dir_writable": os.access(images_dir, os.W_OK),
        })

    # --- routes -----------------------------------------------------------

    @app.route("/")
    def index():
        images = _recent_media(limit=images_per_page)
        can_delete = _ip_allowed(_client_ip())
        return render_template(
            "index.html",
            images=images,
            base_url=base_url,
            can_delete=can_delete,
            has_api=bool(api_key),
            format_size=_format_size,
        )

    @app.route("/upload", methods=["GET", "POST"])
    def upload_page():
        if request.method == "GET":
            return render_template("upload.html", base_url=base_url)

        ip = _client_ip()
        if not _ip_allowed(ip):
            abort(403, description=f"Uploading not allowed from {ip}")

        f = _get_upload_file()
        if f is None or not f.filename:
            return render_template("upload.html", base_url=base_url,
                                   error="No file selected."), 400
        if not _allowed_file(f.filename):
            return render_template(
                "upload.html", base_url=base_url,
                error=f"File type not allowed. Allowed: {', '.join(allowed_extensions)}"
            ), 400

        unlisted = bool(request.form.get("unlisted"))
        try:
            slug = _save_media(f.filename, f.stream, unlisted=unlisted)
        except UploadTooLarge:
            return render_template(
                "upload.html", base_url=base_url,
                error=f"File too large. Max is {_format_size(max_image_size)}."
            ), 413
        except EmptyUpload:
            return render_template("upload.html", base_url=base_url,
                                   error="Empty file."), 400
        return redirect(f"{base_url}/{slug}")

    @app.route("/api/upload", methods=["POST"])
    def api_upload():
        if not _api_key_valid():
            return jsonify({"ok": False, "error": "Invalid or missing API key"}), 401

        f = _get_upload_file()
        if f is None or not f.filename:
            return jsonify({"ok": False, "error": "No file field ('file'|'image'|'video') provided"}), 400

        if not _allowed_file(f.filename):
            return jsonify({
                "ok": False,
                "error": f"File type not allowed. Allowed: {', '.join(allowed_extensions)}"
            }), 400

        raw = (request.form.get("unlisted") or "0").strip().lower()
        unlisted = raw in ("1", "true", "yes", "on")
        try:
            slug = _save_media(f.filename, f.stream, unlisted=unlisted)
        except UploadTooLarge:
            return jsonify({
                "ok": False,
                "error": f"File too large. Max is {_format_size(max_image_size)}."
            }), 413
        except EmptyUpload:
            return jsonify({"ok": False, "error": "Empty file"}), 400

        meta = _load_meta(slug)
        return jsonify({
            "ok": True,
            "slug": slug,
            "kind": meta.get("kind", "image"),
            "url": f"{base_url}/{slug}",
            "direct_url": f"{base_url}/img/{slug}",
            "thumb_url": f"{base_url}/thumb/{slug}",
            "filename": f.filename,
        }), 201

    @app.route("/<slug>")
    def view_media(slug: str):
        if not _valid_slug(slug):
            abort(404)
        meta = _load_meta(slug)
        if meta is None:
            abort(404)
        can_delete = _ip_allowed(_client_ip())
        is_video = meta.get("kind") == "video" or (meta.get("mime_type") or "").startswith("video/")
        return render_template(
            "view_image.html",
            meta=meta,
            base_url=base_url,
            can_delete=can_delete,
            format_size=_format_size,
            is_video=is_video,
            has_thumb=bool(meta.get("has_thumb", False)),
        )

    @app.route("/img/<slug>")
    def serve_media(slug: str):
        if not _valid_slug(slug):
            abort(404)
        meta = _load_meta(slug)
        if meta is None:
            abort(404)
        ext = meta.get("extension", ".png")
        media_path = images_dir / f"{slug}{ext}"
        if not media_path.exists():
            abort(404)
        # send_file handles Range/206 for media players & Discord previews
        return send_file(
            str(media_path),
            mimetype=meta.get("mime_type", "application/octet-stream"),
            as_attachment=False,
            conditional=True,
        )

    @app.route("/thumb/<slug>")
    def serve_thumbnail(slug: str):
        if not _valid_slug(slug):
            abort(404)
        meta = _load_meta(slug)
        if meta is None:
            abort(404)
        thumb_path = images_dir / f"{slug}.thumb.webp"
        if not thumb_path.exists():
            abort(404)
        return send_file(
            str(thumb_path),
            mimetype="image/webp",
            as_attachment=False,
        )

    @app.route("/<slug>/download")
    def download_media(slug: str):
        if not _valid_slug(slug):
            abort(404)
        meta = _load_meta(slug)
        if meta is None:
            abort(404)
        ext = meta.get("extension", ".png")
        media_path = images_dir / f"{slug}{ext}"
        if not media_path.exists():
            abort(404)
        return send_file(
            str(media_path),
            mimetype=meta.get("mime_type", "application/octet-stream"),
            as_attachment=True,
            download_name=meta.get("filename", f"{slug}{ext}"),
        )

    @app.route("/api/info/<slug>")
    def api_info(slug: str):
        if not _valid_slug(slug):
            return jsonify({"ok": False, "error": "Not found"}), 404
        meta = _load_meta(slug)
        if meta is None:
            return jsonify({"ok": False, "error": "Not found"}), 404
        return jsonify({
            "ok": True,
            "slug": meta["slug"],
            "kind": meta.get("kind", "image"),
            "filename": meta["filename"],
            "extension": meta["extension"],
            "mime_type": meta["mime_type"],
            "file_size": meta["file_size"],
            "file_hash": meta["file_hash"],
            "created_at": meta["created_at"],
            "unlisted": meta.get("unlisted", False),
            "has_thumb": meta.get("has_thumb", False),
            "url": f"{base_url}/{slug}",
            "direct_url": f"{base_url}/img/{slug}",
            "thumb_url": f"{base_url}/thumb/{slug}",
            "download_url": f"{base_url}/{slug}/download",
        })

    @app.route("/api/delete/<slug>", methods=["POST"])
    def api_delete(slug: str):
        if not _api_key_valid():
            return jsonify({"ok": False, "error": "Invalid or missing API key"}), 401
        if not _valid_slug(slug):
            return jsonify({"ok": False, "error": "Not found"}), 404
        meta = db.find_one({"slug": slug})
        if meta is None:
            return jsonify({"ok": False, "error": "Not found"}), 404
        db.delete_one({"slug": slug})
        ext = meta.get("extension", ".png")
        (images_dir / f"{slug}{ext}").unlink(missing_ok=True)
        (images_dir / f"{slug}.thumb.webp").unlink(missing_ok=True)
        return jsonify({"ok": True, "slug": slug, "deleted": True})

    @app.route("/<slug>/delete", methods=["POST"])
    def delete_media(slug: str):
        if not _valid_slug(slug):
            abort(404)
        ip = _client_ip()
        if not _ip_allowed(ip):
            abort(403, description=f"Deletion not allowed from {ip}")
        meta = db.find_one({"slug": slug})
        if meta is None:
            abort(404)
        db.delete_one({"slug": slug})
        ext = meta.get("extension", ".png")
        (images_dir / f"{slug}{ext}").unlink(missing_ok=True)
        (images_dir / f"{slug}.thumb.webp").unlink(missing_ok=True)
        return redirect(url_for("index"))

    @app.errorhandler(404)
    def _not_found(e):
        return render_template(
            "index.html",
            images=_recent_media(limit=images_per_page),
            base_url=base_url,
            error="File not found.",
            has_api=bool(api_key),
        ), 404

    return app


# ---------------------------------------------------------------------------
# Default app (for direct execution / import)
# ---------------------------------------------------------------------------

load_dotenv()
app = create_app()


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    debug = os.getenv("DEBUG", "false").lower() == "true"
    port = int(os.getenv("PORT", "5001"))
    host = os.getenv("HOST", "0.0.0.0")
    cfg = app.config["PENGYSHARE"]
    print(f"Starting PengyShare on {host}:{port}")
    print(f"BASE_URL: {cfg['base_url']}")
    print(f"IMAGES_DIR: {cfg['images_dir']}")
    print(f"API_KEY: {'set' if cfg['api_key'] else 'NOT SET (API uploads disabled)'}")
    app.run(debug=debug, host=host, port=port)
