"""Stage 1b — 텔레그램 채널 속보 수급 (주요 소스).

사용:
  python -m pipeline.collect_telegram --dry-run            # 저장 없이 채택 메시지 출력
  python -m pipeline.collect_telegram --minutes 30         # 최근 30분 메시지만 (기본 lookback_minutes)
  python -m pipeline.collect_telegram --list-dialogs       # 세션이 구독 중인 채널 목록 확인 (채널 선정용)

환경변수 (텔레그램 봇 프로젝트와 동일 이름, 값은 .env 또는 셸에서):
  TG_API_ID, TG_API_HASH, TG_SESSION_STRING   — Telethon 사용자 세션(StringSession)
채널 목록: pipeline/sources.yaml → telegram.channels (username 또는 -100… id)

출력 레코드는 collect.py와 같은 형식 (source_type="telegram") → data/news/{YYYYMMDD}.jsonl 에 append.
텔레그램 메시지는 제목이 없으므로 첫 줄(최대 120자)을 title, 전체를 summary(≤1,500자)로 둔다. 링크가 있으면 url에 넣고,
없으면 t.me/{channel}/{msg_id} 를 url로 쓴다.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from kb.query import KB
from pipeline.collect import NEWS_DIR, SOURCES, article_id, load_seen, save_seen, log, load_seen_titles, save_seen_titles, dedup_stories

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
URL_RX = re.compile(r"https?://[^\s<>\"')\]]+")


def _env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if v:
        return v
    # 루트 .env 폴백 (값은 출력하지 않음)
    # 폴백: 이 프로젝트 .env → 텔레그램 봇 프로젝트 .env (같은 키를 공유; 값은 로그에 쓰지 않는다)
    for envf in (ROOT / ".env", ROOT.parent / "antwiki_telegram_bot_v2" / ".env"):
        if envf.exists():
            for line in envf.read_text(encoding="utf-8").splitlines():
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _client():
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    api_id, api_hash, sess = _env("TG_API_ID"), _env("TG_API_HASH"), _env("TG_SESSION_STRING")
    if not (api_id and api_hash and sess):
        raise SystemExit("TG_API_ID / TG_API_HASH / TG_SESSION_STRING 환경변수가 필요합니다 (텔레그램 봇 프로젝트와 동일 값).")
    return TelegramClient(StringSession(sess), int(api_id), api_hash)


def _strip(text: str) -> str:
    text = re.sub(r"[*_`~]+", "", text or "")
    return re.sub(r"[ \t]+", " ", text).strip()


async def _fetch(channels: list[str | int], since: datetime, per_channel: int) -> list[dict]:
    out: list[dict] = []
    async with _client() as client:
        for ch in channels:
            try:
                entity = await client.get_entity(ch)
                uname = getattr(entity, "username", None) or str(ch)
                async for msg in client.iter_messages(entity, limit=per_channel):
                    if not msg.date or msg.date < since:
                        break
                    text = _strip(msg.message or "")
                    if len(text) < 15:
                        continue
                    urls = URL_RX.findall(msg.message or "")
                    # 제목: 날짜/시각·이모지만 있는 줄, URL 줄을 건너뛴 첫 의미 있는 줄
                    first = next((ln.strip(" ✅🚀💻🧬💸📈✍️🔗📰") for ln in text.split("\n")
                                  if len(re.sub(r"[\d.:\s()KSTkst/-]", "", ln)) >= 6 and not URL_RX.search(ln)), text.split("\n", 1)[0])[:120]
                    out.append({
                        "title": first,
                        "summary": text[:1500],
                        "url": urls[0] if urls else f"https://t.me/{uname}/{msg.id}",
                        "source": f"tg:{uname}",
                        "source_type": "telegram",
                        "published_at": msg.date.astimezone(KST).isoformat(),
                        "tg": {"channel": uname, "msg_id": msg.id, "views": getattr(msg, "views", None), "forwards": getattr(msg, "forwards", None)},
                    })
            except Exception as exc:  # noqa: BLE001
                log(f"  채널 {ch} 실패: {type(exc).__name__}: {exc}")
    return out


async def _list_dialogs() -> None:
    async with _client() as client:
        async for d in client.iter_dialogs():
            if d.is_channel:
                e = d.entity
                print(f"{getattr(e, 'username', None) or '-':30} id={d.id:<16} {d.name}")


def run(dry_run: bool, minutes: int | None, per_channel: int) -> list[dict]:
    cfg = yaml.safe_load(SOURCES.read_text(encoding="utf-8"))
    tcfg = cfg.get("telegram") or {}
    channels = tcfg.get("channels") or []
    if not channels:
        log("sources.yaml telegram.channels 가 비어 있음 → 채널을 등록하세요 (--list-dialogs 로 확인)")
        return []
    lookback = minutes or int(tcfg.get("lookback_minutes", 30))
    flt = cfg["filters"]
    kb = KB()
    seen = load_seen()
    now = datetime.now(KST)
    since = now - timedelta(minutes=lookback)
    raw = asyncio.run(_fetch(channels, since, per_channel))
    log(f"텔레그램 원문 {len(raw)}건 ({len(channels)}채널, 최근 {lookback}분)")
    exclude_rx = [re.compile(p) for p in flt.get("exclude_title_patterns", [])] + [re.compile(p) for p in tcfg.get("exclude_patterns", [])]
    accepted: list[dict] = []
    stats = {"dup": 0, "excluded": 0, "nomatch": 0}
    for it in raw:
        aid = article_id(it["url"], it["title"])
        if aid in seen:
            stats["dup"] += 1
            continue
        if any(k in it["title"] for k in flt.get("exclude_title_keywords", [])) or any(rx.search(it["title"]) for rx in exclude_rx):
            stats["excluded"] += 1
            continue
        m = kb.match(f"{it['title']}. {it['summary']}", min_theme_alias_len=int(flt.get("min_theme_alias_len", 3)))
        strong = [t for t in m["themes"] if t.get("alias_len", 0) >= int(flt.get("min_theme_alias_len", 3))]
        has_signal = any(k in it["summary"] for k in flt.get("signal_keywords", []))
        if not (m["stocks"] or (strong and has_signal)):
            stats["nomatch"] += 1
            continue
        rec = {"id": aid, **it, "collected_at": now.isoformat(),
               "match": {"stocks": [{"stock_id": s["stock_id"], "name": s["name"], "has_wiki": s["has_wiki"]} for s in m["stocks"]],
                         "themes": [t["slug"] for t in m["themes"]], "expanded_themes": [t["slug"] for t in m["expanded_themes"]]}}
        accepted.append(rec)
        seen[aid] = now.isoformat()
    history = load_seen_titles()
    accepted, same_story = dedup_stories(accepted, history)
    log(f"채택 {len(accepted)}건 (중복 {stats['dup']}, 제외 {stats['excluded']}, 미매칭 {stats['nomatch']}, 같은재료 {same_story})")
    if dry_run:
        for a in accepted:
            extra = f"  (+{len(a['also_reported_by'])} 채널)" if a.get("also_reported_by") else ""
            print(f"- [{a['source']}] {a['title']}{extra}  → 종목 {[s['name'] for s in a['match']['stocks']]} 테마 {a['match']['themes']}")
        return accepted
    NEWS_DIR.mkdir(parents=True, exist_ok=True)
    out = NEWS_DIR / f"{now:%Y%m%d}.jsonl"
    with out.open("a", encoding="utf-8") as f:
        for a in accepted:
            toks = a.pop("_tokens", [])
            history.append({"at": now.isoformat(), "tokens": toks, "title": a["title"]})
            f.write(json.dumps(a, ensure_ascii=False) + "\n")
    save_seen(seen)
    save_seen_titles(history)
    log(f"저장 → {out.relative_to(ROOT)}")
    return accepted


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--minutes", type=int, default=None)
    ap.add_argument("--per-channel", type=int, default=100)
    ap.add_argument("--list-dialogs", action="store_true")
    a = ap.parse_args(argv)
    if a.list_dialogs:
        asyncio.run(_list_dialogs())
        return 0
    run(a.dry_run, a.minutes, a.per_channel)
    return 0


if __name__ == "__main__":
    sys.exit(main())
