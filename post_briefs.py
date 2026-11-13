"""
Advisor4u Market Brief → Telegram channel + Facebook Page auto-poster.
काम: Market Brief category चा RSS feed वाचतो, नवीन posts (Morning / Close / Weekly Wrap)
Telegram आणि Facebook वर पाठवतो, आणि पाठवलेल्यांची नोंद posted_briefs.json मध्ये ठेवतो.

पहिल्या run ला (posted_briefs.json नसताना) feed मधले सध्याचे posts फक्त "पाठवले" म्हणून
नोंदवले जातात, पाठवले जात नाहीत — म्हणजे जुन्या posts चा channel वर पूर येणार नाही.
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

# code न बदलता repo Settings → Variables मध्ये BRIEF_FEED_URL ठेवू शकता
DEFAULT_FEED_URL = "https://www.advisor4u.in/category/market-brief/feed/"

STATE_FILE = Path("posted_briefs.json")
MAX_POSTS_PER_RUN = 3          # एका run मध्ये जास्तीत जास्त posts (channel spam टाळण्यासाठी)
EXCERPT_CHARS = 260            # सारांशाची कमाल लांबी
TIMEOUT_SECONDS = 30
USER_AGENT = "Mozilla/5.0 (compatible; Advisor4uBriefBot/1.0)"  # काही hosts default python UA block करतात
GRAPH_VERSION_DEFAULT = "v23.0"  # Facebook Graph API version (repo Variables मध्ये FB_GRAPH_VERSION ने बदलता येतो)


def load_state() -> set[str] | None:
    """पाठवलेल्या posts चे IDs. File नसेल तर None (= पहिला run)."""
    if not STATE_FILE.exists():
        return None
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return set(data) if isinstance(data, list) else set()
    except (json.JSONDecodeError, OSError):
        print("WARNING: posted_briefs.json unreadable, starting fresh")
        return set()


def save_state(ids: set[str]) -> None:
    """IDs sorted स्वरूपात साठवतो (दिवसाला ~2 ओळी, त्यामुळे file लहानच राहते)."""
    STATE_FILE.write_text(json.dumps(sorted(ids), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def clean_text(raw: str) -> str:
    """HTML tags/entities काढून साधा, एका ओळीचा मजकूर."""
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    # WordPress excerpt च्या शेवटी येणारे "The post … appeared first on …" काढा
    text = re.sub(r"The post .* appeared first on .*$", "", text).strip()
    return text


def shorten(text: str, limit: int) -> str:
    """शब्द न तोडता मजकूर लहान करतो."""
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",.;:-")
    return cut + "…"


def fetch_posts(feed_url: str) -> list[dict[str, str]]:
    """RSS feed मधून posts ची यादी (id, title, link, excerpt) — नवीन आधी."""
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

    posts: list[dict[str, str]] = []
    for item in root.iter("item"):
        title = clean_text(item.findtext("title") or "")
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or link).strip()
        excerpt = shorten(clean_text(item.findtext("description") or ""), EXCERPT_CHARS)
        if title and link and guid:  # अपूर्ण item वगळा
            posts.append({"id": guid, "title": title, "link": link, "excerpt": excerpt})
    return posts


def emoji_for(title: str) -> str:
    """Post प्रकारानुसार emoji."""
    t = title.lower()
    if "weekly" in t:
        return "🗓️"
    if "close" in t:
        return "🔔"
    return "📊"


def telegram_message(post: dict[str, str]) -> str:
    """Telegram post (HTML format). User-facing → English."""
    # TODO [user-facing text]: Telegram post चा शेवटचा CTA — हवा तसा बदला
    parts = [f"{emoji_for(post['title'])} <b>{html.escape(post['title'])}</b>"]
    if post["excerpt"]:
        parts.append(html.escape(post["excerpt"]))
    parts.append(f"👉 Read full brief: {html.escape(post['link'], quote=True)}")
    parts.append("💬 Questions? WhatsApp us: https://wa.me/919405908605")
    return "\n\n".join(parts)


def facebook_message(post: dict[str, str]) -> str:
    """Facebook post चा मजकूर (link preview आपोआप येतो). User-facing → English."""
    lines = [f"{emoji_for(post['title'])} {post['title']}"]
    if post["excerpt"]:
        lines.append(post["excerpt"])
    # TODO [user-facing text]: Facebook hashtags — हवे तसे बदला
    lines.append("#StockMarket #Sensex #Nifty #MutualFunds #SahareWealth")
    return "\n\n".join(lines)


def http_post(url: str, fields: dict[str, str], label: str) -> dict:
    """Form POST पाठवून JSON उत्तर परत देतो; अपयश आल्यास RuntimeError (token log मध्ये येत नाही)."""
    data = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"{label} API error {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{label} request failed: {exc.reason}") from exc


def send_telegram(token: str, chat_id: str, text: str) -> None:
    """Telegram channel वर message."""
    result = http_post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        {"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
        "Telegram",
    )
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API returned not ok: {result}")


def send_facebook(page_id: str, page_token: str, version: str, message: str, link: str) -> None:
    """Facebook Page वर link post."""
    result = http_post(
        f"https://graph.facebook.com/{version}/{page_id}/feed",
        {"message": message, "link": link, "access_token": page_token},
        "Facebook",
    )
    if "id" not in result:
        raise RuntimeError(f"Facebook API returned no post id: {result}")


def main() -> int:
    """मुख्य flow: feed वाचा → नवीन posts शोधा → Telegram + Facebook → नोंद साठवा."""
    env = os.environ
    tg_token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    # Market Brief साठी वेगळा channel असल्यास BRIEF_TELEGRAM_CHAT_ID; नसेल तर jobs चाच channel
    tg_chat = env.get("BRIEF_TELEGRAM_CHAT_ID", "").strip() or env.get("TELEGRAM_CHAT_ID", "").strip()
    fb_page = env.get("FB_PAGE_ID", "").strip()
    fb_token = env.get("FB_PAGE_TOKEN", "").strip()
    fb_version = env.get("FB_GRAPH_VERSION", "").strip() or GRAPH_VERSION_DEFAULT
    feed_url = env.get("BRIEF_FEED_URL", "").strip() or DEFAULT_FEED_URL
    dry_run = env.get("DRY_RUN", "false").strip().lower() == "true"

    use_tg = bool(tg_token and tg_chat)
    use_fb = bool(fb_page and fb_token)
    if not dry_run and not (use_tg or use_fb):
        print("ERROR: no channel configured (Telegram or Facebook secrets missing)")
        return 1
    print(f"Channels: telegram={'on' if use_tg else 'off'}, facebook={'on' if use_fb else 'off'}")

    try:
        posts = fetch_posts(feed_url)
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"Feed OK: {len(posts)} post(s) in feed")

    posted = load_state()
    if posted is None:
        # पहिला run: सध्याचे posts फक्त नोंदवा, पाठवू नका
        if not dry_run:
            save_state({p["id"] for p in posts})
        print(f"First run: marked {len(posts)} existing post(s) as already posted. Nothing sent.")
        return 0

    new_posts = [p for p in reversed(posts) if p["id"] not in posted][:MAX_POSTS_PER_RUN]
    if not new_posts:
        print("No new briefs. Nothing to post.")
        return 0

    failures = 0
    for post in new_posts:
        if dry_run:
            print(f"[DRY RUN] Would post: {post['title']}\n---\n{telegram_message(post)}\n---")
            continue
        ok_any = False
        if use_tg:
            try:
                send_telegram(tg_token, tg_chat, telegram_message(post))
                ok_any = True
                print(f"Telegram posted: {post['title']}")
            except RuntimeError as exc:
                print(f"ERROR Telegram '{post['title']}': {exc}")
        if use_fb:
            try:
                send_facebook(fb_page, fb_token, fb_version, facebook_message(post), post["link"])
                ok_any = True
                print(f"Facebook posted: {post['title']}")
            except RuntimeError as exc:
                print(f"ERROR Facebook '{post['title']}': {exc}")
        if ok_any:
            posted.add(post["id"])  # एका channel वर तरी गेलं → पुन्हा पाठवू नका (duplicate टाळणे)
        else:
            failures += 1           # कुठेच गेलं नाही → पुढच्या run ला पुन्हा प्रयत्न

    if not dry_run:
        save_state(posted)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
