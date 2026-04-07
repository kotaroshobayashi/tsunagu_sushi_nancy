#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

from data_sources import build_project_snapshot


JST = ZoneInfo("Asia/Tokyo")
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MAX_LINE_TEXT_LENGTH = 5000


@dataclass
class Settings:
    gemini_api_key: str
    gemini_model: str
    line_channel_access_token: str
    line_target_id: str
    report_day: int
    report_hour: int
    report_minute: int
    timezone: ZoneInfo


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a weekly project report from local files and push it to LINE."
    )
    parser.add_argument(
        "--run-once",
        action="store_true",
        help="Generate and send one report immediately.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate the report and print it without sending to LINE.",
    )
    return parser.parse_args()


def load_settings() -> Settings:
    load_dotenv()
    gemini_api_key = require_env("GEMINI_API_KEY")
    line_channel_access_token = require_env("LINE_CHANNEL_ACCESS_TOKEN")
    line_target_id = require_env("LINE_TARGET_ID")

    return Settings(
        gemini_api_key=gemini_api_key,
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.0-flash"),
        line_channel_access_token=line_channel_access_token,
        line_target_id=line_target_id,
        report_day=int(os.getenv("REPORT_DAY", "0")),  # Monday
        report_hour=int(os.getenv("REPORT_HOUR", "22")),
        report_minute=int(os.getenv("REPORT_MINUTE", "0")),
        timezone=ZoneInfo(os.getenv("REPORT_TIMEZONE", "Asia/Tokyo")),
    )


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def build_report_input(settings: Settings) -> dict[str, Any]:
    now = datetime.now(settings.timezone)
    snapshot = build_project_snapshot()

    return {
        "generated_at": now.isoformat(),
        **snapshot,
    }


def generate_weekly_report(settings: Settings, source_data: dict[str, Any]) -> str:
    instructions = (
        "You are an operations assistant for a Japanese/French food event project. "
        "Create a concise weekly report in Japanese for a LINE group. "
        "Use exact dates where possible. "
        "Focus on current status, application progress, blockers, and next actions. "
        "Do not invent facts. If something is unclear, say that it is not yet confirmed. "
        "Keep the report readable in LINE and under 3500 Japanese characters. "
        "Use this structure exactly:\n"
        "【今週の要点】\n"
        "【申請・書類の進捗】\n"
        "【未解決事項】\n"
        "【来週のアクション】\n"
        "【担当別メモ】"
    )

    prompt = (
        "以下のプロジェクト情報を元に、週次レポートを作ってください。\n\n"
        f"{json.dumps(source_data, ensure_ascii=False, indent=2)}"
    )
    return generate_with_gemini(
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
        system_instruction=instructions,
        user_prompt=prompt,
    )


def send_line_message(settings: Settings, text: str) -> None:
    headers = {
        "Authorization": f"Bearer {settings.line_channel_access_token}",
        "Content-Type": "application/json",
    }

    chunks = split_for_line(text)
    for chunk in chunks:
        payload = {
            "to": settings.line_target_id,
            "messages": [{"type": "text", "text": chunk}],
        }
        retry_key = str(uuid.uuid4())
        response = requests.post(
            LINE_PUSH_URL,
            headers={**headers, "X-Line-Retry-Key": retry_key},
            json=payload,
            timeout=30,
        )
        response.raise_for_status()


def generate_with_gemini(
    *,
    api_key: str,
    model: str,
    system_instruction: str,
    user_prompt: str,
) -> str:
    url = GEMINI_API_URL.format(model=model)
    payload = {
        "systemInstruction": {
            "parts": [{"text": system_instruction}],
        },
        "contents": [
            {
                "role": "user",
                "parts": [{"text": user_prompt}],
            }
        ],
        "generationConfig": {
            "temperature": 0.4,
            "topP": 0.9,
            "maxOutputTokens": 2200,
        },
    }
    response = requests.post(
        url,
        params={"key": api_key},
        headers={"Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()

    candidates = data.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"Gemini returned no candidates: {data}")

    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(part.get("text", "") for part in parts).strip()
    if not text:
        raise RuntimeError(f"Gemini returned empty text: {data}")
    return text


def split_for_line(text: str) -> list[str]:
    if len(text) <= MAX_LINE_TEXT_LENGTH:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > MAX_LINE_TEXT_LENGTH:
            if current:
                chunks.append(current.rstrip())
            current = line
        else:
            current += line

    if current.strip():
        chunks.append(current.rstrip())
    return chunks


def next_run_at(settings: Settings, now: datetime | None = None) -> datetime:
    now = now or datetime.now(settings.timezone)
    candidate = now.replace(
        hour=settings.report_hour,
        minute=settings.report_minute,
        second=0,
        microsecond=0,
    )

    days_ahead = (settings.report_day - candidate.weekday()) % 7
    candidate = candidate + timedelta(days=days_ahead)
    if candidate <= now:
        candidate = candidate + timedelta(days=7)
    return candidate


def sleep_until(target: datetime) -> None:
    while True:
        now = datetime.now(target.tzinfo)
        remaining = (target - now).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 60))


def run_once(settings: Settings, dry_run: bool) -> None:
    source_data = build_report_input(settings)
    report = generate_weekly_report(settings, source_data)

    if dry_run:
        print(report)
        return

    send_line_message(settings, report)
    logging.info("Weekly report sent to LINE.")


def run_scheduler(settings: Settings, dry_run: bool) -> None:
    while True:
        target = next_run_at(settings)
        logging.info("Next weekly report scheduled for %s", target.isoformat())
        sleep_until(target)
        try:
            run_once(settings, dry_run=dry_run)
        except Exception as exc:
            logging.exception("Failed to send weekly report: %s", exc)


def main() -> int:
    args = parse_args()
    configure_logging()

    try:
        settings = load_settings()
        if args.run_once:
            run_once(settings, dry_run=args.dry_run)
        else:
            run_scheduler(settings, dry_run=args.dry_run)
    except KeyboardInterrupt:
        logging.info("Stopped by user.")
        return 130
    except Exception as exc:
        logging.exception("Bot stopped with error: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
