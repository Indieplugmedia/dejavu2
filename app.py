import os
import shutil
import tempfile
from datetime import date, datetime
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

import requests
from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from dejavu import Dejavu
from dejavu.logic.recognizer.file_recognizer import FileRecognizer

APP_VERSION = "2026-08-18-full-song-30s-sample"

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
        config = {
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
    else:
        config = {
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
    config["fingerprint_limit"] = 0
    return config


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
        "fingerprint_limit": 0,
        "recognize_sample_bytes": 500000,
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
        get_dejavu().fingerprint_file(dest, song_name=req.song_id)
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
        _download(req.audio_url, tmp_path, max_bytes=500_000)
        payload = _recognize_path(tmp_path)
        payload["version"] = APP_VERSION
        return JSONResponse(content=_json_safe(payload))
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
def recognize_bytes(file: UploadFile = File(...), authorization: str = Header(None)):
    require_auth(authorization)
    filename = file.filename or "clip.mp3"
    ext = _guess_ext(filename)
    tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
    tmp_path = tmp.name
    tmp.close()
    try:
        with open(tmp_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
        payload = _recognize_path(tmp_path)
        payload["version"] = APP_VERSION
        return JSONResponse(content=_json_safe(payload))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
