"""화면(Streamlit) 없이 24시간 센서 수집·자동제어만 실행하는 서비스.

사용법:  python app/collector.py            (farms.json 의 주기로 계속 실행)
        python app/collector.py --once     (1회 측정 후 결과 출력)
"""
import argparse
import os
import sys
import time

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)

from smartfarm import FarmManager  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="표고버섯 스마트팜 수집기")
    ap.add_argument("--once", action="store_true", help="1회만 측정")
    ap.add_argument("--config", default=os.path.join(APP_DIR, "farms.json"))
    ap.add_argument("--db", default=os.path.join(APP_DIR, "smartfarm.db"))
    args = ap.parse_args()

    m = FarmManager(args.config, args.db)
    while True:
        for fid, r in m.poll_all().items():
            name = m.farm(fid)["name"]
            if r.get("error"):
                print(f"[{name}] 센서 오류: {r['error']}")
                continue
            if r.get("pending") or "reading" not in r:
                continue
            rd = r["reading"]
            on = [d for d, v in r["actuators"].items() if v]
            print(f"[{name}] {rd['temperature']}℃ {rd['humidity']}% CO2 {rd['co2']}ppm "
                  f"{rd['lux']}lux | {m.status_level(r)} | ON: {','.join(on) or '-'}")
            for a in r["alerts"]:
                print(f"   ⚠ {a['message']}")
        m.maintenance()  # 보관기간 지난 데이터 정리 (하루 1회)
        if args.once:
            break
        time.sleep(max(2, int(m.config.get("poll_interval_sec", 10))))


if __name__ == "__main__":
    main()
