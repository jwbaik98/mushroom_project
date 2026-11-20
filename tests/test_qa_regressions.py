"""QA 결함 재현/회귀 테스트."""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from smartfarm import FarmManager, get_profile, rules  # noqa: E402
from smartfarm.drivers import _normalize  # noqa: E402


FID = "container-1"


def _mgr(tmp_path):
    return FarmManager(str(tmp_path / "farms.json"), str(tmp_path / "sf.db"))


def test_heater_turns_off_when_temperature_missing():
    """[결함] 온도센서 값 누락 시 히터가 켜진 채로 유지됨 → 과열/화재 위험."""
    p = get_profile("growth")
    s = rules.decide_actuators({"humidity": 85}, p, {"heater": True, "humidifier": True}, hour=12)
    assert s["heater"] is False and s["cooler"] is False


def test_humidifier_turns_off_when_humidity_missing():
    p = get_profile("growth")
    s = rules.decide_actuators({"temperature": 15}, p, {"humidifier": True}, hour=12)
    assert s["humidifier"] is False


def test_invalid_sensor_values_are_rejected():
    """[결함] NaN·물리적으로 불가능한 값이 그대로 저장/제어에 사용됨."""
    r = _normalize({"temperature": float("nan"), "humidity": 150, "co2": -1, "lux": 300, "temp2": 1})
    assert r == {"lux": 300.0}
    assert _normalize({"temp": "-999"}) == {}


def test_long_range_history_not_truncated(tmp_path):
    """[결함] 7일 조회 시 최근 5000건(약 14시간)만 반환되어 그래프가 잘림."""
    m = _mgr(tmp_path)
    start = datetime.now() - timedelta(days=6)
    rows = [(FID, (start + timedelta(seconds=60 * i)).strftime("%Y-%m-%d %H:%M:%S"),
             15.0, 85.0, 800.0, 100.0, 60.0) for i in range(8000)]
    m.storage.conn.executemany("INSERT INTO readings (farm_id, ts, temperature, humidity, co2, lux, "
                               "substrate_moisture) VALUES (?,?,?,?,?,?,?)", rows)
    m.storage.conn.commit()
    data = m.storage.readings(FID, hours=168)
    assert data[0]["ts"][:10] == start.strftime("%Y-%m-%d")
    assert len(data) <= 2000


def test_korean_farm_name_gets_readable_unique_id(tmp_path):
    """[결함] 한글 이름은 ID가 'c', '3' 또는 타임스탬프가 되어 식별 불가."""
    m = _mgr(tmp_path)
    a = m.add_farm("비닐하우스", ftype="greenhouse")
    b = m.add_farm("비닐하우스", ftype="greenhouse")
    assert a["id"].startswith("greenhouse-") and a["id"] != b["id"]


def test_driver_closed_when_sensor_reconfigured(tmp_path):
    """[결함] 센서 설정 변경 시 기존 시리얼/MQTT 연결이 닫히지 않아 포트 재사용 불가."""
    m = _mgr(tmp_path)
    m.poll(FID)
    closed = []
    m.drivers[FID].close = lambda: closed.append(True)
    m.update_farm(FID, sensor={"driver": "simulator", "base_temperature": 20})
    assert closed == [True]


def test_sensor_failure_puts_actuators_in_safe_state(tmp_path):
    """[결함] 센서 통신이 끊기면 마지막 제어 상태(히터·가습기 ON)가 무기한 유지됨."""
    m = _mgr(tmp_path)
    m.actuators[FID] = {"heater": True, "cooler": False, "humidifier": True, "fan": False, "led": True}

    class Broken:
        def read(self, _):
            raise IOError("timeout")

        def apply(self, _):
            pass

        def close(self):
            pass

    m.drivers[FID] = Broken()
    r = m.poll(FID)
    assert r["error"]
    st = m.actuator_state(FID)
    assert not st["heater"] and not st["humidifier"]


def test_heater_cooler_interlock_manual(tmp_path):
    """[결함] 수동 모드에서 히터와 냉방기를 동시에 켤 수 있음."""
    m = _mgr(tmp_path)
    m.set_actuator(FID, "heater", True)
    m.set_actuator(FID, "cooler", True)
    st = m.actuator_state(FID)
    assert st["cooler"] and not st["heater"]


def test_auto_control_minimum_switch_interval(tmp_path):
    """[결함] 경계값에서 냉방기가 매 측정마다 ON/OFF 반복(단주기 운전) → 압축기 고장."""
    m = _mgr(tmp_path)
    m.config["min_switch_sec"] = 120
    m._auto_apply(FID, {"cooler": True})
    m._auto_apply(FID, {"cooler": False})
    assert m.actuator_state(FID)["cooler"] is True


def test_stale_data_detected(tmp_path):
    """[결함] 수집이 멈춰도 마지막 값이 '정상'으로 계속 표시됨."""
    m = _mgr(tmp_path)
    old = (datetime.now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    m.storage.add_reading(FID, {"temperature": 15, "humidity": 85, "co2": 700, "lux": 100}, ts=old)
    snap = m.snapshot(FID)
    assert any(a["metric"] == "stale" for a in snap["alerts"])
    assert m.status_level(snap) == "critical"


def test_day_night_diff_needs_enough_history(tmp_path):
    """[결함] 수집 시작 직후 몇 분 데이터로 '변온관리 미달' 오경보."""
    now = datetime.now()
    hist = [{"ts": (now - timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S"), "temperature": 15}
            for i in range(10)]
    assert rules.day_night_diff(hist) is None


def test_broken_config_file_does_not_crash(tmp_path):
    """[결함] farms.json 이 손상되면 앱 전체가 시작되지 않음."""
    cfg = tmp_path / "farms.json"
    cfg.write_text("{ broken", encoding="utf-8")
    m = FarmManager(str(cfg), str(tmp_path / "sf.db"))
    assert m.farms and m.config_error
    assert list(tmp_path.glob("farms.json.broken-*"))
