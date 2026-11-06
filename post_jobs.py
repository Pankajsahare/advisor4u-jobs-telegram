"""
Advisor4u jobs → Telegram channel auto-poster.
काम: Careers RSS feed वाचतो, नवीन jobs Telegram channel वर post करतो,
आणि post केलेल्यांची नोंद posted.json मध्ये ठेवतो (duplicate post टाळण्यासाठी).
"""
from __future__ import annotations

import html
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

# TODO [मॅन्युअल बदल]: jobs RSS feed URL (Step 7b मध्ये तपासलेला)
#   पर्याय: code न बदलता repo Settings → Variables मध्ये FEED_URL ठेवू शकता
#   चुकीचा असल्यास: "Feed fetch failed" error ने workflow थांबेल
DEFAULT_FEED_URL = "https://www.advisor4u.in/?post_type=awsm_job_openings&feed=rss2"  # ← येथे बदला

STATE_FILE = Path("posted.json")
MAX_POSTS_PER_RUN = 5  # एका run मध्ये जास्तीत जास्त posts (channel spam टाळण्यासाठी)
TIMEOUT_SECONDS = 30
USER_AGENT = "Mozilla/5.0 (compatible; Advisor4uJobsBot/1.0)"  # काही hosts default python UA block करतात


def load_posted_ids() -> set[str]:
    """आधी post केलेल्या jobs चे IDs वाचतो; file नसेल किंवा खराब असेल तर रिकामा set."""
    if not STATE_FILE.exists():
        return set()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return set(data) if isinstance(data, list) else set()
    except (json.JSONDecodeError, OSError):
        print("WARNING: posted.json unreadable, starting fresh")
        return set()


def save_posted_ids(ids: set[str]) -> None:
    """Post केलेल्या IDs ची यादी sorted स्वरूपात साठवतो."""
    STATE_FILE.write_text(
        json.dumps(sorted(ids), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def fetch_jobs(feed_url: str) -> list[dict[str, str]]:
    """RSS feed मधून खुल्या jobs ची यादी (id, title, link) काढतो."""
    request = urllib.request.Request(feed_url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read()
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Feed fetch failed: {exc}") from exc

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise RuntimeError(f"Feed is not valid XML: {exc}") from exc

    jobs: list[dict[str, str]] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or link).strip()
        # अपूर्ण item वगळा
        if title and link and guid:
            jobs.append({"id": guid, "title": title, "link": link})
    return jobs


def build_message(job: dict[str, str]) -> str:
    """Telegram post चा मजकूर (HTML format) तयार करतो."""
    title = html.escape(job["title"])
    link = html.escape(job["link"], quote=True)
    # TODO [user-facing text]: post चा मजकूर (English) — हवा तसा बदला
    return (
        "💼 <b>New Opening at Advisor4u</b>\n\n"
        f"<b>{title}</b>\n\n"
        f"👉 Apply here: {link}"
    )


def send_telegram(token: str, chat_id: str, text: str) -> None:
    """Telegram Bot API ने channel वर message पाठवतो; अपयश आल्यास RuntimeError."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    ).encode("utf-8")
    request = urllib.request.Request(url, data=payload)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram API error {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Telegram request failed: {exc.reason}") from exc

    if not result.get("ok"):
        raise RuntimeError(f"Telegram API returned not ok: {result}")


def main() -> int:
    """मुख्य flow: feed वाचा → नवीन jobs शोधा → post करा → नोंद साठवा."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    feed_url = os.environ.get("FEED_URL", "").strip() or DEFAULT_FEED_URL
    dry_run = os.environ.get("DRY_RUN", "false").strip().lower() == "true"

    if not dry_run and (not token or not chat_id):
        print("ERROR: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID secrets are missing")
        return 1

    try:
        jobs = fetch_jobs(feed_url)
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"Feed OK: {len(jobs)} open job(s) found")

    posted = load_posted_ids()
    # feed मध्ये नवीन jobs वर असतात — जुने आधी post व्हावेत म्हणून उलट क्रम
    new_jobs = [job for job in reversed(jobs) if job["id"] not in posted][:MAX_POSTS_PER_RUN]

    if not new_jobs:
        print("No new jobs. Nothing to post.")
        return 0

    failures = 0
    for job in new_jobs:
        if dry_run:
            print(f"[DRY RUN] Would post: {job['title']}")
            continue
        try:
            send_telegram(token, chat_id, build_message(job))
            posted.add(job["id"])
            print(f"Posted: {job['title']}")
        except RuntimeError as exc:
            failures += 1  # हा job नोंदवला नाही → पुढच्या run ला पुन्हा प्रयत्न
            print(f"ERROR posting '{job['title']}': {exc}")

    if not dry_run:
        save_posted_ids(posted)

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
