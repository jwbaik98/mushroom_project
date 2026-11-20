"""센서값 → 경보 / 병해 위험도 / 자동제어 판단 로직 (하드웨어 독립, 테스트 가능)."""
from __future__ import annotations

from .profiles import METRICS

LEVEL_ORDER = {"normal": 0, "warning": 1, "critical": 2}


def _check_range(value, rng, margin):
    lo, hi = rng
    if lo is not None and value < lo:
        return ("critical" if value < lo - margin else "warning"), "low"
    if hi is not None and value > hi:
        return ("critical" if value > hi + margin else "warning"), "high"
    return "normal", None


# 항목별 위험 경보 여유폭 (기준 이탈량이 이 값을 넘으면 critical)
CRITICAL_MARGIN = {"temperature": 3.0, "humidity": 7.0, "co2": 500.0, "lux": 200.0,
                   "substrate_moisture": 10.0}


def evaluate(reading: dict, profile: dict, hour: int | None = None) -> list[dict]:
    """기준 범위를 벗어난 항목을 경보 리스트로 반환. 조도 부족은 주간(06~18시)에만 판단."""
    alerts = []
    night = hour is not None and not (6 <= hour < 18)
    for key, (name, unit) in METRICS.items():
        val = reading.get(key)
        rng = profile.get(key)
        if val is None or rng is None:
            continue
        level, direction = _check_range(float(val), rng, CRITICAL_MARGIN[key])
        if level == "normal" or (key == "lux" and direction == "low" and night):
            continue
        lo, hi = rng
        target = f"{lo if lo is not None else ''}~{hi if hi is not None else ''}{unit}"
        word = "낮음" if direction == "low" else "높음"
        alerts.append({
            "metric": key, "level": level, "direction": direction,
            "message": f"{name} {word}: {val:.1f}{unit} (기준 {target})",
        })
    return alerts


def disease_risk(reading: dict, recent: list[dict] | None = None) -> dict:
    """매뉴얼의 병해 발생 조건을 점수화 (0~100).

    recent: 최근 측정값 리스트(오래된→최신). 급격한 온도 하강(결로) 판단에 사용.
    """
    t = reading.get("temperature")
    h = reading.get("humidity")
    co2 = reading.get("co2") or 0
    risks = {}
    if t is None or h is None:
        return risks

    # 푸른곰팡이병: 25℃ 이상 고온 + 90% 이상 고습 + 환기 부족
    s = 0
    s += min(40, max(0, (t - 23) * 10))
    s += min(40, max(0, (h - 85) * 6))
    s += 20 if co2 > 1500 else 0
    risks["푸른곰팡이병"] = int(min(100, s))

    # 흰곰팡이병: 습도 95% 이상, 공기순환 불량
    s = min(70, max(0, (h - 90) * 10)) + (30 if co2 > 1200 else 0)
    risks["흰곰팡이병"] = int(min(100, s))

    # 털곰팡이병: 고온·고습, 배지 수분 과다
    sm = reading.get("substrate_moisture") or 0
    s = min(35, max(0, (t - 22) * 7)) + min(35, max(0, (h - 85) * 5)) + min(30, max(0, (sm - 70) * 3))
    risks["털곰팡이병"] = int(min(100, s))

    # 세균성 갈색무늬병: 85% 초과 습도 + 온도 급변(결로)
    s = min(60, max(0, (h - 85) * 6))
    if recent:
        temps = [r["temperature"] for r in recent if r.get("temperature") is not None]
        if len(temps) >= 2 and (max(temps) - t) >= 4:
            s += 40
    risks["세균성 갈색무늬병"] = int(min(100, s))
    return risks


def day_night_diff(history: list[dict]) -> float | None:
    temps = [r["temperature"] for r in history if r.get("temperature") is not None]
    if len(temps) < 2:
        return None
    return round(max(temps) - min(temps), 1)


def _mid(rng):
    lo, hi = rng
    if lo is None:
        return hi
    if hi is None:
        return lo
    return (lo + hi) / 2


def decide_actuators(reading: dict, profile: dict, current: dict, hour: int) -> dict:
    """히스테리시스(기준 이탈 시 ON, 중앙값 도달 시 OFF) 기반 자동제어.

    장치: heater, cooler, humidifier, fan(환기), led
    """
    s = dict(current)
    t, h = reading.get("temperature"), reading.get("humidity")
    co2, lux = reading.get("co2"), reading.get("lux")

    if t is not None and profile.get("temperature"):
        lo, hi = profile["temperature"]
        mid = _mid(profile["temperature"])
        if lo is not None and t < lo:
            s["heater"], s["cooler"] = True, False
        elif hi is not None and t > hi:
            s["cooler"], s["heater"] = True, False
        else:
            if s.get("heater") and t >= mid:
                s["heater"] = False
            if s.get("cooler") and t <= mid:
                s["cooler"] = False

    fan_reasons = []
    if h is not None and profile.get("humidity"):
        lo, hi = profile["humidity"]
        mid = _mid(profile["humidity"])
        if lo is not None and h < lo:
            s["humidifier"] = True
        elif hi is not None and h > hi:
            s["humidifier"] = False
            fan_reasons.append("humidity")
        elif s.get("humidifier") and h >= mid:
            s["humidifier"] = False
        if s.get("fan") and h > mid and hi is not None and h > hi - 2:
            fan_reasons.append("humidity")

    if co2 is not None and profile.get("co2") and profile["co2"][1] is not None:
        hi = profile["co2"][1]
        if co2 > hi or (s.get("fan") and co2 > hi * 0.8):
            fan_reasons.append("co2")
    s["fan"] = bool(fan_reasons)

    # 조명: 주간(06~18시)에만 산광 보충, 상한 초과/야간 OFF
    if lux is not None and profile.get("lux"):
        lo, hi = profile["lux"]
        daytime = 6 <= hour < 18
        if not daytime or (hi is not None and lux > hi):
            s["led"] = False
        elif lo is not None and lux < lo:
            s["led"] = True
    return s
