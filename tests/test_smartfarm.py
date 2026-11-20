import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from smartfarm import FarmManager, get_profile  # noqa: E402
from smartfarm import rules  # noqa: E402


def test_evaluate_levels():
    p = get_profile("growth")
    alerts = rules.evaluate({"temperature": 27, "humidity": 85, "co2": 1200, "lux": 100}, p)
    by = {a["metric"]: a for a in alerts}
    assert by["temperature"]["level"] == "critical"   # 22 + 3 초과
    assert by["co2"]["level"] == "warning"
    assert "humidity" not in by


def test_threshold_override():
    p = get_profile("growth", {"temperature": [10, 15]})
    assert p["temperature"] == (10, 15)
    assert rules.evaluate({"temperature": 16}, p)[0]["direction"] == "high"


def test_disease_risk_trichoderma():
    r = rules.disease_risk({"temperature": 27, "humidity": 95, "co2": 2000})
    assert r["푸른곰팡이병"] >= 90
    safe = rules.disease_risk({"temperature": 16, "humidity": 82, "co2": 600})
    assert max(safe.values()) < 30


def test_actuator_hysteresis():
    p = get_profile("growth")
    s = rules.decide_actuators({"temperature": 25, "humidity": 70, "co2": 1500, "lux": 10}, p, {}, hour=12)
    assert s["cooler"] and s["humidifier"] and s["fan"] and s["led"]
    # 기준 안이지만 중앙값(15) 전이면 냉방 유지
    s = rules.decide_actuators({"temperature": 18}, p, s, hour=12)
    assert s["cooler"]
    s = rules.decide_actuators({"temperature": 14}, p, s, hour=12)
    assert not s["cooler"]
    s = rules.decide_actuators({"lux": 10}, p, s, hour=22)
    assert not s["led"]


def test_manager_multi_farm(tmp_path):
    m = FarmManager(str(tmp_path / "farms.json"), str(tmp_path / "sf.db"))
    m.add_farm("Greenhouse 2", ftype="greenhouse", stage="culture")
    assert len(m.farms) == 2
    res = m.poll_all()
    for fid, r in res.items():
        assert "reading" in r and r["reading"]["temperature"] is not None
        assert m.storage.latest(fid) is not None
    m.set_actuator("container-a", "fan", True)
    assert m.storage.actuator_log("container-a")[0]["source"] == "manual"
    m.remove_farm("greenhouse-2")
    assert len(FarmManager(str(tmp_path / "farms.json"), str(tmp_path / "sf2.db")).farms) == 1


def test_lux_low_ignored_at_night():
    p = get_profile("growth")
    assert rules.evaluate({"lux": 5}, p, hour=23) == []
    assert rules.evaluate({"lux": 5}, p, hour=12)[0]["metric"] == "lux"
