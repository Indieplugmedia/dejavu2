import os
import tempfile
from datetime import date, datetime
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from dejavu import Dejavu
from dejavu.logic.recognizer.file_recognizer import FileRecognizer

app = FastAPI(
    title="Dejavu Audio Fingerprinting API",
    description="Fingerprint songs and recognize radio/mic audio clips for Base44.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_djv: Optional[Dejavu] = None


def _db_config_from_env() -> dict:
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        parsed = urlparse(database_url)
        config = {
            "host": parsed.hostname or "localhost",
            "port": parsed.port or 5432,
            "user": unquote(parsed.username or "postgres"),
            "password": unquote(parsed.password or ""),
            "database": (parsed.path or "/dejavu").lstrip("/") or "dejavu",
        }
        query = parse_qs(parsed.query)
        sslmode = (query.get("sslmode") or [os.getenv("DB_SSLMODE", "prefer")])[0]
        config["sslmode"] = sslmode
        return config

    host = os.getenv("PGHOST") or os.getenv("DB_HOST")
    if not host:
        raise RuntimeError(
            "Set DATABASE_URL or PGHOST/PGUSER/PGPASSWORD/PGDATABASE for Postgres."
        )

    return {
        "host": host,
        "port": int(os.getenv("PGPORT") or os.getenv("DB_PORT") or 5432),
        "user": os.getenv("PGUSER") or os.getenv("DB_USER") or "postgres",
        "password": os.getenv("PGPASSWORD") or os.getenv("DB_PASSWORD") or "",
        "database": os.getenv("PGDATABASE") or os.getenv("DB_NAME") or "dejavu",
        "sslmode": os.getenv("DB_SSLMODE", "prefer"),
    }


def get_dejavu() -> Dejavu:
    global _djv
    if _djv is None:
        config = {
            "database": _db_config_from_env(),
            "database_type": os.getenv("DATABASE_TYPE", "postgres"),
            "fingerprint_limit": int(os.getenv("FINGERPRINT_LIMIT", "0")) or None,
        }
        _djv = Dejavu(config)
    return _djv


def require_api_key(x_api_key: Optional[str] = Header(default=None)):
    expected = os.getenv("API_KEY")
    if expected and x_api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


def _safe_name(name: Optional[str], fallback: str) -> str:
    base = os.path.basename(name or fallback)
    return base.replace("..", "_").replace("/", "_").replace("\\", "_") or fallback


def _write_upload(upload: UploadFile) -> str:
    suffix = os.path.splitext(upload.filename or "audio.bin")[1] or ".bin"
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    with open(path, "wb") as out:
        while True:
            chunk = upload.file.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
    return path


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


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/songs", dependencies=[Depends(require_api_key)])
def list_songs():
    djv = get_dejavu()
    return {"songs": _json_safe(djv.get_fingerprinted_songs())}


@app.post("/fingerprint", dependencies=[Depends(require_api_key)])
async def fingerprint_song(
    file: UploadFile = File(...),
    song_name: Optional[str] = Form(default=None),
):
    path = _write_upload(file)
    try:
        djv = get_dejavu()
        name = song_name or _safe_name(file.filename, "untitled")
        if name.lower().endswith((".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac")):
            name = os.path.splitext(name)[0]
        djv.fingerprint_file(path, song_name=name)
        return {"ok": True, "song_name": name}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if os.path.exists(path):
            os.remove(path)


@app.post("/recognize", dependencies=[Depends(require_api_key)])
async def recognize_song(file: UploadFile = File(...)):
    path = _write_upload(file)
    try:
        djv = get_dejavu()
        result = djv.recognize(FileRecognizer, path)
        return JSONResponse(content=_json_safe(result or {}))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if os.path.exists(path):
            os.remove(path)
