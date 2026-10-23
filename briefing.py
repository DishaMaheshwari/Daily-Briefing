#!/usr/bin/env python3
"""
Daily Briefing: collects the day's news (articles + videos) from RSS feeds,
has an LLM (free Gemini tier by default) pick the important stories and summarize them, and emails you a
single HTML newsletter.

Usage:
    python briefing.py            # build and send the email
    python briefing.py --dry-run  # build only, write preview.html (no email)
"""
import argparse
import html
import json
import logging
import os
import re
import smtplib
import ssl
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import feedparser
import requests
import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("briefing")

UA = {"User-Agent": "Mozilla/5.0 (DailyBriefing/1.0)"}


# --------------------------------------------------------------------------
# 1. Fetching
# --------------------------------------------------------------------------
def clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", title.lower()).strip()


def fetch_feed(url: str, cutoff: datetime):
    """Return entries from one feed published after `cutoff`. Never raises."""
    try:
        resp = requests.get(url, headers=UA, timeout=15)
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
    except Exception as exc:
        log.warning("Feed failed: %s (%s)", url, exc)
        return []

    source = clean(parsed.feed.get("title", "")) or url
    out = []
    for e in parsed.entries:
        ts = e.get("published_parsed") or e.get("updated_parsed")
        if not ts:
            continue
        published = datetime(*ts[:6], tzinfo=timezone.utc)
        if published < cutoff:
            continue
        vid = e.get("yt_videoid")
        out.append(
            {
                "title": clean(e.get("title", "")),
                "link": e.get("link", ""),
                "source": source,
                "snippet": clean(e.get("summary", ""))[:500],
                "published": published,
                "thumb": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg" if vid else None,
            }
        )
    return out


def collect_articles(feeds, cutoff, cap):
    seen, items = set(), []
    for url in feeds:
        for item in fetch_feed(url, cutoff):
            key = norm_title(item["title"])
            if not key or key in seen:
                continue
            seen.add(key)
            items.append(item)
    items.sort(key=lambda x: x["published"], reverse=True)
    return items[:cap]


def collect_videos(channels, cutoff, per_channel, total):
    videos = []
    for ch in channels:
        url = f"https://www.youtube.com/feeds/videos.xml?channel_id={ch['channel_id']}"
        got = fetch_feed(url, cutoff)[:per_channel]
        for v in got:
            v["source"] = ch["name"]
        videos.extend(got)
    videos.sort(key=lambda x: x["published"], reverse=True)
    return videos[:total]


# --------------------------------------------------------------------------
# 2. Summarizing (free Gemini tier by default; Claude optional)
# --------------------------------------------------------------------------
def llm_available():
    return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"))


def llm_json(prompt, max_tokens=8000):
    """Send one prompt to whichever provider is configured and return parsed JSON."""
    last_exc = None
    for attempt in range(3):
        try:
            if os.environ.get("GEMINI_API_KEY"):
                # "gemini-flash-latest" is an alias, so it survives model renames/retirements.
                model = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
                    json={
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {
                            "responseMimeType": "application/json",
                            "maxOutputTokens": max_tokens,
                        },
                    },
                    timeout=180,
                )
                r.raise_for_status()
                parts = r.json()["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts)
            else:
                r = requests.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
                        "anthropic-version": "2023-06-01",
                    },
                    json={
                        "model": os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5"),
                        "max_tokens": max_tokens,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                    timeout=180,
                )
                r.raise_for_status()
                text = r.json()["content"][0]["text"]
            text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
            return json.loads(text)
        except Exception as exc:  # rate limit, overload, bad JSON: wait and retry
            last_exc = exc
            log.warning("LLM attempt %d failed: %s", attempt + 1, exc)
            time.sleep(20)
    raise last_exc


def fallback_sections(candidates):
    return [
        {
            "name": c["name"],
            "overview": "",
            "picks": [{**i, "summary": i["snippet"][:220]} for i in c["items"][: c["keep"]]],
        }
        for c in candidates
    ]


def summarize_all(candidates):
    """ONE LLM call for the whole newsletter (keeps us far inside free-tier limits)."""
    if not candidates or not llm_available():
        if not llm_available():
            log.warning("No LLM key set: using raw feed snippets instead of AI summaries.")
        return fallback_sections(candidates), []

    flat, payload = [], []
    for c in candidates:
        rows = []
        for it in c["items"]:
            gid = len(flat)
            flat.append(it)
            rows.append({"id": gid, "title": it["title"], "source": it["source"], "snippet": it["snippet"][:250]})
        payload.append({"category": c["name"], "keep": c["keep"], "stories": rows})

    prompt = f"""You are the editor of a daily news briefing. Below (JSON) are candidate stories from the last 24 hours, grouped by category.

For EACH category:
1. Pick the "keep" most important stories. If several stories cover the same event, pick only one.
2. Write a neutral, factual 1-2 sentence summary per pick. Use ONLY information in the title/snippet; never invent details.
3. Write a 2-3 sentence "overview" of what is happening in that category today.
Then write "bullets": 5 short points (max 20 words each) for "Today in 60 seconds", covering the most important things across ALL categories.

Return ONLY JSON in exactly this shape:
{{"bullets": ["..."], "categories": [{{"name": "<category name>", "overview": "...", "picks": [{{"id": 0, "summary": "..."}}]}}]}}

Use the global "id" values given. Input:
{json.dumps(payload, ensure_ascii=False)}"""

    try:
        data = llm_json(prompt)
    except Exception as exc:
        log.error("Summarizing failed, using fallback: %s", exc)
        return fallback_sections(candidates), []

    by_name = {c.get("name"): c for c in data.get("categories", [])}
    fb = {s["name"]: s for s in fallback_sections(candidates)}
    sections = []
    for c in candidates:
        got = by_name.get(c["name"], {})
        picks = []
        for p in got.get("picks", []):
            idx = p.get("id")
            if isinstance(idx, int) and 0 <= idx < len(flat):
                picks.append({**flat[idx], "summary": p.get("summary", "")})
        if picks:
            sections.append({"name": c["name"], "overview": got.get("overview", ""), "picks": picks})
        else:
            sections.append(fb[c["name"]])
    return sections, data.get("bullets", [])


# --------------------------------------------------------------------------
# 3. Rendering
# --------------------------------------------------------------------------
def esc(s):
    return html.escape(s or "", quote=True)


def render_html(date_label, bullets, sections, videos):
    parts = [
        f"""<div style="max-width:640px;margin:0 auto;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#1a1a1a;background:#ffffff;padding:24px">
<h1 style="margin:0 0 4px;font-size:26px">Your Daily Briefing</h1>
<div style="color:#666;margin-bottom:20px">{esc(date_label)}</div>"""
    ]

    if bullets:
        lis = "".join(f"<li style='margin-bottom:6px'>{esc(b)}</li>" for b in bullets)
        parts.append(
            f"<div style='background:#f3f6fb;border-left:4px solid #2b5fd9;padding:12px 18px;margin-bottom:28px'>"
            f"<div style='font-weight:700;margin-bottom:8px'>Today in 60 seconds</div><ul style='margin:0;padding-left:18px'>{lis}</ul></div>"
        )

    for s in sections:
        if not s["picks"]:
            continue
        parts.append(
            f"<h2 style='font-size:20px;border-bottom:2px solid #eee;padding-bottom:6px;margin-top:32px'>{esc(s['name'])}</h2>"
        )
        if s["overview"]:
            parts.append(f"<p style='color:#444;font-style:italic'>{esc(s['overview'])}</p>")
        for p in s["picks"]:
            parts.append(
                f"<div style='margin:16px 0'>"
                f"<a href='{esc(p['link'])}' style='color:#1a0dab;font-weight:600;text-decoration:none;font-size:16px'>{esc(p['title'])}</a>"
                f"<div style='color:#888;font-size:12px;margin:2px 0'>{esc(p['source'])}</div>"
                f"<div style='font-size:14px;line-height:1.5'>{esc(p['summary'])}</div></div>"
            )

    if videos:
        parts.append("<h2 style='font-size:20px;border-bottom:2px solid #eee;padding-bottom:6px;margin-top:32px'>Watch</h2>")
        for v in videos:
            thumb = (
                f"<a href='{esc(v['link'])}'><img src='{esc(v['thumb'])}' width='160' style='border-radius:6px;display:block'></a>"
                if v.get("thumb")
                else ""
            )
            parts.append(
                f"<table style='margin:14px 0'><tr><td style='vertical-align:top;padding-right:12px'>{thumb}</td>"
                f"<td style='vertical-align:top'><a href='{esc(v['link'])}' style='color:#1a0dab;font-weight:600;text-decoration:none'>{esc(v['title'])}</a>"
                f"<div style='color:#888;font-size:12px;margin-top:4px'>{esc(v['source'])}</div></td></tr></table>"
            )

    parts.append(
        "<p style='color:#999;font-size:12px;margin-top:36px'>Summaries are AI-generated from headlines and feed snippets. "
        "Open the links for full context.</p></div>"
    )
    return "".join(parts)


# --------------------------------------------------------------------------
# 4. Sending
# --------------------------------------------------------------------------
def send_email(subject, body_html):
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    user, password = os.environ["SMTP_USER"], os.environ["SMTP_PASS"]
    to = os.environ.get("EMAIL_TO", user)

    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.attach(MIMEText("Your email client does not support HTML.", "plain"))
    msg.attach(MIMEText(body_html, "html"))

    with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context()) as server:
        server.login(user, password)
        server.sendmail(user, [to], msg.as_string())
    log.info("Email sent to %s", to)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="write preview.html instead of emailing")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    tz = ZoneInfo(cfg.get("timezone", "Asia/Kolkata"))
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=cfg.get("lookback_hours", 26))
    date_label = now.astimezone(tz).strftime("%A, %d %B %Y")

    candidates = []
    for cat in cfg["categories"]:
        items = collect_articles(cat["feeds"], cutoff, cfg.get("candidates_per_category", 25))
        log.info("%s: %d candidates", cat["name"], len(items))
        if items:
            keep = cat.get("keep", cfg.get("stories_per_category", 5))
            candidates.append({"name": cat["name"], "keep": keep, "items": items})

    sections, bullets = summarize_all(candidates)

    videos = collect_videos(
        cfg.get("video_channels", []), cutoff, cfg.get("videos_per_channel", 2), cfg.get("max_videos", 8)
    )
    log.info("Videos: %d", len(videos))

    body = render_html(date_label, bullets, sections, videos)

    if args.dry_run:
        with open("preview.html", "w", encoding="utf-8") as f:
            f.write(body)
        log.info("Wrote preview.html")
    else:
        send_email(f"Daily Briefing: {date_label}", body)


if __name__ == "__main__":
    main()
