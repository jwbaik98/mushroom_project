"""여러 스마트팜(컨테이너·비닐하우스)을 한 곳에서 관리하는 FarmManager."""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from datetime import datetime

from . import rules
from .drivers import DEVICES, create_driver
from .profiles import FARM_TYPES, STAGE_PROFILES, get_profile
from .storage import Storage

DEFAULT_CONFIG = {
    "poll_interval_sec": 10,
    "min_switch_sec": 60,       # 자동제어 시 같은 장치 최소 전환 간격 (압축기·릴레이 보호)
    "retention_days": 90,       # 측정 데이터 보관 기간
    "notify": {"webhook_url": ""},
    "farms": [
        {"id": "container-1", "name": "컨테이너 1동", "type": "container", "stage": "growth",
         "location": "", "auto_control": True, "sensor": {"driver": "simulator", "base_temperature": 16},
         "actuator": {"driver": "simulator"}, "thresholds": {}},
    ],
}

# 서로 동시에 켜지면 안 되는 장치
INTERLOCK = {"heater": "cooler", "cooler": "heater"}
# 센서 고장 시 안전 상태 (열원·가습 OFF, 환기 ON)
FAILSAFE = {"heater": False, "cooler": False, "humidifier": False, "fan": True}
# 끄는 동작은 최소 전환 간격과 무관하게 즉시 허용하는 장치 (과열·과습 방지)
IMMEDIATE_OFF = {"heater", "humidifier"}


def friendly_error(e: Exception) -> str:
    """통신 예외를 현장 작업자가 이해할 수 있는 문구로 변환."""
    msg = str(e)
    low = msg.lower()
    if "refused" in low:
        return "연결 거부 — 센서 노드 전원/주소(IP·포트) 확인"
    if "timed out" in low or "timeout" in low:
        return "응답 시간 초과 — 와이파이 신호/노드 상태 확인"
    if "name or service not known" in low or "getaddrinfo" in low or "nodename" in low:
        return "주소를 찾을 수 없음 — 센서 URL 확인"
    if "no route" in low or "unreachable" in low:
        return "네트워크 연결 불가 — 공유기/케이블 확인"
    if "could not open port" in low or "filenotfound" in low:
        return "시리얼 포트를 열 수 없음 — USB 연결/포트 번호 확인"
    return msg


class FarmManager:
    def __init__(self, config_path: str, db_path: str):
        self.config_path = config_path
        self.storage = Storage(db_path)
        self.lock = threading.RLock()
        self.config_error: str | None = None
        self.config = self._load()
        self.drivers: dict = {}
        self.actuators: dict = {}
        self.errors: dict = {}
        self.last_switch: dict = {}
        self.last_apply: dict = {}
        self._collector: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_purge = 0.0

    # ---------- 설정 ----------
    def _load(self) -> dict:
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, encoding="utf-8") as f:
                    cfg = json.load(f)
                if not isinstance(cfg.get("farms", []), list):
                    raise ValueError("'farms' 항목이 목록이 아닙니다")
                for k, v in DEFAULT_CONFIG.items():
                    cfg.setdefault(k, v if k != "farms" else [])
                return cfg
            except (ValueError, OSError) as e:
                # 손상된 설정은 백업해두고 기본값으로 기동 (앱 전체 중단 방지)
                backup = f"{self.config_path}.broken-{datetime.now():%Y%m%d%H%M%S}"
                os.replace(self.config_path, backup)
                self.config_error = f"설정 파일 오류로 기본값으로 시작했습니다: {e} (백업: {os.path.basename(backup)})"
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        self._write(cfg)
        return cfg

    def _write(self, cfg):
        tmp = self.config_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.config_path)

    def save(self):
        with self.lock:
            self._write(self.config)

    @property
    def farms(self) -> list[dict]:
        return self.config.get("farms", [])

    def farm(self, farm_id: str) -> dict | None:
        return next((f for f in self.farms if f["id"] == farm_id), None)

    def _new_id(self, ftype: str) -> str:
        prefix = ftype if ftype in FARM_TYPES else "farm"
        n = 1
        while self.farm(f"{prefix}-{n}") or self.storage.latest(f"{prefix}-{n}"):
            n += 1  # 삭제된 농장의 과거 데이터와도 섞이지 않게 사용된 적 없는 ID 사용
        return f"{prefix}-{n}"

    def add_farm(self, name, ftype="container", stage="growth", sensor=None, actuator=None, location=""):
        name = (name or "").strip()
        if not name:
            raise ValueError("농장 이름을 입력하세요")
        if stage not in STAGE_PROFILES:
            raise ValueError(f"알 수 없는 재배 단계: {stage}")
        with self.lock:
            farm = {"id": self._new_id(ftype), "name": name, "type": ftype, "stage": stage, "location": location,
                    "auto_control": True, "sensor": sensor or {"driver": "simulator"},
                    "actuator": actuator or {"driver": "simulator"}, "thresholds": {}}
            self.config.setdefault("farms", []).append(farm)
            self.save()
            return farm

    def update_farm(self, farm_id, **changes):
        with self.lock:
            farm = self.farm(farm_id)
            if not farm:
                raise KeyError(farm_id)
            if "stage" in changes and changes["stage"] != farm.get("stage"):
                if changes["stage"] == "disease":
                    farm["prev_stage"] = farm.get("stage")
                    farm["prev_thresholds"] = farm.get("thresholds", {})
                    changes.setdefault("thresholds", {})  # 긴급관리 기준은 기본값으로 강제
            farm.update(changes)
            if {"sensor", "actuator", "type"} & changes.keys():
                self._close_driver(farm_id)
            self.save()
            return farm

    def restore_stage(self, farm_id):
        """병해 긴급관리 → 직전 재배 단계/기준으로 복귀."""
        farm = self.farm(farm_id)
        if farm and farm.get("prev_stage"):
            self.update_farm(farm_id, stage=farm.pop("prev_stage"),
                             thresholds=farm.pop("prev_thresholds", {}))

    def remove_farm(self, farm_id):
        with self.lock:
            self._close_driver(farm_id)
            self.config["farms"] = [f for f in self.farms if f["id"] != farm_id]
            self.actuators.pop(farm_id, None)
            self.errors.pop(farm_id, None)
            self.save()

    def profile(self, farm_id) -> dict:
        farm = self.farm(farm_id)
        return get_profile(farm.get("stage", "growth"), farm.get("thresholds"))

    # ---------- 드라이버 / 구동기 ----------
    def _driver(self, farm):
        if farm["id"] not in self.drivers:
            self.drivers[farm["id"]] = create_driver(farm)
        return self.drivers[farm["id"]]

    def _close_driver(self, farm_id):
        drv = self.drivers.pop(farm_id, None)
        if drv:
            try:
                drv.close()
            except Exception:
                pass

    def actuator_state(self, farm_id) -> dict:
        return self.actuators.setdefault(farm_id, {d: False for d in DEVICES})

    def _change(self, farm_id, device, on, source) -> bool:
        st = self.actuator_state(farm_id)
        if st.get(device) == on:
            return False
        st[device] = on
        self.last_switch[(farm_id, device)] = time.time()
        self.storage.log_actuator(farm_id, device, on, source)
        partner = INTERLOCK.get(device)
        if on and partner and st.get(partner):
            st[partner] = False
            self.last_switch[(farm_id, partner)] = time.time()
            self.storage.log_actuator(farm_id, partner, False, "interlock")
        return True

    def set_actuator(self, farm_id, device, state, source="manual"):
        if device not in DEVICES:
            raise ValueError(f"알 수 없는 장치: {device}")
        with self.lock:
            if self._change(farm_id, device, bool(state), source):
                self._apply(farm_id)

    def _auto_apply(self, farm_id, desired: dict, source="auto", force=False):
        """자동제어 결과 반영. 최소 전환 간격 내 재전환은 보류(단주기 운전 방지)."""
        min_gap = float(self.config.get("min_switch_sec", 60))
        st, now, changed = self.actuator_state(farm_id), time.time(), False
        for dev, on in desired.items():
            if dev not in DEVICES or st.get(dev) == on:
                continue
            recent = now - self.last_switch.get((farm_id, dev), 0) < min_gap
            if recent and not force and not (not on and dev in IMMEDIATE_OFF):
                continue
            changed |= self._change(farm_id, dev, on, source)
        if changed:
            self._apply(farm_id)
        return changed

    def _apply(self, farm_id):
        farm = self.farm(farm_id)
        try:
            self._driver(farm).apply(self.actuator_state(farm_id))
            self.last_apply[farm_id] = time.time()
        except Exception as e:
            self.storage.add_alert(farm_id, "warning", "actuator", f"구동기 통신 실패: {friendly_error(e)}")

    # ---------- 상태 판단 ----------
    def snapshot(self, farm_id, reading: dict | None = None, ts: str | None = None) -> dict:
        """센서 재측정 없이 최신값으로 경보·위험도·지연 여부를 계산 (화면/수집기 공통)."""
        farm = self.farm(farm_id)
        if not farm:
            raise KeyError(farm_id)
        if farm_id in self.errors:
            return {"farm_id": farm_id, "error": self.errors[farm_id], "actuators": dict(self.actuator_state(farm_id))}
        if reading is None:
            latest = self.storage.latest(farm_id)
            if not latest:
                return {"farm_id": farm_id, "pending": True, "alerts": [], "risks": {},
                        "actuators": dict(self.actuator_state(farm_id))}
            reading, ts = latest, latest["ts"]
        profile = self.profile(farm_id)
        alerts = rules.evaluate(reading, profile, datetime.now().hour)
        risks = rules.disease_risk(reading, self.storage.readings(farm_id, hours=1)[-30:])
        for name, score in risks.items():
            if score >= 70:
                alerts.append({"metric": f"risk:{name}", "level": "critical" if score >= 85 else "warning",
                               "message": f"{name} 발생 위험 {score}점 — 환기·제습 권장"})
        if ts:
            age = (datetime.now() - datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")).total_seconds()
            limit = max(120, 3 * int(self.config.get("poll_interval_sec", 10)))
            if age > limit:
                alerts.insert(0, {"metric": "stale", "level": "critical",
                                  "message": f"센서 데이터 {int(age // 60)}분째 갱신 없음 — 수집기/통신 확인"})
        return {"farm_id": farm_id, "reading": reading, "ts": ts, "alerts": alerts, "risks": risks,
                "day_night_diff": rules.day_night_diff(self.storage.readings(farm_id, hours=24)),
                "day_night_required": profile.get("day_night_diff"),
                "actuators": dict(self.actuator_state(farm_id))}

    def status_level(self, result: dict) -> str:
        if result.get("error"):
            return "critical"
        lv = "normal"
        for a in result.get("alerts", []):
            if rules.LEVEL_ORDER[a["level"]] > rules.LEVEL_ORDER[lv]:
                lv = a["level"]
        return lv

    # ---------- 측정/제어 ----------
    def poll(self, farm_id) -> dict:
        """센서 1회 측정 → 저장 → 경보/위험도 평가 → (자동모드) 구동기 제어."""
        with self.lock:
            farm = self.farm(farm_id)
            if not farm:
                raise KeyError(farm_id)
            state = self.actuator_state(farm_id)
            try:
                reading = self._driver(farm).read(state)
                if not reading:
                    raise ValueError("유효한 센서값이 없습니다 (범위 밖/누락)")
            except Exception as exc:
                e = friendly_error(exc)
                first = farm_id not in self.errors
                self.errors[farm_id] = e
                self._close_driver(farm_id)  # 다음 주기에 재연결 시도
                if self.storage.add_alert(farm_id, "critical", "sensor", f"센서 통신 오류: {e}"):
                    self._notify(farm, [f"센서 통신 오류: {e}"])
                if farm.get("auto_control", True) and first:
                    self._auto_apply(farm_id, FAILSAFE, source="failsafe", force=True)
                return {"farm_id": farm_id, "error": str(e), "actuators": dict(state)}
            self.errors.pop(farm_id, None)

            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.storage.add_reading(farm_id, reading, ts=ts)
            snap = self.snapshot(farm_id, reading, ts)

            new_critical = [a["message"] for a in snap["alerts"]
                            if self.storage.add_alert(farm_id, a["level"], a["metric"], a["message"])
                            and a["level"] == "critical"]
            if new_critical:
                self._notify(farm, new_critical)

            if farm.get("auto_control", True):
                desired = rules.decide_actuators(reading, self.profile(farm_id), state, datetime.now().hour)
                changed = self._auto_apply(farm_id, desired)
            else:
                changed = False
            # 노드 재부팅으로 릴레이가 초기화될 수 있으므로 주기적으로 현재 상태 재전송
            if not changed and time.time() - self.last_apply.get(farm_id, 0) > 60:
                self._apply(farm_id)
            snap["actuators"] = dict(state)
            return snap

    def poll_all(self) -> dict:
        out = {}
        for f in list(self.farms):
            try:
                out[f["id"]] = self.poll(f["id"])
            except Exception as e:  # 한 농장 오류가 다른 농장 수집을 막지 않도록
                out[f["id"]] = {"farm_id": f["id"], "error": str(e)}
        return out

    # ---------- 알림 ----------
    def _notify(self, farm, messages):
        url = (self.config.get("notify") or {}).get("webhook_url")
        if not url:
            return
        text = f"[🍄 {farm['name']}] 위험 경보\n" + "\n".join(f"- {m}" for m in messages)
        body = json.dumps({"text": text, "content": text}, ensure_ascii=False).encode("utf-8")

        def send():  # 별도 스레드: 알림 서버 지연이 측정·제어를 멈추지 않게
            try:
                req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=5).close()
            except Exception as e:
                print(f"[notify] 전송 실패: {e}")

        threading.Thread(target=send, daemon=True).start()

    # ---------- 백그라운드 수집 ----------
    def maintenance(self):
        if time.time() - self._last_purge > 86400:
            self._last_purge = time.time()
            self.storage.purge(int(self.config.get("retention_days", 90)))

    def start_collector(self):
        if self._collector and self._collector.is_alive():
            return
        self._stop.clear()

        def loop():
            while not self._stop.is_set():
                try:
                    self.poll_all()
                    self.maintenance()
                except Exception as e:
                    print(f"[collector] {e}")
                self._stop.wait(max(2, int(self.config.get("poll_interval_sec", 10))))

        self._collector = threading.Thread(target=loop, daemon=True, name="smartfarm-collector")
        self._collector.start()

    def stop_collector(self):
        self._stop.set()

    @property
    def collector_alive(self) -> bool:
        return bool(self._collector and self._collector.is_alive())

    def env_summary(self, farm_id) -> str:
        """LLM 프롬프트에 넣을 현재 환경 요약."""
        farm, latest = self.farm(farm_id), self.storage.latest(farm_id)
        if not farm or not latest:
            return ""
        p = self.profile(farm_id)
        return (f"{farm['name']}({p['label']}) 현재 온도 {latest['temperature']}℃, 습도 {latest['humidity']}%, "
                f"CO2 {latest['co2']}ppm, 조도 {latest['lux']}lux")
