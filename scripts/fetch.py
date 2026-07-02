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
import sys
import time
import datetime as dt
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
import feedparser

# Windows 콘솔(cp949)에서도 한글/특수문자 로그가 깨지거나 죽지 않도록 UTF-8 강제
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

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

# Nitter 인스턴스 후보 (앞에서부터 시도, 무료). 2026-07 기준 nitter.net 정상.
DEFAULT_NITTER = [
    "https://nitter.net",
    "https://nitter.privacyredirect.com",
    "https://nitter.poast.org",
]
# RSSHub 공개 인스턴스 후보 (니터 실패 시 2차)
DEFAULT_RSSHUB = [
    "https://rsshub.app",
    "https://rsshub.rssforever.com",
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
    # 채널 표시 이름(og:title). 실패 시 username 사용.
    og = soup.select_one('meta[property="og:title"]')
    channel_name = og["content"].strip() if og and og.get("content") else channel

    items = []
    for msg in soup.select(".tgme_widget_message"):
        post = msg.get("data-post")  # "channel/123"
        if not post:
            continue
        # 본문
        body = msg.select_one(".tgme_widget_message_text")
        text = body.get_text("\n", strip=True) if body else ""
        # 첨부 이미지 (background-image url 추출)
        images = []
        for ph in msg.select(".tgme_widget_message_photo_wrap"):
            m = re.search(r"background-image:\s*url\('([^']+)'\)", ph.get("style", ""))
            if m:
                images.append(m.group(1))
        # 시간
        t = msg.select_one("a.tgme_widget_message_date time")
        published = t.get("datetime") if t and t.get("datetime") else now_iso()
        link_el = msg.select_one("a.tgme_widget_message_date")
        link = link_el.get("href") if link_el else f"https://t.me/{post}"

        # 장식용 글(◆ 같은 기호만) 처리: 이미지도 없으면 스킵, 이미지 있으면 라벨로 대체
        if _is_decorative(text):
            if not images:
                continue
            text = ""
        if not text and not images:
            continue
        if not text:
            text = "(이미지/차트 — 원문 참고)"

        items.append({
            "id": f"tg:{post}",
            "source": "telegram",
            "author": channel,
            "author_name": channel_name,
            "author_url": f"https://t.me/{channel}",
            "url": link,
            "text": text,
            "images": images,
            "summary_ko": "",
            "published_at": _norm_dt(published),
        })

    items.sort(key=lambda x: x["published_at"], reverse=True)
    log(f"telegram '{channel}': {len(items)}건")
    return items[:limit]


# ================================================================= X 수집
def fetch_x(account: str, limit: int, display_name: str = "") -> list[dict]:
    """무료 우선: Nitter RSS → RSSHub → (키 있으면) RapidAPI 폴백."""
    items = _fetch_x_nitter(account, limit)
    if not items:
        items = _fetch_x_rsshub(account, limit)
    if not items and RAPIDAPI_KEY:
        items = _fetch_x_rapidapi(account, limit)
    if not items:
        log(f"x '{account}': 수집 실패(무료·폴백 모두)")
        return []
    if display_name:
        for it in items:
            it["author_name"] = display_name
    return items


def _fetch_x_nitter(account: str, limit: int) -> list[dict]:
    """Nitter 인스턴스의 /<account>/rss 를 파싱(무료·키 불필요)."""
    import urllib.parse
    bases = [b.strip() for b in os.environ.get("NITTER_BASES", "").split(",") if b.strip()]
    bases = bases or DEFAULT_NITTER
    for base in bases:
        try:
            r = httpx.get(f"{base}/{account}/rss", headers=HEADERS,
                          timeout=TIMEOUT, follow_redirects=True)
            if r.status_code != 200 or "<item>" not in r.text:
                continue
            feed = feedparser.parse(r.text)
            if not feed.entries:
                continue
            items = []
            for e in feed.entries[:limit]:
                link = e.get("link", "")
                m = re.search(r"/status/(\d+)", link)
                native = m.group(1) if m else (e.get("id") or link)
                text = _clean_html(e.get("title") or "")
                # 첨부 이미지: description 의 <img> → nitter 프록시를 pbs 원본으로 변환
                images = []
                raw = e.get("summary", "") or ""
                for src in re.findall(r'<img[^>]+src="([^"]+)"', raw)[:2]:
                    mm = re.search(r"/pic/(?:orig/)?(.+)$", src)
                    if mm:
                        images.append("https://pbs.twimg.com/"
                                      + urllib.parse.unquote(mm.group(1)))
                    elif src.startswith("http"):
                        images.append(src)
                x_url = f"https://x.com/{account}/status/{native}" if str(native).isdigit() \
                    else f"https://x.com/{account}"
                items.append({
                    "id": f"x:{account}:{native}",
                    "source": "x",
                    "author": account,
                    "author_name": f"@{account}",
                    "author_url": f"https://x.com/{account}",
                    "url": x_url,
                    "text": text,
                    "images": images,
                    "summary_ko": "",
                    "published_at": _entry_time(e),
                })
            log(f"x '{account}': Nitter({base}) {len(items)}건")
            return items
        except Exception as e:
            log(f"x '{account}': Nitter({base}) 오류 {e}")
            continue
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
                    "author_name": f"@{account}",
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
        name = (tw.get("user") or {}).get("name") if isinstance(tw.get("user"), dict) else None
        items.append({
            "id": f"x:{account}:{native}",
            "source": "x",
            "author": account,
            "author_name": name or f"@{account}",
            "author_url": f"https://x.com/{account}",
            "url": f"https://x.com/{account}/status/{native}" if native else f"https://x.com/{account}",
            "text": text,
            "summary_ko": "",
            "published_at": _norm_dt(created),
        })
    log(f"x '{account}': RapidAPI {len(items)}건")
    return items


# ============================================================= RSS 수집
def fetch_rss(feed: dict, limit: int) -> list[dict]:
    """일반 RSS/Atom 피드 수집(무료). SemiAnalysis 등 뉴스레터·블로그용.
    feed = {"id","name","url","site"(선택)}"""
    fid = feed.get("id") or feed.get("name", "rss")
    name = feed.get("name", fid)
    url = feed.get("url", "")
    site = feed.get("site") or url
    if not url:
        return []
    try:
        r = httpx.get(url, headers=HEADERS, timeout=TIMEOUT, follow_redirects=True)
        r.raise_for_status()
        parsed = feedparser.parse(r.text)
    except Exception as e:
        log(f"rss '{fid}' 실패: {e}")
        return []

    translate = bool(feed.get("translate", True))
    items = []
    for e in parsed.entries[:limit]:
        link = e.get("link", "") or site
        m = re.search(r"/([^/?#]+)/?(?:[?#].*)?$", link)
        native = (m.group(1) if m else e.get("id") or link)[:80]
        title = _clean_html(e.get("title") or "")
        summ = _clean_html(e.get("summary") or e.get("description") or "")
        # 대표 이미지: enclosure → media:content → 본문 첫 <img> 순
        images = []
        for enc in e.get("enclosures", []):
            if enc.get("href") and "image" in (enc.get("type") or "image"):
                images.append(enc["href"]); break
        if not images:
            for mc in e.get("media_content", []):
                if mc.get("url"):
                    images.append(mc["url"]); break
        if not images:
            raw = ""
            if e.get("content"):
                raw = e["content"][0].get("value", "")
            raw = raw or e.get("summary", "")
            mi = re.search(r'<img[^>]+src="([^"]+)"', raw or "")
            if mi:
                images.append(mi.group(1))
        text = title + (("\n\n" + summ) if summ else "")
        items.append({
            "id": f"rss:{fid}:{native}",
            "source": "rss",
            "author": fid,
            "author_name": name,
            "author_url": site,
            "url": link,
            "text": text.strip() or title or "(내용 없음)",
            "images": images,
            "summary_ko": "",
            "published_at": _entry_time(e),
            "_translate": translate,   # 신규 항목만 나중에 번역 (저장 전 제거)
        })
    log(f"rss '{fid}': {len(items)}건")
    return items[:limit]


# ============================================================= 한국어 요약
def summarize_batch(items: list[dict]):
    """신규 항목들의 한국어 요약을 배치로 채운다(제자리 수정).
    무료 한도를 고려해 한 회차 요약 건수를 제한하고, 할당량(429)에 걸리면
    남은 배치를 중단한다(실패 로그 폭주 방지)."""
    if not items or not GEMINI_API_KEY:
        if items and not GEMINI_API_KEY:
            log("GEMINI_API_KEY 없음 → 요약 생략(원문만 저장)")
        return

    model_name = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash").strip()
    max_n = int(os.environ.get("MAX_SUMMARIZE", "60"))
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel(model_name)
    except Exception as e:
        log(f"Gemini 초기화 실패 → 요약 생략: {e}")
        return

    # 최신순으로 최대 max_n건만 요약(무료 한도 절약)
    targets = sorted(items, key=lambda x: x["published_at"], reverse=True)[:max_n]
    if len(items) > max_n:
        log(f"요약 대상 {len(items)}건 중 최신 {max_n}건만 처리(무료 한도)")

    BATCH = 15
    for start in range(0, len(targets), BATCH):
        chunk = targets[start:start + BATCH]
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
            msg = str(e)
            if "429" in msg or "quota" in msg.lower() or "exhausted" in msg.lower():
                log("Gemini 할당량 초과(429) → 이번 회차 요약 중단(원문만 저장). "
                    "무료 한도 회복 후 다음 실행에서 재시도됨.")
                return
            log(f"요약 배치 실패({start}): {e}")
        time.sleep(2.0)  # 무료 티어 RPM 여유


# ============================================================= AI 브리핑
def make_briefing(items: list[dict], prev: dict | None) -> dict | None:
    """최근 글들을 종합한 'AI 브리핑'(헤드라인 1문장 + 요점 3~5개) 생성.
    Gemini 무료 → Groq 무료 순으로 시도, 모두 실패하면 이전 브리핑 유지."""
    now = dt.datetime.now(dt.timezone.utc)
    recent = [it for it in items
              if (now - dt.datetime.fromisoformat(it["published_at"])).total_seconds() < 36 * 3600]
    recent = recent[:40] or items[:25]
    if len(recent) < 5:
        return prev

    def _hl(it):
        s = (it.get("summary_ko") or it.get("text") or "").strip()
        for ln in s.split("\n"):
            if ln.strip() and re.search(r"[0-9A-Za-z가-힣]", ln):
                return ln.strip()[:90]
        return s[:90]

    lines = "\n".join(f"- [{it.get('author_name') or it['author']}] {_hl(it)}" for it in recent)
    prompt = (
        "다음은 최근 수집된 AI·글로벌 투자 소식 헤드라인 목록이다. 네이버 'AI 브리핑'처럼 "
        "전체 흐름을 종합하라. 출력은 반드시 아래 JSON 형식만(설명·마크다운 금지):\n"
        '{"headline": "전체를 관통하는 핵심 한 문장(뉴스 헤드라인체, 60자 이내)", '
        '"bullets": ["주제별 요점(45자 이내)", "... 3~5개"]}\n\n' + lines
    )

    text = ""
    used = ""
    if GEMINI_API_KEY:
        try:
            import google.generativeai as genai
            genai.configure(api_key=GEMINI_API_KEY)
            model = genai.GenerativeModel(os.environ.get("GEMINI_MODEL", "gemini-2.0-flash"))
            text = (model.generate_content(prompt).text or "").strip()
            used = "gemini"
        except Exception as e:
            log(f"브리핑 Gemini 실패: {e}")
    if not text and os.environ.get("GROQ_API_KEY", "").strip():
        try:
            r = httpx.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": "Bearer " + os.environ["GROQ_API_KEY"].strip()},
                json={"model": os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile"),
                      "messages": [{"role": "user", "content": prompt}],
                      "response_format": {"type": "json_object"},
                      "temperature": 0.3},
                timeout=40)
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"].strip()
            used = "groq"
        except Exception as e:
            log(f"브리핑 Groq 실패: {e}")
    if not text:
        log("브리핑 생성 불가 → 이전 브리핑 유지")
        return prev

    try:
        text = re.sub(r"^```(json)?|```$", "", text, flags=re.MULTILINE).strip()
        obj = json.loads(text)
        headline = str(obj.get("headline", "")).strip()
        bullets = [str(b).strip() for b in obj.get("bullets", []) if str(b).strip()][:5]
        if not headline:
            return prev
        log(f"브리핑 생성 완료 ({used})")
        return {"headline": headline, "bullets": bullets,
                "generated_at": now_iso(), "model": used}
    except Exception as e:
        log(f"브리핑 파싱 실패: {e}")
        return prev


# =================================================================== 유틸
def _is_decorative(s: str) -> bool:
    """글자/숫자가 하나도 없는 기호 전용 텍스트(◆, ▶, ---- 등)인지."""
    return not re.search(r"[0-9A-Za-z가-힣一-鿿ぁ-ヺ]", s or "")


def translate_ko(text: str) -> str:
    """구글 번역 무료 엔드포인트(gtx — Chrome 번역과 동일 엔진)로 한국어 번역.
    키 불필요. 실패 시 빈 문자열."""
    text = (text or "").strip()
    if not text:
        return ""
    try:
        r = httpx.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "auto", "tl": "ko", "dt": "t", "q": text[:1500]},
            headers=HEADERS, timeout=TIMEOUT,
        )
        r.raise_for_status()
        arr = r.json()
        return "".join(seg[0] for seg in arr[0] if seg and seg[0]).strip()
    except Exception as e:
        log(f"번역 실패: {e}")
        return ""


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
    rss_feeds = cfg.get("rss_feeds", [])
    max_items = int(cfg.get("max_items_per_source", 12))
    do_summarize = bool(cfg.get("summarize", True))

    state = load_json(STATE_PATH, {})           # {source_key: [id, ...], "_meta": {...}}
    prev = load_json(DATA_PATH, {"items": []})
    prev_items = {it["id"]: it for it in prev.get("items", [])}

    # X는 RapidAPI 유료 한도(월 1,000회)를 아끼려고 N시간마다만 수집.
    # cron은 매시간 돌지만, 마지막 X 수집 후 간격이 안 지났으면 이번 회차 X는 건너뜀.
    meta = state.get("_meta", {}) if isinstance(state.get("_meta"), dict) else {}
    x_every_h = float(cfg.get("x_fetch_every_hours", 12))
    last_x = meta.get("last_x_fetch")
    do_x = True
    if last_x:
        try:
            elapsed = (dt.datetime.now(dt.timezone.utc)
                       - dt.datetime.fromisoformat(last_x)).total_seconds()
            do_x = elapsed >= x_every_h * 3600 * 0.95   # cron 지연 여유 5%
        except Exception:
            do_x = True

    # 1) 수집  (x_accounts 는 "id" 문자열 또는 {"id","name"} 객체 허용)
    fetched: list[dict] = []
    if x_accounts and do_x:
        log(f"X 수집 진행 (마지막 수집 후 {x_every_h}h 경과)")
        for acc in x_accounts:
            if isinstance(acc, dict):
                fetched += fetch_x(acc.get("id", ""), max_items, acc.get("name", ""))
            else:
                fetched += fetch_x(acc, max_items)
    elif x_accounts:
        log(f"X 수집 건너뜀 (아직 {x_every_h}h 미경과 — 무료 인스턴스 부담 완화)")
    for ch in tg_channels:
        fetched += fetch_telegram(ch, max_items)
    for feed in rss_feeds:                     # RSS(무료): SemiAnalysis 등
        fetched += fetch_rss(feed, max_items)

    # 2) 신규 판별 (state 의 리스트 값만 대상, _meta 같은 dict 는 제외)
    seen_ids = {i for ids in state.values() if isinstance(ids, list) for i in ids}
    new_items = [it for it in fetched if it["id"] not in seen_ids
                 and it["id"] not in prev_items]
    log(f"수집 {len(fetched)}건 / 신규 {len(new_items)}건")

    # 3-a) RSS(영문)·X 신규 항목은 구글 번역(무료)으로 한국어 번역 → summary_ko
    tr_max = int(os.environ.get("TRANSLATE_MAX", "80"))
    tr_done = 0
    for it in sorted(new_items, key=lambda x: x["published_at"], reverse=True):
        if tr_done >= tr_max:
            break
        need = (it.get("_translate") and it["source"] == "rss") or it["source"] == "x"
        if need:
            ko = translate_ko(it["text"][:800])
            if ko and ko.strip() != it["text"].strip():
                it["summary_ko"] = ko
            tr_done += 1
            time.sleep(0.6)
    for it in fetched:
        it.pop("_translate", None)

    # 3-b) 나머지 신규만 Gemini 요약 (이미 번역된 항목 제외)
    if do_summarize:
        targets = [it for it in new_items if not it.get("summary_ko")]
        if targets:
            summarize_batch(targets)

    # 4) 병합 — 기존 항목도 최신 수집분으로 필드 갱신(요약/번역만 보존)
    merged: dict[str, dict] = dict(prev_items)
    for it in fetched:
        if it["id"] in merged:
            old_summary = merged[it["id"]].get("summary_ko", "")
            merged[it["id"]] = it
            if old_summary and not it.get("summary_ko"):
                merged[it["id"]]["summary_ko"] = old_summary
        else:
            merged[it["id"]] = it

    # 4-b) 설정에서 제거된 소스의 글은 삭제 (좀비 소스 방지)
    allowed = set()
    for acc in x_accounts:
        allowed.add("x:" + (acc.get("id", "") if isinstance(acc, dict) else acc).lower())
    for ch in tg_channels:
        allowed.add("telegram:" + ch.lower())
    for feed in rss_feeds:
        allowed.add("rss:" + (feed.get("id") or feed.get("name", "")).lower())
    merged = {k: v for k, v in merged.items()
              if f"{v['source']}:{v['author']}".lower() in allowed}

    # 4-c) X 표시 이름은 설정 기준으로 항상 최신화 (수집 스킵 회차 포함)
    name_map = {acc["id"].lower(): acc.get("name", "")
                for acc in x_accounts if isinstance(acc, dict) and acc.get("id")}
    for v in merged.values():
        if v["source"] == "x" and name_map.get(v["author"].lower()):
            v["author_name"] = name_map[v["author"].lower()]

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

    # X 수집 시각 기록 (다음 회차의 12h 간격 판정용)
    new_state["_meta"] = {
        "last_x_fetch": now_iso() if (x_accounts and do_x) else (last_x or ""),
    }

    kept.sort(key=lambda x: x["published_at"], reverse=True)

    briefing = make_briefing(kept, prev.get("briefing"))

    data = {
        "updated_at": now_iso(),
        "counts": {"total": len(kept), "new_this_run": len(new_items)},
        "briefing": briefing,
        "items": kept,
    }
    save_json(DATA_PATH, data)
    save_json(STATE_PATH, new_state)
    log(f"완료 → data.json {len(kept)}건 (신규 {len(new_items)})")


if __name__ == "__main__":
    main()
