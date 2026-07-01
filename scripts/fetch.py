#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 소식 대시보드 — 수집 파이프라인
--------------------------------------------------------------------------
1) Telegram 공개 채널  : https://t.me/s/<channel> HTML 파싱 (무료·키 불필요)
2) X(트위터) 계정      : RSSHub 무료 인스턴스 → 실패 시 RapidAPI 폴백
3) 중복 제거           : state.json 의 처리 완료 id 와 대조, 신규만 처리
4) 한국어 요약/번역    : Gemini 무료 API (신규 항목만, 배치 호출로 한도 절약)
5) data.json 갱신      : 최신순 정렬 후 소스별 컷

환경변수(Secrets):
  GEMINI_API_KEY   요약용 (없으면 요약 생략, 원문만 저장)
  RAPIDAPI_KEY     X 유료 폴백용 (없으면 무료 경로만 사용)
  RAPIDAPI_HOST    (선택) 기본 twitter-api45.p.rapidapi.com
  RSSHUB_BASES     (선택) 콤마 구분 RSSHub 인스턴스 목록
"""

import os
import re
import json
import html
import time
import datetime as dt
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
import feedparser

# ---------------------------------------------------------------- 경로/상수
ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "sources.json"
DATA_PATH = ROOT / "data.json"
STATE_PATH = ROOT / "state.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en,ko;q=0.9"}
TIMEOUT = 25.0
STATE_KEEP = 200  # 소스별로 기억할 최근 id 개수

# RSSHub 공개 인스턴스 후보 (앞에서부터 시도)
DEFAULT_RSSHUB = [
    "https://rsshub.app",
    "https://rsshub.rssforever.com",
    "https://rss.shab.fun",
]

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "").strip()
RAPIDAPI_HOST = os.environ.get("RAPIDAPI_HOST", "twitter-api45.p.rapidapi.com").strip()


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def log(*a):
    print("[fetch]", *a, flush=True)


# ============================================================= 텔레그램 수집
def fetch_telegram(channel: str, limit: int) -> list[dict]:
    """t.me/s/<channel> 미리보기 HTML을 파싱해 최근 글을 반환."""
    url = f"https://t.me/s/{channel}"
    try:
        r = httpx.get(url, headers=HEADERS, timeout=TIMEOUT, follow_redirects=True)
        r.raise_for_status()
    except Exception as e:
        log(f"telegram '{channel}' 실패: {e}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    for msg in soup.select(".tgme_widget_message"):
        post = msg.get("data-post")  # "channel/123"
        if not post:
            continue
        # 본문
        body = msg.select_one(".tgme_widget_message_text")
        text = body.get_text("\n", strip=True) if body else ""
        # 시간
        t = msg.select_one("a.tgme_widget_message_date time")
        published = t.get("datetime") if t and t.get("datetime") else now_iso()
        link_el = msg.select_one("a.tgme_widget_message_date")
        link = link_el.get("href") if link_el else f"https://t.me/{post}"

        if not text:
            # 미디어만 있는 글은 간단 표기
            text = "(미디어/첨부 글 — 원문에서 확인)"

        items.append({
            "id": f"tg:{post}",
            "source": "telegram",
            "author": channel,
            "author_url": f"https://t.me/{channel}",
            "url": link,
            "text": text,
            "summary_ko": "",
            "published_at": _norm_dt(published),
        })

    items.sort(key=lambda x: x["published_at"], reverse=True)
    log(f"telegram '{channel}': {len(items)}건")
    return items[:limit]


# ================================================================= X 수집
def fetch_x(account: str, limit: int) -> list[dict]:
    """무료(RSSHub) 우선, 실패 시 RapidAPI 폴백."""
    items = _fetch_x_rsshub(account, limit)
    if items:
        return items
    if RAPIDAPI_KEY:
        items = _fetch_x_rapidapi(account, limit)
        if items:
            return items
    log(f"x '{account}': 수집 실패(무료·폴백 모두)")
    return []


def _fetch_x_rsshub(account: str, limit: int) -> list[dict]:
    bases = [b.strip() for b in os.environ.get("RSSHUB_BASES", "").split(",") if b.strip()]
    bases = bases or DEFAULT_RSSHUB
    for base in bases:
        feed_url = f"{base}/twitter/user/{account}"
        try:
            r = httpx.get(feed_url, headers=HEADERS, timeout=TIMEOUT, follow_redirects=True)
            if r.status_code != 200 or not r.text.strip():
                continue
            feed = feedparser.parse(r.text)
            if not feed.entries:
                continue
            items = []
            for e in feed.entries[:limit]:
                link = e.get("link", "")
                m = re.search(r"/status/(\d+)", link)
                native = m.group(1) if m else (e.get("id") or link)
                text = _clean_html(e.get("title") or e.get("summary") or "")
                published = _entry_time(e)
                items.append({
                    "id": f"x:{account}:{native}",
                    "source": "x",
                    "author": account,
                    "author_url": f"https://x.com/{account}",
                    "url": link or f"https://x.com/{account}",
                    "text": text,
                    "summary_ko": "",
                    "published_at": published,
                })
            log(f"x '{account}': RSSHub({base}) {len(items)}건")
            return items
        except Exception as e:
            log(f"x '{account}': RSSHub({base}) 오류 {e}")
            continue
    return []


def _fetch_x_rapidapi(account: str, limit: int) -> list[dict]:
    """RapidAPI 트위터 스크래퍼 폴백. 기본값은 twitter-api45(저가/인기).
    다른 제공자를 쓰면 응답 파싱부만 수정하면 됨."""
    url = f"https://{RAPIDAPI_HOST}/timeline.php"
    headers = {"x-rapidapi-key": RAPIDAPI_KEY, "x-rapidapi-host": RAPIDAPI_HOST}
    try:
        r = httpx.get(url, headers=headers, params={"screenname": account},
                      timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log(f"x '{account}': RapidAPI 오류 {e}")
        return []

    tweets = data.get("timeline") or data.get("tweets") or []
    items = []
    for tw in tweets[:limit]:
        native = str(tw.get("tweet_id") or tw.get("id_str") or tw.get("id") or "")
        text = _clean_html(tw.get("text") or tw.get("full_text") or "")
        created = tw.get("created_at") or ""
        items.append({
            "id": f"x:{account}:{native}",
            "source": "x",
            "author": account,
            "author_url": f"https://x.com/{account}",
            "url": f"https://x.com/{account}/status/{native}" if native else f"https://x.com/{account}",
            "text": text,
            "summary_ko": "",
            "published_at": _norm_dt(created),
        })
    log(f"x '{account}': RapidAPI {len(items)}건")
    return items


# ============================================================= 한국어 요약
def summarize_batch(items: list[dict]):
    """신규 항목들의 한국어 요약을 배치로 채운다(제자리 수정)."""
    if not items or not GEMINI_API_KEY:
        if items and not GEMINI_API_KEY:
            log("GEMINI_API_KEY 없음 → 요약 생략(원문만 저장)")
        return
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-2.0-flash")
    except Exception as e:
        log(f"Gemini 초기화 실패 → 요약 생략: {e}")
        return

    BATCH = 10
    for start in range(0, len(items), BATCH):
        chunk = items[start:start + BATCH]
        numbered = "\n\n".join(
            f"[{i}] (@{it['author']}) {it['text'][:800]}" for i, it in enumerate(chunk)
        )
        prompt = (
            "다음은 AI/투자 관련 소셜·텔레그램 글 목록이다. 각 글을 한국어로 "
            "1~2문장(최대 90자)으로 핵심만 요약하라. 영어 원문은 한국어로 번역해 요약한다. "
            "링크·해시태그·이모지는 빼고 사실 중심으로. "
            "반드시 아래 JSON 배열 형식으로만 출력하라(설명 금지):\n"
            '[{"i": 0, "summary": "..."}, ...]\n\n' + numbered
        )
        try:
            resp = model.generate_content(prompt)
            text = (resp.text or "").strip()
            text = re.sub(r"^```(json)?|```$", "", text, flags=re.MULTILINE).strip()
            arr = json.loads(text)
            by_i = {int(o["i"]): str(o["summary"]).strip() for o in arr if "i" in o}
            for i, it in enumerate(chunk):
                if i in by_i:
                    it["summary_ko"] = by_i[i]
            log(f"요약 완료: {start}~{start+len(chunk)-1}")
        except Exception as e:
            log(f"요약 배치 실패({start}): {e}")
        time.sleep(1.2)  # 무료 티어 RPM 여유


# =================================================================== 유틸
def _clean_html(s: str) -> str:
    s = html.unescape(s or "")
    s = BeautifulSoup(s, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", s).strip()


def _norm_dt(s: str) -> str:
    """다양한 시간 문자열을 ISO(UTC)로 정규화. 실패 시 현재 시각."""
    if not s:
        return now_iso()
    s = s.strip()
    # 이미 ISO 형태
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()
    except Exception:
        pass
    # 트위터 형식: "Wed Oct 10 20:19:24 +0000 2018"
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            d = dt.datetime.strptime(s, fmt)
            return d.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()
        except Exception:
            continue
    return now_iso()


def _entry_time(e) -> str:
    if getattr(e, "published_parsed", None):
        return dt.datetime(*e.published_parsed[:6], tzinfo=dt.timezone.utc)\
            .replace(microsecond=0).isoformat()
    return _norm_dt(e.get("published", ""))


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


# =================================================================== main
def main():
    cfg = load_json(CONFIG_PATH, {})
    x_accounts = cfg.get("x_accounts", [])
    tg_channels = cfg.get("telegram_channels", [])
    max_items = int(cfg.get("max_items_per_source", 12))
    do_summarize = bool(cfg.get("summarize", True))

    state = load_json(STATE_PATH, {})           # {source_key: [id, ...]}
    prev = load_json(DATA_PATH, {"items": []})
    prev_items = {it["id"]: it for it in prev.get("items", [])}

    # 1) 수집
    fetched: list[dict] = []
    for acc in x_accounts:
        fetched += fetch_x(acc, max_items)
    for ch in tg_channels:
        fetched += fetch_telegram(ch, max_items)

    # 2) 신규 판별
    seen_ids = {i for ids in state.values() for i in ids}
    new_items = [it for it in fetched if it["id"] not in seen_ids
                 and it["id"] not in prev_items]
    log(f"수집 {len(fetched)}건 / 신규 {len(new_items)}건")

    # 3) 신규만 요약
    if do_summarize and new_items:
        summarize_batch(new_items)

    # 4) 병합 (기존 요약은 유지, 신규는 새로)
    merged: dict[str, dict] = dict(prev_items)
    for it in fetched:
        if it["id"] in merged:
            # 기존 항목 유지 (요약 재사용), 최신 url/text 소폭 갱신
            merged[it["id"]]["url"] = it["url"]
        else:
            merged[it["id"]] = it

    # 5) 소스별 최신순 컷
    by_source: dict[str, list[dict]] = {}
    for it in merged.values():
        by_source.setdefault(f"{it['source']}:{it['author']}", []).append(it)
    kept: list[dict] = []
    new_state: dict[str, list[str]] = {}
    for key, lst in by_source.items():
        lst.sort(key=lambda x: x["published_at"], reverse=True)
        keep = lst[:max_items]
        kept += keep
        new_state[key] = [x["id"] for x in lst[:STATE_KEEP]]

    kept.sort(key=lambda x: x["published_at"], reverse=True)

    data = {
        "updated_at": now_iso(),
        "counts": {"total": len(kept), "new_this_run": len(new_items)},
        "items": kept,
    }
    save_json(DATA_PATH, data)
    save_json(STATE_PATH, new_state)
    log(f"완료 → data.json {len(kept)}건 (신규 {len(new_items)})")


if __name__ == "__main__":
    main()
