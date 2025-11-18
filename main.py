import os
import io
import json
from typing import Optional, Dict, Any

import requests
from fastapi import FastAPI, HTTPException, Header, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse

# ---- Config ----
SUNO_BASE_URL = os.getenv("SUNO_BASE_URL", "https://sunoapi.org/api")

app = FastAPI(title="Suno Proxy API", version="1.0.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)


def _resolve_api_key(header_key: Optional[str], query_key: Optional[str]) -> str:
    api_key = (header_key or "").strip() or (query_key or "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="Missing API key (send in 'x-suno-api-key' header or 'api_key' query param)")
    return api_key


def _suno_headers(api_key: str) -> Dict[str, str]:
    # Some APIs use x-api-key, others use Authorization. We'll send both safely.
    return {
        "x-api-key": api_key,
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }


@app.get("/")
def read_root():
    return {"message": "Suno Proxy API running", "upstream": SUNO_BASE_URL}


@app.post("/generate")
async def generate(
    request: Request,
    x_suno_api_key: Optional[str] = Header(default=None, convert_underscores=True),
    api_key: Optional[str] = Query(default=None)
):
    """Forward prompt + model + callback URL to Suno API and return the job info.
    Expects JSON body: { prompt, model, callback_url }
    """
    resolved_key = _resolve_api_key(x_suno_api_key, api_key)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    prompt = (body or {}).get("prompt", "").strip()
    model = (body or {}).get("model", "").strip()
    callback_url = (body or {}).get("callback_url", "").strip()

    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required")
    if len(prompt) > 500:
        raise HTTPException(status_code=400, detail="Prompt must be <= 500 characters")
    if not model:
        raise HTTPException(status_code=400, detail="Model is required")

    payload = {
        "prompt": prompt,
        "model": model
    }
    if callback_url:
        payload["callback_url"] = callback_url

    try:
        resp = requests.post(
            f"{SUNO_BASE_URL}/generate",
            headers=_suno_headers(resolved_key),
            data=json.dumps(payload),
            timeout=60
        )
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Upstream error: {str(e)}")

    if not resp.ok:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

    return JSONResponse(content=resp.json())


@app.get("/status")
async def status(
    id: str = Query(..., description="Generation or track id"),
    x_suno_api_key: Optional[str] = Header(default=None, convert_underscores=True),
    api_key: Optional[str] = Query(default=None)
):
    resolved_key = _resolve_api_key(x_suno_api_key, api_key)

    try:
        resp = requests.get(
            f"{SUNO_BASE_URL}/status",
            headers=_suno_headers(resolved_key),
            params={"id": id},
            timeout=30
        )
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Upstream error: {str(e)}")

    if not resp.ok:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

    return JSONResponse(content=resp.json())


@app.get("/lyrics")
async def lyrics(
    id: str = Query(..., description="Track id"),
    x_suno_api_key: Optional[str] = Header(default=None, convert_underscores=True),
    api_key: Optional[str] = Query(default=None)
):
    resolved_key = _resolve_api_key(x_suno_api_key, api_key)

    try:
        resp = requests.get(
            f"{SUNO_BASE_URL}/lyrics",
            headers=_suno_headers(resolved_key),
            params={"id": id},
            timeout=30
        )
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Upstream error: {str(e)}")

    if not resp.ok:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

    return JSONResponse(content=resp.json())


@app.get("/stream")
async def stream(
    id: str = Query(..., description="Track id to stream"),
    x_suno_api_key: Optional[str] = Header(default=None, convert_underscores=True),
    api_key: Optional[str] = Query(default=None)
):
    resolved_key = _resolve_api_key(x_suno_api_key, api_key)

    try:
        upstream = requests.get(
            f"{SUNO_BASE_URL}/stream",
            headers=_suno_headers(resolved_key),
            params={"id": id},
            timeout=60,
            stream=True
        )
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Upstream error: {str(e)}")

    if not upstream.ok:
        raise HTTPException(status_code=upstream.status_code, detail=upstream.text)

    def iter_stream():
        for chunk in upstream.iter_content(chunk_size=1024 * 32):
            if chunk:
                yield chunk

    return StreamingResponse(iter_stream(), media_type="audio/mpeg")


@app.get("/download")
async def download(
    id: str = Query(..., description="Track id to download"),
    x_suno_api_key: Optional[str] = Header(default=None, convert_underscores=True),
    api_key: Optional[str] = Query(default=None)
):
    resolved_key = _resolve_api_key(x_suno_api_key, api_key)

    try:
        upstream = requests.get(
            f"{SUNO_BASE_URL}/download",
            headers=_suno_headers(resolved_key),
            params={"id": id},
            timeout=120
        )
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Upstream error: {str(e)}")

    if not upstream.ok:
        raise HTTPException(status_code=upstream.status_code, detail=upstream.text)

    filename = f"suno-{id}.mp3"
    data = io.BytesIO(upstream.content)
    headers = {"Content-Disposition": f"attachment; filename={filename}"}
    return StreamingResponse(data, media_type="audio/mpeg", headers=headers)


# Basic in-memory store to showcase callback processing (ephemeral)
_CALLBACK_STORE: Dict[str, Any] = {}


@app.post("/callback")
async def callback_handler(payload: Dict[str, Any]):
    # Store and acknowledge; in production, persist to DB/queue
    track_id = str(payload.get("id") or payload.get("track_id") or payload.get("job_id") or "unknown")
    _CALLBACK_STORE[track_id] = payload
    return {"received": True, "id": track_id}


@app.get("/callback/store")
async def callback_store_get(id: Optional[str] = None):
    if id:
        return _CALLBACK_STORE.get(id) or {"error": "not_found"}
    return {"count": len(_CALLBACK_STORE), "items": list(_CALLBACK_STORE.keys())}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
