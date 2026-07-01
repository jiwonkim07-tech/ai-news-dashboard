/* =========================================================================
 * AI 소식 대시보드 — app.js
 * data.json 을 불러와 소스별 카드로 렌더. 최신순, 필터 칩, NEW 뱃지.
 * 데이터 갱신은 GitHub Actions(1시간). 브라우저는 주기적으로 다시 fetch.
 * ========================================================================= */

var CFG = window.SITE_CONFIG || {};
var LAST_SEEN_KEY = "ai-news:lastSeen";

var state = { items: [], filter: "all", updatedAt: null };

/* ---------- 유틸 ---------- */
function el(tag, cls, text) {
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function timeAgo(iso) {
  var d = new Date(iso);
  if (isNaN(d)) return "";
  var s = Math.floor((Date.now() - d.getTime()) / 1000);
  if (s < 60) return "방금";
  if (s < 3600) return Math.floor(s / 60) + "분 전";
  if (s < 86400) return Math.floor(s / 3600) + "시간 전";
  if (s < 604800) return Math.floor(s / 86400) + "일 전";
  return (d.getMonth() + 1) + "/" + d.getDate();
}

function fmtUpdated(iso) {
  var d = new Date(iso);
  if (isNaN(d)) return "";
  var p = function (n) { return String(n).padStart(2, "0"); };
  return d.getFullYear() + "." + p(d.getMonth() + 1) + "." + p(d.getDate()) +
    " " + p(d.getHours()) + ":" + p(d.getMinutes());
}

/* ---------- 로드 ---------- */
function load(showSpin) {
  var btn = document.getElementById("refreshBtn");
  if (showSpin && btn) btn.classList.add("spin");
  fetch("./data.json?t=" + Date.now(), { cache: "no-store" })
    .then(function (r) {
      if (!r.ok) throw new Error("data.json " + r.status);
      return r.json();
    })
    .then(function (data) {
      state.items = (data.items || []).slice();
      state.updatedAt = data.updated_at;
      render();
    })
    .catch(function (e) {
      var empty = document.getElementById("emptyState");
      if (empty) {
        empty.style.display = "";
        empty.textContent = "데이터를 아직 불러올 수 없습니다. (첫 수집 전이거나 네트워크 오류)";
      }
      document.getElementById("updatedAt").textContent = "불러오기 실패";
      console.warn(e);
    })
    .finally(function () {
      if (btn) setTimeout(function () { btn.classList.remove("spin"); }, 500);
    });
}

/* ---------- 렌더 ---------- */
function render() {
  var lastSeen = Number(localStorage.getItem(LAST_SEEN_KEY) || 0);

  // 상태바
  document.getElementById("updatedAt").textContent =
    state.updatedAt ? "마지막 갱신 " + fmtUpdated(state.updatedAt) : "";
  var newCount = state.items.filter(function (it) {
    return new Date(it.published_at).getTime() > lastSeen;
  }).length;
  document.getElementById("countInfo").innerHTML =
    "총 <b>" + state.items.length + "</b>건" +
    (newCount ? " · 새 글 <b>" + newCount + "</b>" : "");

  renderFilters();

  var feed = document.getElementById("feed");
  feed.innerHTML = "";
  var list = state.items.filter(matchFilter);

  if (!list.length) {
    var empty = el("div", "empty", "표시할 소식이 없습니다.");
    feed.appendChild(empty);
  } else {
    list.forEach(function (it) {
      feed.appendChild(renderItem(it, lastSeen));
    });
  }

  // 방문 시점 기록 (다음 방문의 NEW 기준)
  localStorage.setItem(LAST_SEEN_KEY, String(Date.now()));
}

function matchFilter(it) {
  if (state.filter === "all") return true;
  if (state.filter === "x" || state.filter === "telegram") return it.source === state.filter;
  return (it.source + ":" + it.author) === state.filter;
}

function renderFilters() {
  var nav = document.getElementById("filters");
  nav.innerHTML = "";
  var counts = { all: state.items.length, x: 0, telegram: 0 };
  var authors = {};
  state.items.forEach(function (it) {
    counts[it.source] = (counts[it.source] || 0) + 1;
    var k = it.source + ":" + it.author;
    authors[k] = authors[k] || { author: it.author, source: it.source, n: 0 };
    authors[k].n++;
  });

  var chips = [{ key: "all", label: "전체", n: counts.all }];
  if (counts.x) chips.push({ key: "x", label: "X", n: counts.x });
  if (counts.telegram) chips.push({ key: "telegram", label: "텔레그램", n: counts.telegram });
  Object.keys(authors).sort().forEach(function (k) {
    chips.push({ key: k, label: "@" + authors[k].author, n: authors[k].n });
  });

  chips.forEach(function (c) {
    var chip = el("button", "chip" + (state.filter === c.key ? " active" : ""));
    chip.appendChild(document.createTextNode(c.label));
    var n = el("span", "n", c.n);
    chip.appendChild(n);
    chip.onclick = function () { state.filter = c.key; render(); };
    nav.appendChild(chip);
  });
}

function renderItem(it, lastSeen) {
  var card = el("div", "item " + it.source);
  var isNew = new Date(it.published_at).getTime() > lastSeen;

  var head = el("div", "item-head");
  head.appendChild(el("span", "src-badge " + it.source, it.source === "x" ? "X" : "TG"));
  var a = el("a", "author", "@" + it.author);
  a.href = it.author_url; a.target = "_blank"; a.rel = "noopener";
  head.appendChild(a);
  if (isNew) head.appendChild(el("span", "new-badge", "NEW"));
  head.appendChild(el("span", "time", timeAgo(it.published_at)));
  card.appendChild(head);

  if (it.summary_ko) {
    card.appendChild(el("div", "summary", it.summary_ko));
  } else {
    card.appendChild(el("div", "summary none", "(자동 요약 없음 — 원문 참고)"));
  }

  var orig = el("div", "orig clamp", it.text || "");
  card.appendChild(orig);

  var foot = el("div", "item-foot");
  if ((it.text || "").length > 140) {
    var toggle = el("button", "toggle-orig", "원문 더보기");
    toggle.onclick = function () {
      var clamped = orig.classList.toggle("clamp");
      toggle.textContent = clamped ? "원문 더보기" : "접기";
    };
    foot.appendChild(toggle);
  }
  var link = el("a", "orig-link", "원글 열기 ↗");
  link.href = it.url; link.target = "_blank"; link.rel = "noopener";
  foot.appendChild(link);
  card.appendChild(foot);

  return card;
}

/* ---------- 텔레그램 헤더 버튼 ---------- */
(function tgBtn() {
  if (!CFG.telegramUrl) return;
  var bar = document.querySelector(".topbar .wrap");
  if (!bar) return;
  var a = document.createElement("a");
  a.href = CFG.telegramUrl; a.target = "_blank"; a.rel = "noopener";
  a.className = "refresh-btn"; a.title = "Telegram"; a.textContent = "✈";
  bar.appendChild(a);
})();

/* ---------- 이벤트 & 주기 갱신 ---------- */
document.getElementById("refreshBtn").addEventListener("click", function () { load(true); });
load(false);
var mins = Number(CFG.refreshMinutes) > 0 ? Number(CFG.refreshMinutes) : 30;
setInterval(function () { load(false); }, mins * 60 * 1000);
