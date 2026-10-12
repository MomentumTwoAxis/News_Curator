"""속보형 자동 루프 — 수집(텔레그램 → RSS) → 미분석 건 LLM 분석 → (웹 페이지는 파일을 읽으므로 즉시 반영).

사용:
  .venv/bin/python -m pipeline.loop                 # 10분 주기 무한 루프 (Ctrl+C 로 종료)
  .venv/bin/python -m pipeline.loop --once          # 1회 실행
  .venv/bin/python -m pipeline.loop --interval 5 --workers 4 --no-rss

발행은 포함하지 않는다(발행은 사람 승인 후 pipeline.publish --live).
"""
from __future__ import annotations

import argparse
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))


def log(msg: str) -> None:
    print(f"[{datetime.now(KST):%H:%M:%S}] [loop] {msg}", flush=True)


def run_once(use_rss: bool, use_telegram: bool, workers: int, limit: int, tg_minutes: int) -> None:
    if use_telegram:
        try:
            from pipeline import collect_telegram
            collect_telegram.run(dry_run=False, minutes=tg_minutes, per_channel=60)
        except SystemExit as exc:
            log(f"텔레그램 건너뜀: {exc}")
        except Exception:  # noqa: BLE001
            log("텔레그램 수집 오류\n" + traceback.format_exc(limit=2))
    if use_rss:
        try:
            from pipeline import collect
            collect.run(dry_run=False, use_naver=True, hot_n=0)
        except Exception:  # noqa: BLE001
            log("RSS 수집 오류\n" + traceback.format_exc(limit=2))
    try:
        from pipeline import analyze
        analyze.main(["--pending", "--limit", str(limit), "--workers", str(workers)])
    except Exception:  # noqa: BLE001
        log("분석 오류\n" + traceback.format_exc(limit=2))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--interval", type=int, default=10, help="분")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--limit", type=int, default=30, help="주기당 최대 분석 건수")
    ap.add_argument("--no-rss", action="store_true")
    ap.add_argument("--no-telegram", action="store_true")
    a = ap.parse_args(argv)
    while True:
        log("사이클 시작")
        run_once(not a.no_rss, not a.no_telegram, a.workers, a.limit, tg_minutes=max(a.interval + 5, 30))
        log("사이클 끝")
        if a.once:
            return 0
        time.sleep(a.interval * 60)


if __name__ == "__main__":
    sys.exit(main())
