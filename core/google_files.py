"""Temporary Google Files API uploads for AI Coach audio analysis."""

from __future__ import annotations

import asyncio
import os
import time

import httpx


GOOGLE_FILE_MAX_BYTES = 2_000_000_000
GOOGLE_AUDIO_MAX_SECONDS = int(9.5 * 60 * 60)
CHUNK_BYTES = 1024 * 1024


async def upload_audio_file(
    path: str, mime_type: str, api_key: str, *, on_chunk=None,
) -> dict:
    size = os.path.getsize(path)
    if not 1 <= size <= GOOGLE_FILE_MAX_BYTES:
        raise ValueError("錄音超出 Google Files API 2GB 技術邊界")
    headers = {
        "x-goog-api-key": api_key,
        "X-Goog-Upload-Protocol": "resumable",
        "X-Goog-Upload-Command": "start",
        "X-Goog-Upload-Header-Content-Length": str(size),
        "X-Goog-Upload-Header-Content-Type": mime_type,
        "Content-Type": "application/json",
    }
    timeout = httpx.Timeout(120, connect=20)
    async with httpx.AsyncClient(timeout=timeout) as client:
        start = await client.post(
            "https://generativelanguage.googleapis.com/upload/v1beta/files",
            headers=headers,
            json={"file": {"display_name": "temporary-ai-coach-audio"}},
        )
        start.raise_for_status()
        upload_url = start.headers.get("X-Goog-Upload-URL")
        if not upload_url:
            raise RuntimeError("Google Files API 未回傳 upload URL")

        async def chunks():
            with open(path, "rb") as handle:
                while True:
                    chunk = await asyncio.to_thread(handle.read, CHUNK_BYTES)
                    if not chunk:
                        break
                    if on_chunk is not None:
                        on_chunk(len(chunk))
                    yield chunk

        uploaded = await client.post(
            upload_url,
            headers={
                "X-Goog-Upload-Offset": "0",
                "X-Goog-Upload-Command": "upload, finalize",
                "Content-Length": str(size),
            },
            content=chunks(),
        )
        uploaded.raise_for_status()
        payload = uploaded.json()
    file_data = payload.get("file") or payload
    if not file_data.get("name") or not file_data.get("uri"):
        raise RuntimeError("Google Files API 回傳資料不完整")
    return {**file_data, "uploaded_bytes": size}


async def wait_until_active(file_data: dict, api_key: str, timeout_seconds=180) -> dict:
    name = str(file_data.get("name") or "")
    deadline = time.monotonic() + max(1, int(timeout_seconds))
    current = dict(file_data)
    async with httpx.AsyncClient(timeout=30) as client:
        while time.monotonic() < deadline:
            state = str(current.get("state") or "").upper()
            if state == "ACTIVE":
                return current
            if state in {"FAILED", "ERROR"}:
                raise RuntimeError("Google 無法處理錄音檔案")
            await asyncio.sleep(2)
            response = await client.get(
                f"https://generativelanguage.googleapis.com/v1beta/{name}",
                headers={"x-goog-api-key": api_key},
            )
            response.raise_for_status()
            current = response.json()
    raise TimeoutError("Google 錄音處理逾時")


async def delete_file(file_data: dict | None, api_key: str) -> None:
    name = str((file_data or {}).get("name") or "")
    if not name:
        return
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.delete(
            f"https://generativelanguage.googleapis.com/v1beta/{name}",
            headers={"x-goog-api-key": api_key},
        )
        if response.status_code not in (200, 204, 404):
            response.raise_for_status()
