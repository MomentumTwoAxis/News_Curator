"""Stage 3 — 앤트위키 피드 게시 (antwiki-feed CLI 래퍼).

기본은 dry-run(check)이며, 실제 발행은 --live 를 명시해야 한다. 발행은 운영 피드에 대한 실제 외부 쓰기다.

사용:
  python -m pipeline.publish data/analysis/20261012/abc123.json            # check(dry-run)만
  python -m pipeline.publish data/analysis/20261012/abc123.json --live     # 사람 승인 후 실제 발행
  python -m pipeline.publish --doctor                                       # CLI/권한 상태

분석 JSON의 post 필드(PLAN.md 4-3)를 bundle로 변환해 넘기고, 발행 성공 시 data/published.csv 에 기록한다.
idempotency_key = news-curator-{YYYYMMDD}-{article_id}  (같은 기사 재시도는 같은 키 → 중복 발행 방지)
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
LOG_CSV = ROOT / "data" / "published.csv"
CLI = shutil.which("antwiki-feed") or str(Path.home() / ".local" / "bin" / "antwiki-feed")
DISCLAIMER = "본 글은 정보 제공 목적이며 투자 판단과 책임은 본인에게 있습니다."
BODY_MAX = 4000


def run_cli(*args: str) -> dict:
    proc = subprocess.run([CLI, "--json", *args], capture_output=True, text=True)
    out = proc.stdout.strip() or proc.stderr.strip()
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        data = {"ok": False, "error": {"code": "cli_output", "message": out[:500]}}
    if proc.returncode != 0 and data.get("ok", True):
        data["ok"] = False
    return data


def build_bundle(analysis: dict) -> dict:
    post = dict(analysis.get("post") or {})
    body = (post.get("body_text") or "").strip()
    if DISCLAIMER not in body:
        body = f"{body}\n\n{DISCLAIMER}"
    if len(body) > BODY_MAX:
        raise ValueError(f"body_text {len(body)}자 > {BODY_MAX}자 제한")
    stock_ids = list(dict.fromkeys(post.get("stock_ids") or [b["stock_id"] for b in analysis.get("beneficiaries", [])]))[:10]
    primary = post.get("primary_stock_id") or (stock_ids[0] if stock_ids else None)
    if primary and primary not in stock_ids:
        stock_ids.insert(0, primary)
    news = analysis.get("news") or {}
    return {
        "post": {
            "body_text": body,
            "stance": post.get("stance") or "neutral",
            "stock_ids": stock_ids,
            "primary_stock_id": primary,
            "poll": post.get("poll") or None,  # {"question","options"[2-4],"allow_multiple"?,"ends_at"?}
            "attachments": [],
        },
        "scenario": {
            "pipeline": "news_curator_v1",
            "content_type": "article_relay",
            "source_event_ids": [f"news:{analysis.get('id') or ''}"] if analysis.get("id") else [],
            "source_kind": "news_article",
            "source_role": news.get("source"),
        },
    }


def idempotency_key(analysis: dict) -> str:
    day = (analysis.get("news") or {}).get("published_at") or datetime.now(KST).isoformat()
    aid = analysis.get("id") or "noid"
    return f"news-curator-{day[:10].replace('-', '')}-{aid}"


def log_publish(analysis: dict, key: str, resp: dict) -> None:
    LOG_CSV.parent.mkdir(parents=True, exist_ok=True)
    new = not LOG_CSV.exists()
    pub = resp.get("published") or {}
    with LOG_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["published_at", "article_id", "news_title", "news_url", "post_id", "duplicate", "idempotency_key", "stock_ids", "stance"])
        w.writerow([datetime.now(KST).isoformat(timespec="seconds"), analysis.get("id"), (analysis.get("news") or {}).get("title"),
                    (analysis.get("news") or {}).get("url"), pub.get("id"), resp.get("duplicate"), key,
                    " ".join((analysis.get("post") or {}).get("stock_ids") or []), (analysis.get("post") or {}).get("stance")])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("analysis", nargs="?", help="분석 결과 JSON 경로")
    ap.add_argument("--live", action="store_true", help="실제 발행 (없으면 check만)")
    ap.add_argument("--doctor", action="store_true")
    a = ap.parse_args(argv)

    if a.doctor:
        print(json.dumps(run_cli("doctor"), ensure_ascii=False, indent=1))
        return 0
    if not a.analysis:
        ap.error("analysis 경로가 필요합니다")
    analysis = json.loads(Path(a.analysis).read_text(encoding="utf-8"))
    bundle = build_bundle(analysis)
    tmp = ROOT / "data" / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    bpath = tmp / f"bundle_{analysis.get('id', 'noid')}.json"
    bpath.write_text(json.dumps(bundle, ensure_ascii=False, indent=1), encoding="utf-8")
    key = idempotency_key(analysis)

    chk = run_cli("check", "--bundle", str(bpath))
    print("[check]", json.dumps(chk, ensure_ascii=False)[:800])
    if not chk.get("ok", False) and chk.get("status") != "validated":
        print("check 실패 → 발행 중단")
        return 2
    if not a.live:
        print(f"dry-run 완료. 실제 발행은 --live (idempotency_key={key})")
        return 0
    resp = run_cli("publish", "--bundle", str(bpath), "--idempotency-key", key)
    print("[publish]", json.dumps(resp, ensure_ascii=False)[:800])
    if resp.get("ok", True) and (resp.get("published") or resp.get("status") == "published"):
        log_publish(analysis, key, resp)
        pid = (resp.get("published") or {}).get("id")
        if pid:
            print(f"→ https://www.ant.wiki/feed/{pid}")
        try:
            from pipeline.track import record as _track_record
            _track_record(a.analysis, pid)
        except Exception as exc:  # noqa: BLE001
            print(f"(성과 추적 등록 실패: {exc})")
        return 0
    return 3


if __name__ == "__main__":
    sys.exit(main())
