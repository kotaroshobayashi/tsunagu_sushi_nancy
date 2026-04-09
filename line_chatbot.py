#!/usr/bin/env python3
from __future__ import annotations

import base64
from datetime import datetime
import hashlib
import hmac
import json
import logging
import os

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from zoneinfo import ZoneInfo

from data_sources import build_project_snapshot
from weekly_report_bot import generate_weekly_report, load_settings, send_line_message


LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
JST = ZoneInfo("Asia/Tokyo")


def load_config() -> dict[str, str]:
    load_dotenv()
    config = {
        "line_channel_secret": os.getenv("LINE_CHANNEL_SECRET", ""),
        "line_channel_access_token": os.getenv("LINE_CHANNEL_ACCESS_TOKEN", ""),
        "gemini_api_key": os.getenv("GEMINI_API_KEY", ""),
        "gemini_model": os.getenv("GEMINI_MODEL", "gemini-2.0-flash"),
        "cron_secret": os.getenv("CRON_SECRET", ""),
        "test_report_date": os.getenv("TEST_REPORT_DATE", ""),
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
    if not response.ok:
        logger.error(
            "LINE reply failed: status=%s body=%s",
            response.status_code,
            response.text,
        )
        response.raise_for_status()


def push_to_line(channel_access_token: str, to: str, message_text: str) -> None:
    payload = {
        "to": to,
        "messages": [{"type": "text", "text": message_text[:5000]}],
    }
    response = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={
            "Authorization": f"Bearer {channel_access_token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )
    if not response.ok:
        logger.error(
            "LINE push failed: status=%s body=%s",
            response.status_code,
            response.text,
        )
        response.raise_for_status()


def verify_cron_secret(auth_header: str | None, cron_secret: str) -> bool:
    if not cron_secret:
        return True
    return auth_header == f"Bearer {cron_secret}"


app = FastAPI()
logger = logging.getLogger(__name__)


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

        source = event.get("source", {})
        logger.info(
            f"LINE source received: "
            f"type={source.get('type')} "
            f"userId={source.get('userId')} "
            f"groupId={source.get('groupId')} "
            f"roomId={source.get('roomId')}"
        )

        user_message = message.get("text", "").strip()
        if not user_message:
            continue

        if user_message.lower() in {"id", "userid", "whoami", "target"}:
            reply_to_line(
                channel_access_token=config["line_channel_access_token"],
                reply_token=event["replyToken"],
                message_text=(
                    "LINE source info\n"
                    f"type={source.get('type')}\n"
                    f"userId={source.get('userId')}\n"
                    f"groupId={source.get('groupId')}\n"
                    f"roomId={source.get('roomId')}"
                ),
            )
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


@app.get("/cron/test-weekly")
def cron_test_weekly(authorization: str | None = Header(default=None)) -> JSONResponse:
    config = get_config()
    if not verify_cron_secret(authorization, config["cron_secret"]):
        raise HTTPException(status_code=401, detail="Unauthorized cron invocation")

    today_jst = datetime.now(JST).date().isoformat()
    test_report_date = config["test_report_date"]
    if test_report_date and today_jst != test_report_date:
        return JSONResponse(
            {
                "ok": True,
                "skipped": True,
                "reason": "test date mismatch",
                "today_jst": today_jst,
                "expected_date": test_report_date,
            }
        )

    settings = load_settings()
    source_data = {
        "generated_at": datetime.now(settings.timezone).isoformat(),
        **build_project_snapshot(),
    }
    report = generate_weekly_report(settings, source_data)
    test_message = f"【テスト送信】{today_jst} 13:50実行予定の確認です。\n\n{report}"
    send_line_message(settings, test_message)

    return JSONResponse(
        {"ok": True, "sent": True, "today_jst": today_jst, "target": "LINE_TARGET_ID"}
    )


@app.get("/debug/push-test")
def debug_push_test(authorization: str | None = Header(default=None)) -> JSONResponse:
    config = get_config()
    if not verify_cron_secret(authorization, config["cron_secret"]):
        raise HTTPException(status_code=401, detail="Unauthorized debug invocation")

    settings = load_settings()
    push_to_line(
        channel_access_token=settings.line_channel_access_token,
        to=settings.line_target_id,
        message_text="debug push test from vercel",
    )
    return JSONResponse({"ok": True, "sent": True})


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run("line_chatbot:app", host="0.0.0.0", port=8000, reload=False)
