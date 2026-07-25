import base64
import os
import shutil
import tempfile
from datetime import date, datetime
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

import requests
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from dejavu import Dejavu
from dejavu.logic.recognizer.file_recognizer import FileRecognizer

APP_VERSION = "2026-07-25-recognize-bytes"

app = FastAPI(title="Indie Plug Dejavu Fingerprint Service", version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "")
_djv: Optional[Dejavu] = None


def get_db_config():
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        u = urlparse(database_url)
        query = parse_qs(u.query)
        sslmode = (query.get("sslmode") or [os.environ.get("DB_SSLMODE", "prefer")])[0]
        return {
            "database": {
                "host": u.hostname or "localhost",
                "user": unquote(u.username or "postgres"),
                "password": unquote(u.password or ""),
                "database": (u.path[1:] if u.path and len(u.path) > 1 else "dejavu"),
                "port": int(u.port or 5432),
                "sslmode": sslmode,
            },
            "database_type": "postgres",
        }

    return {
        "database": {
            "host": os.environ.get("DB_HOST") or os.environ.get("PGHOST", "localhost"),
            "user": os.environ.get("DB_USER") or os.environ.get("PGUSER", "postgres"),
            "password": os.environ.get("DB_PASSWORD") or os.environ.get("PGPASSWORD", ""),
            "database": os.environ.get("DB_NAME") or os.environ.get("PGDATABASE", "dejavu"),
            "port": int(os.environ.get("DB_PORT") or os.environ.get("PGPORT", "5432")),
            "sslmode": os.environ.get("DB_SSLMODE", "prefer"),
        },
        "database_type": "postgres",
    }


def get_dejavu() -> Dejavu:
    global _djv
    if _djv is None:
        _djv = Dejavu(get_db_config())
    return _djv


def require_auth(authorization: str = Header(None)):
    if not AUTH_TOKEN:
        return
    token = (authorization or "").replace("Bearer ", "").strip()
    if token != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:
            pass
    return str(value)


class IndexReq(BaseModel):
    song_id: str
    audio_url: str


class RecognizeReq(BaseModel):
    audio_url: str


def _download(url: str, dest_path: str, max_bytes: int) -> None:
    with requests.get(
        url, stream=True, timeout=60, headers={"User-Agent": "IndiePlugDejavu/1.0"}
    ) as r:
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            written = 0
            for chunk in r.iter_content(8192):
                if not chunk:
                    continue
                f.write(chunk)
                written += len(chunk)
                if written >= max_bytes:
                    break


def _guess_ext(name_or_url: str) -> str:
    path = urlparse(name_or_url).path.lower() if "://" in name_or_url else name_or_url.lower()
    for ext in (".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac", ".webm"):
        if path.endswith(ext):
            return ext
    return ".mp3"


def _recognize_path(tmp_path: str) -> dict:
    djv = get_dejavu()
    raw = djv.recognize(FileRecognizer, tmp_path) or {}
    results = raw.get("results") or []
    top = results[0] if results else None

    if not top:
        return {
            "matched": False,
            "song_id": None,
            "confidence": 0,
            "offset": 0,
            "offset_seconds": 0,
            "match_time": raw.get("total_time", 0),
            "results": [],
        }

    song_id = top.get("song_name")
    if isinstance(song_id, bytes):
        song_id = song_id.decode("utf-8", errors="ignore")

    confidence = top.get("input_confidence", 0) or 0
    return {
        "matched": True,
        "song_id": song_id,
        "confidence": confidence,
        "offset": top.get("offset", 0),
        "offset_seconds": top.get("offset_seconds", 0),
        "match_time": raw.get("total_time", 0),
        "hashes_matched_in_input": top.get("hashes_matched_in_input", 0),
        "fingerprinted_confidence": top.get("fingerprinted_confidence", 0),
        "results": results,
    }


@app.get("/")
def root():
    return {
        "service": "Indie Plug Dejavu Fingerprint Service",
        "version": APP_VERSION,
        "routes": [
            "GET /health",
            "GET /",
            "POST /index",
            "POST /recognize",
            "POST /recognize_bytes",
        ],
    }


@app.get("/health")
def health():
    return {"status": "ok", "version": APP_VERSION}


@app.post("/index")
def index_song(req: IndexReq, authorization: str = Header(None)):
    require_auth(authorization)
    workdir = tempfile.mkdtemp(prefix="dejavu_idx_")
    try:
        ext = _guess_ext(req.audio_url)
        dest = os.path.join(workdir, f"{req.song_id}{ext}")
        _download(req.audio_url, dest, max_bytes=50_000_000)
        djv = get_dejavu()
        djv.fingerprint_file(dest, song_name=req.song_id)
        return {"ok": True, "song_id": req.song_id, "version": APP_VERSION}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@app.post("/recognize")
def recognize_stream(req: RecognizeReq, authorization: str = Header(None)):
    require_auth(authorization)
    ext = _guess_ext(req.audio_url)
    tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
    tmp_path = tmp.name
    tmp.close()
    try:
        _download(req.audio_url, tmp_path, max_bytes=300_000)
        return JSONResponse(content=_json_safe(_recognize_path(tmp_path)))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


@app.post("/recognize_bytes")
async def recognize_bytes(
    request: Request,
    authorization: str = Header(None),
):
    require_auth(authorization)

    data = b""
    filename = "clip.mp3"
    content_type = (request.headers.get("content-type") or "").lower()

    try:
        if "multipart/form-data" in content_type:
            form = await request.form()
            upload = form.get("file") or form.get("audio") or form.get("clip")
            if upload is None:
                raise HTTPException(
                    status_code=400, detail="multipart field file/audio/clip required"
                )
            data = await upload.read()
            filename = getattr(upload, "filename", None) or filename
        elif "application/json" in content_type:
            body = await request.json()
            b64 = body.get("audio_base64") or body.get("audio_b64") or body.get("bytes")
            if not b64:
                raise HTTPException(status_code=400, detail="audio_base64 required")
            if isinstance(b64, str) and "," in b64 and b64.strip().startswith("data:"):
                b64 = b64.split(",", 1)[1]
            data = base64.b64decode(b64)
            filename = body.get("filename") or filename
        else:
            data = await request.body()
            if not data:
                raise HTTPException(status_code=400, detail="empty body")

        if not data:
            raise HTTPException(status_code=400, detail="no audio bytes received")

        ext = _guess_ext(filename)
        tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
        tmp_path = tmp.name
        tmp.write(data)
        tmp.close()
        try:
            payload = _recognize_path(tmp_path)
            payload["version"] = APP_VERSION
            return JSONResponse(content=_json_safe(payload))
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
