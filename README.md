# AI 소식 대시보드

매일 챙겨보는 **X 공식 계정**(OpenAI, Anthropic, Andrej Karpathy 등)과 **애널리스트 텔레그램
공개 채널**의 새 글을, 1시간마다 자동 수집 → 한국어 요약 → 한 화면 대시보드로 보여줍니다.

- **수집/자동화**: GitHub Actions (무료 harness) — 매시 정각 크론
- **X**: 무료(RSSHub 공개 인스턴스) 우선 → 실패 시 RapidAPI 폴백(선택)
- **Telegram**: `t.me/s/<채널>` 공개 미리보기 파싱 (무료·키 불필요)
- **요약**: Google Gemini 무료 API (`gemini-2.0-flash`)
- **프론트**: 바닐라 JS 정적 사이트 → Netlify 또는 GitHub Pages

## 구조
```
index.html / app.js / styles.css / config.js   # 프론트 (data.json 렌더)
config/sources.json                            # 감시 대상 (여기만 수정)
scripts/fetch.py                               # 수집·요약·갱신 파이프라인
data.json / state.json                         # 결과 / 중복방지 상태 (Actions가 커밋)
.github/workflows/update.yml                   # 1시간 크론 + 커밋/푸시
```

## 감시 대상 바꾸기
`config/sources.json` 의 값만 수정:
```json
{
  "x_accounts": ["OpenAI", "AnthropicAI", "karpathy"],
  "telegram_channels": ["andyc14note"],
  "max_items_per_source": 12,
  "summarize": true
}
```

## 셋업 (5단계)
1. GitHub에 repo 생성 후 이 폴더 내용을 푸시.
   - 15분보다 잦은 갱신이나 private repo 무제한을 원하면 **public repo** 권장(Actions 무제한).
2. **Google AI Studio**에서 API 키 무료 발급 → repo **Settings → Secrets and variables →
   Actions** 에 `GEMINI_API_KEY` 등록. (없으면 요약 없이 원문만 표시)
3. (선택) **RapidAPI** 트위터 API 구독(무료 티어) → `RAPIDAPI_KEY` 등록. X 무료 경로가 막힐 때만 사용.
   - 다른 제공자를 쓰면 `RAPIDAPI_HOST` 도 함께 등록하고, `scripts/fetch.py` 의
     `_fetch_x_rapidapi` 파싱부만 응답 형식에 맞게 수정.
4. **배포**
   - Netlify: repo 연결, Build command 비움, Publish directory = `/` (루트). push 시 자동 배포.
   - 또는 GitHub Pages: Settings → Pages → Source = main / root.
5. Actions 탭에서 **Run workflow**(workflow_dispatch)로 1회 수동 실행 → `data.json` 이 채워지고
   사이트에 반영되는지 확인.

## 로컬 실행/테스트
```bash
pip install -r scripts/requirements.txt
export GEMINI_API_KEY=...        # (선택) 없으면 요약 생략
python scripts/fetch.py          # data.json 갱신 — 텔레그램은 키 없이도 수집됨
python -m http.server 8000       # http://localhost:8000 에서 대시보드 확인
```

## 참고 / 한계
- X 무료 경로(RSSHub)는 X 정책에 따라 언제든 막힐 수 있어 유료 폴백을 둡니다. 폴백 키가 없으면
  X 항목이 간헐적으로 비어 있을 수 있습니다.
- 텔레그램 **비공개/저장제한** 채널은 공개 미리보기가 없어 수집 불가.
- GitHub 크론은 부하 시 지연/스킵될 수 있어 실제 간격은 1시간~그 이상일 수 있습니다.
- 한국어 요약은 자동 생성으로 오차가 있을 수 있으니 중요한 내용은 원문 확인 권장.
