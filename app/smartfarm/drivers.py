"""센서/구동기 드라이버.

- simulator : 하드웨어 없이 시연/테스트용 가상 환경 (구동기 동작에 반응)
- http      : ESP32/아두이노 등 센서 노드가 제공하는 JSON API (firmware/sensor_node 참고)
- mqtt      : MQTT 브로커 구독/발행 (paho-mqtt 설치 시)
- serial    : USB 시리얼로 JSON 라인을 보내는 센서 (pyserial 설치 시)
"""
from __future__ import annotations

import json
import math
import random
import time
import urllib.request

DEVICES = {"heater": "🔥 히터", "cooler": "❄️ 냉방기", "humidifier": "💧 가습기",
           "fan": "🌀 환기팬", "led": "💡 LED 조명"}

READING_KEYS = ("temperature", "humidity", "co2", "lux", "substrate_moisture")

# 센서가 물리적으로 낼 수 있는 값 범위. 벗어나면 고장/노이즈로 보고 버린다.
VALID_RANGE = {"temperature": (-30.0, 60.0), "humidity": (0.0, 100.0), "co2": (250.0, 20000.0),
               "lux": (0.0, 200000.0), "substrate_moisture": (0.0, 100.0)}


def _normalize(data: dict) -> dict:
    """노드별 서로 다른 키 이름을 표준 키로 맞춘다."""
    alias = {"temp": "temperature", "t": "temperature", "hum": "humidity", "rh": "humidity",
             "h": "humidity", "co2_ppm": "co2", "light": "lux", "moisture": "substrate_moisture"}
    out = {}
    for k, v in data.items():
        k = alias.get(k.lower(), k.lower())
        if k in READING_KEYS and v is not None:
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            lo, hi = VALID_RANGE[k]
            if f == f and lo <= f <= hi:  # f == f : NaN 제외
                out[k] = f
    return out


class BaseDriver:
    def __init__(self, farm: dict):
        self.farm = farm
        self.cfg = farm.get("sensor", {})
        self.act_cfg = farm.get("actuator", {})

    def read(self, actuators: dict) -> dict:
        raise NotImplementedError

    def apply(self, actuators: dict) -> None:
        """구동기 상태를 실제 장치에 전송 (기본: 아무것도 하지 않음)."""

    def close(self) -> None:
        """연결 자원(시리얼 포트, MQTT 세션) 해제."""


class SimulatorDriver(BaseDriver):
    """주야간 온도변화 + 노이즈 + 구동기 효과를 반영하는 가상 재배사."""

    def __init__(self, farm: dict):
        super().__init__(farm)
        seed = sum(map(ord, farm.get("id", "x")))
        self.rng = random.Random(seed)
        greenhouse = farm.get("type") == "greenhouse"
        # 비닐하우스는 외기 영향이 커서 일교차·조도 변동이 크다
        self.swing = 7.0 if greenhouse else 2.5
        self.base_t = self.cfg.get("base_temperature", 17.0)
        self.state = {"temperature": self.base_t, "humidity": self.cfg.get("base_humidity", 85.0),
                      "co2": 800.0, "lux": 100.0, "substrate_moisture": 62.0}
        self.greenhouse = greenhouse
        self.last = time.time()

    def read(self, actuators: dict) -> dict:
        now = time.time()
        dt = min(60.0, max(1.0, now - self.last)) / 10.0  # 경과시간 스케일
        self.last = now
        hour = time.localtime(now).tm_hour + time.localtime(now).tm_min / 60
        s, r = self.state, self.rng

        ambient = self.base_t + self.swing * math.sin((hour - 9) / 24 * 2 * math.pi)
        s["temperature"] += (ambient - s["temperature"]) * 0.1 * dt
        if actuators.get("heater"):
            s["temperature"] += 0.6 * dt
        if actuators.get("cooler"):
            s["temperature"] -= 0.6 * dt
        if actuators.get("fan"):
            s["temperature"] += (ambient - 2 - s["temperature"]) * 0.05 * dt

        s["humidity"] += (82 - s["humidity"]) * 0.05 * dt + r.uniform(-0.8, 0.8)
        if actuators.get("humidifier"):
            s["humidity"] += 1.5 * dt
        if actuators.get("fan"):
            s["humidity"] -= 1.2 * dt

        s["co2"] += 25 * dt + r.uniform(-15, 15)  # 균사 호흡으로 CO2 증가
        if actuators.get("fan"):
            s["co2"] -= 90 * dt

        sun = max(0.0, math.sin((hour - 6) / 12 * math.pi)) if 6 <= hour < 18 else 0.0
        natural = (800 if self.greenhouse else 30) * sun
        s["lux"] = natural + (300 if actuators.get("led") else 0) + r.uniform(0, 10)

        s["substrate_moisture"] += (0.15 if actuators.get("humidifier") else -0.05) * dt

        s["humidity"] = max(30.0, min(99.9, s["humidity"]))
        s["co2"] = max(400.0, min(5000.0, s["co2"]))
        s["substrate_moisture"] = max(30.0, min(85.0, s["substrate_moisture"]))
        return {k: round(v + r.uniform(-0.1, 0.1), 1) for k, v in s.items()}


class HttpDriver(BaseDriver):
    """GET {url} → {"temperature":..,"humidity":..,...}
    POST {actuator.url} ← {"heater":true,"fan":false,...}"""

    def read(self, actuators: dict) -> dict:
        url = self.cfg["url"]
        with urllib.request.urlopen(url, timeout=self.cfg.get("timeout", 3)) as resp:
            return _normalize(json.loads(resp.read().decode("utf-8")))

    def apply(self, actuators: dict) -> None:
        url = self.act_cfg.get("url")
        if not url:
            return
        body = json.dumps(actuators).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=self.act_cfg.get("timeout", 3)).close()


class MqttDriver(BaseDriver):
    """sensor.topic 을 구독해 마지막 메시지를 사용, actuator.topic 으로 상태 발행."""

    def __init__(self, farm: dict):
        super().__init__(farm)
        import paho.mqtt.client as mqtt  # 선택 의존성

        self.latest: dict = {}
        self.client = mqtt.Client()
        if self.cfg.get("username"):
            self.client.username_pw_set(self.cfg["username"], self.cfg.get("password"))
        self.client.on_message = self._on_message
        self.client.connect(self.cfg.get("host", "localhost"), int(self.cfg.get("port", 1883)))
        self.client.subscribe(self.cfg["topic"])
        self.client.loop_start()

    def _on_message(self, _c, _u, msg):
        try:
            self.latest = _normalize(json.loads(msg.payload.decode("utf-8")))
            self.latest_at = time.time()
        except Exception:
            pass

    def read(self, actuators: dict) -> dict:
        if not self.latest:
            raise RuntimeError("MQTT 수신 데이터 없음")
        if time.time() - getattr(self, "latest_at", 0) > self.cfg.get("max_age_sec", 120):
            raise RuntimeError("MQTT 센서 메시지가 2분 이상 수신되지 않음")
        return dict(self.latest)

    def apply(self, actuators: dict) -> None:
        topic = self.act_cfg.get("topic")
        if topic:
            self.client.publish(topic, json.dumps(actuators), retain=True)

    def close(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()


class SerialDriver(BaseDriver):
    """시리얼 포트에서 JSON 한 줄을 읽는다. 예: {"temp":18.2,"hum":88}"""

    def __init__(self, farm: dict):
        super().__init__(farm)
        import serial  # 선택 의존성 (pyserial)

        self.ser = serial.Serial(self.cfg["port"], int(self.cfg.get("baudrate", 9600)), timeout=2)

    def read(self, actuators: dict) -> dict:
        for _ in range(5):
            line = self.ser.readline().decode("utf-8", "ignore").strip()
            if line.startswith("{"):
                return _normalize(json.loads(line))
        raise RuntimeError("시리얼 데이터 없음")

    def apply(self, actuators: dict) -> None:
        self.ser.write((json.dumps(actuators) + "\n").encode("utf-8"))

    def close(self) -> None:
        self.ser.close()


DRIVERS = {"simulator": SimulatorDriver, "http": HttpDriver, "mqtt": MqttDriver, "serial": SerialDriver}


def create_driver(farm: dict) -> BaseDriver:
    name = farm.get("sensor", {}).get("driver", "simulator")
    if name not in DRIVERS:
        raise ValueError(f"알 수 없는 센서 드라이버: {name}")
    return DRIVERS[name](farm)
