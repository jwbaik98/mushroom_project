"""여러 스마트팜(컨테이너·비닐하우스)을 한 곳에서 관리하는 FarmManager."""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
from datetime import datetime

from . import rules
from .drivers import DEVICES, create_driver
from .profiles import get_profile
from .storage import Storage

DEFAULT_CONFIG = {
    "poll_interval_sec": 10,
    "notify": {"webhook_url": ""},
    "farms": [
        {"id": "container-a", "name": "컨테이너 A동", "type": "container", "stage": "growth",
         "location": "", "auto_control": True, "sensor": {"driver": "simulator", "base_temperature": 16},
         "actuator": {"driver": "simulator"}, "thresholds": {}},
    ],
}


def slugify(text: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "-", text).strip("-").lower()
    return s or f"farm-{int(time.time())}"


class FarmManager:
    def __init__(self, config_path: str, db_path: str):
        self.config_path = config_path
        self.storage = Storage(db_path)
        self.lock = threading.RLock()
        self.config = self._load()
        self.drivers: dict = {}
        self.actuators: dict = {}
        self.errors: dict = {}
        self._collector: threading.Thread | None = None
        self._stop = threading.Event()

    # ---------- 설정 ----------
    def _load(self) -> dict:
        if os.path.exists(self.config_path):
            with open(self.config_path, encoding="utf-8") as f:
                return json.load(f)
        self._write(DEFAULT_CONFIG)
        return json.loads(json.dumps(DEFAULT_CONFIG))

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

    def add_farm(self, name, ftype="container", stage="growth", sensor=None, actuator=None, location=""):
        with self.lock:
            fid, base, n = slugify(name), slugify(name), 2
            while self.farm(fid):
                fid, n = f"{base}-{n}", n + 1
            farm = {"id": fid, "name": name, "type": ftype, "stage": stage, "location": location,
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
            farm.update(changes)
            if "sensor" in changes or "actuator" in changes or "type" in changes:
                self.drivers.pop(farm_id, None)
            self.save()
            return farm

    def remove_farm(self, farm_id):
        with self.lock:
            self.config["farms"] = [f for f in self.farms if f["id"] != farm_id]
            self.drivers.pop(farm_id, None)
            self.actuators.pop(farm_id, None)
            self.save()

    def profile(self, farm_id) -> dict:
        farm = self.farm(farm_id)
        return get_profile(farm.get("stage", "growth"), farm.get("thresholds"))

    # ---------- 측정/제어 ----------
    def _driver(self, farm):
        if farm["id"] not in self.drivers:
            self.drivers[farm["id"]] = create_driver(farm)
        return self.drivers[farm["id"]]

    def actuator_state(self, farm_id) -> dict:
        return self.actuators.setdefault(farm_id, {d: False for d in DEVICES})

    def set_actuator(self, farm_id, device, state, source="manual"):
        with self.lock:
            st = self.actuator_state(farm_id)
            if st.get(device) == bool(state):
                return
            st[device] = bool(state)
            self.storage.log_actuator(farm_id, device, state, source)
            self._apply(farm_id)

    def _apply(self, farm_id):
        farm = self.farm(farm_id)
        try:
            self._driver(farm).apply(self.actuator_state(farm_id))
        except Exception as e:  # 구동기 통신 실패는 경보로 남긴다
            self.storage.add_alert(farm_id, "warning", "actuator", f"구동기 통신 실패: {e}")

    def poll(self, farm_id) -> dict:
        """센서 1회 측정 → 저장 → 경보/위험도 평가 → (자동모드) 구동기 제어."""
        with self.lock:
            farm = self.farm(farm_id)
            if not farm:
                raise KeyError(farm_id)
            profile = self.profile(farm_id)
            state = self.actuator_state(farm_id)
            try:
                reading = self._driver(farm).read(state)
                self.errors.pop(farm_id, None)
            except Exception as e:
                self.errors[farm_id] = str(e)
                self.storage.add_alert(farm_id, "critical", "sensor", f"센서 통신 오류: {e}")
                return {"farm_id": farm_id, "error": str(e)}

            self.storage.add_reading(farm_id, reading)
            alerts = rules.evaluate(reading, profile, datetime.now().hour)
            recent = self.storage.readings(farm_id, hours=1)
            risks = rules.disease_risk(reading, recent[-30:])
            for name, score in risks.items():
                if score >= 70:
                    alerts.append({"metric": f"risk:{name}", "level": "critical" if score >= 85 else "warning",
                                   "message": f"{name} 발생 위험 {score}점 — 환기·제습 권장"})

            dnd = rules.day_night_diff(self.storage.readings(farm_id, hours=24))
            need = profile.get("day_night_diff")

            new_critical = []
            for a in alerts:
                if self.storage.add_alert(farm_id, a["level"], a["metric"], a["message"]) and a["level"] == "critical":
                    new_critical.append(a["message"])
            if new_critical:
                self._notify(farm, new_critical)

            if farm.get("auto_control", True):
                desired = rules.decide_actuators(reading, profile, state, datetime.now().hour)
                changed = False
                for dev, on in desired.items():
                    if state.get(dev) != on:
                        state[dev] = on
                        self.storage.log_actuator(farm_id, dev, on, "auto")
                        changed = True
                if changed:
                    self._apply(farm_id)

            return {"farm_id": farm_id, "reading": reading, "alerts": alerts, "risks": risks,
                    "day_night_diff": dnd, "day_night_required": need, "actuators": dict(state),
                    "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    def poll_all(self) -> dict:
        return {f["id"]: self.poll(f["id"]) for f in list(self.farms)}

    def status_level(self, result: dict) -> str:
        if result.get("error"):
            return "critical"
        lv = "normal"
        for a in result.get("alerts", []):
            if rules.LEVEL_ORDER[a["level"]] > rules.LEVEL_ORDER[lv]:
                lv = a["level"]
        return lv

    # ---------- 알림 ----------
    def _notify(self, farm, messages):
        url = (self.config.get("notify") or {}).get("webhook_url")
        if not url:
            return
        text = f"[🍄 {farm['name']}] 위험 경보\n" + "\n".join(f"- {m}" for m in messages)
        body = json.dumps({"text": text, "content": text}, ensure_ascii=False).encode("utf-8")
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5).close()
        except Exception:
            pass

    # ---------- 백그라운드 수집 ----------
    def start_collector(self):
        if self._collector and self._collector.is_alive():
            return
        self._stop.clear()

        def loop():
            while not self._stop.is_set():
                try:
                    self.poll_all()
                except Exception as e:
                    print(f"[collector] {e}")
                self._stop.wait(max(2, int(self.config.get("poll_interval_sec", 10))))

        self._collector = threading.Thread(target=loop, daemon=True, name="smartfarm-collector")
        self._collector.start()

    def stop_collector(self):
        self._stop.set()

    def env_summary(self, farm_id) -> str:
        """LLM 프롬프트에 넣을 현재 환경 요약."""
        farm, latest = self.farm(farm_id), self.storage.latest(farm_id)
        if not farm or not latest:
            return ""
        p = self.profile(farm_id)
        return (f"{farm['name']}({p['label']}) 현재 온도 {latest['temperature']}℃, 습도 {latest['humidity']}%, "
                f"CO2 {latest['co2']}ppm, 조도 {latest['lux']}lux")
