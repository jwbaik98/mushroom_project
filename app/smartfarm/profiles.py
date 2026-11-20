"""표고버섯 재배 단계별 적정 환경 기준 (mushroom_manual.txt 기반).

각 항목은 (최소, 최대) 범위. None 이면 해당 방향 제한 없음.
"""

FARM_TYPES = {
    "container": "🚚 스마트팜 컨테이너",
    "greenhouse": "🏕️ 비닐하우스",
    "indoor": "🏠 실내 재배사",
}

# 센서 항목 메타데이터: key -> (표시명, 단위)
METRICS = {
    "temperature": ("온도", "℃"),
    "humidity": ("습도", "%"),
    "co2": ("CO₂", "ppm"),
    "lux": ("조도", "lux"),
    "substrate_moisture": ("배지 수분", "%"),
}

STAGE_PROFILES = {
    "culture": {
        "label": "배양 (균사 생장)",
        "temperature": (22.0, 26.0),
        "humidity": (85.0, 95.0),
        "co2": (None, 3000.0),
        "lux": (None, 50.0),           # 암상태 유지
        "substrate_moisture": (55.0, 70.0),
        "day_night_diff": None,
        "note": "균사 생장 최적 22~26℃, 습도 90% 내외, 어둡게 유지하고 주기적 환기.",
    },
    "growth": {
        "label": "발생·생육 (자실체)",
        "temperature": (8.0, 22.0),
        "humidity": (80.0, 95.0),
        "co2": (None, 1000.0),
        "lux": (50.0, 500.0),          # 산광 필요
        "substrate_moisture": (55.0, 70.0),
        "day_night_diff": 8.0,         # 변온 관리: 주야간 8~10℃ 이상
        "note": "자실체 형성 8~22℃, 습도 80~95%, 산광 50~500lux, 주야 온도차 8℃ 이상.",
    },
    "rest": {
        "label": "휴양 (수확 후 회복)",
        "temperature": (18.0, 25.0),
        "humidity": (60.0, 75.0),
        "co2": (None, 2000.0),
        "lux": (None, 200.0),
        "substrate_moisture": (45.0, 65.0),
        "day_night_diff": None,
        "note": "수확 후 배지 회복기. 습도를 낮추고 균사 재활력 유도.",
    },
    "disease": {
        "label": "병해 긴급관리",
        "temperature": (15.0, 18.0),
        "humidity": (70.0, 80.0),
        "co2": (None, 800.0),
        "lux": (None, 500.0),
        "substrate_moisture": (None, 65.0),
        "day_night_diff": None,
        "note": "병해 발생 시 습도 75~80% 이하, 온도 15~18℃, 강제 환기.",
    },
}


def get_profile(stage: str, overrides: dict | None = None) -> dict:
    """단계 기준을 가져오고 농장별 사용자 설정(overrides)으로 덮어쓴다."""
    base = dict(STAGE_PROFILES.get(stage, STAGE_PROFILES["growth"]))
    for k, v in (overrides or {}).items():
        if k in METRICS and isinstance(v, (list, tuple)) and len(v) == 2:
            base[k] = (v[0], v[1])
        elif k == "day_night_diff":
            base[k] = v
    return base
