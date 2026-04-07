#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from data_sources import build_project_snapshot
from weekly_report_bot import generate_with_gemini


LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"


def load_config() -> dict[str, str]:
    load_dotenv()
    config = {
        "line_channel_secret": os.getenv("LINE_CHANNEL_SECRET", ""),
        "line_channel_access_token": os.getenv("LINE_CHANNEL_ACCESS_TOKEN", ""),
        "gemini_api_key": os.getenv("GEMINI_API_KEY", ""),
        "gemini_model": os.getenv("GEMINI_MODEL", "gemini-2.0-flash"),
    }
    return config


def verify_signature(channel_secret: str, body: bytes, signature: str | None) -> bool:
    if not signature:
        return False
    digest = hmac.new(
        channel_secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def build_assistant_reply(
    *,
    user_message: str,
    gemini_api_key: str,
    gemini_model: str,
) -> str:
    source_data = build_project_snapshot()
    system_instruction = (
        "You are the shared project assistant for the Nancy food event project. "
        "Answer in Japanese. "
        "Base your answer only on the current project data from Google Drive, Google Sheets, and the latest conversation log. "
        "If information is missing, say that it is not yet confirmed. "
        "Be practical and concise. "
        "When relevant, include current blockers, owners, and next actions."
    )
    user_prompt = (
        "ユーザーからの質問:\n"
        f"{user_message}\n\n"
        "以下が現在のプロジェクト情報です。これを元に回答してください。\n"
        f"{json.dumps(source_data, ensure_ascii=False, indent=2)}"
    )
    return generate_with_gemini(
        api_key=gemini_api_key,
        model=gemini_model,
        system_instruction=system_instruction,
        user_prompt=user_prompt,
    )


def reply_to_line(channel_access_token: str, reply_token: str, message_text: str) -> None:
    payload = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": message_text[:5000]}],
    }
    response = requests.post(
        LINE_REPLY_URL,
        headers={
            "Authorization": f"Bearer {channel_access_token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )
    response.raise_for_status()


app = FastAPI()


def get_config() -> dict[str, str]:
    config = load_config()
    missing = [
        name
        for name, value in (
            ("LINE_CHANNEL_SECRET", config["line_channel_secret"]),
            ("LINE_CHANNEL_ACCESS_TOKEN", config["line_channel_access_token"]),
            ("GEMINI_API_KEY", config["gemini_api_key"]),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}"
        )
    return config


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(
    request: Request,
    x_line_signature: str | None = Header(default=None),
) -> JSONResponse:
    config = get_config()
    body = await request.body()
    if not verify_signature(config["line_channel_secret"], body, x_line_signature):
        raise HTTPException(status_code=401, detail="Invalid LINE signature")

    payload = json.loads(body.decode("utf-8"))
    events = payload.get("events", [])
    if not events:
        return JSONResponse({"ok": True})

    for event in events:
        if event.get("type") != "message":
            continue
        message = event.get("message", {})
        if message.get("type") != "text":
            continue

        user_message = message.get("text", "").strip()
        if not user_message:
            continue

        reply_text = build_assistant_reply(
            user_message=user_message,
            gemini_api_key=config["gemini_api_key"],
            gemini_model=config["gemini_model"],
        )
        reply_to_line(
            channel_access_token=config["line_channel_access_token"],
            reply_token=event["replyToken"],
            message_text=reply_text,
        )

    return JSONResponse({"ok": True})


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run("line_chatbot:app", host="0.0.0.0", port=8000, reload=False)
