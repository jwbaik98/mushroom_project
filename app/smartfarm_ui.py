"""Streamlit 스마트팜 화면: 통합 대시보드 / 농장 모니터링·제어 / 농장 관리."""
from __future__ import annotations

import json
import os
from datetime import datetime

import pandas as pd
import streamlit as st

from smartfarm import FARM_TYPES, STAGE_PROFILES, FarmManager
from smartfarm.drivers import DEVICES
from smartfarm.profiles import METRICS

APP_DIR = os.path.dirname(os.path.abspath(__file__))
LEVEL_STYLE = {"normal": ("#4CAF50", "정상"), "warning": ("#FF9800", "주의"), "critical": ("#FF5252", "위험")}


@st.cache_resource
def get_farm_manager() -> FarmManager:
    m = FarmManager(os.path.join(APP_DIR, "farms.json"), os.path.join(APP_DIR, "smartfarm.db"))
    # 화면을 보지 않아도 백그라운드에서 계속 수집·자동제어
    # (collector.py 를 별도 서비스로 돌릴 때는 farms.json 에 "ui_collector": false)
    if m.config.get("ui_collector", True):
        m.start_collector()
    return m


def _fmt(v, unit):
    return "-" if v is None else f"{v:.1f}{unit}" if isinstance(v, float) else f"{v}{unit}"


def _latest_result(m: FarmManager, fid: str) -> dict:
    """최신 측정값 기반 상태 (센서 재측정 없이 DB 조회)."""
    from smartfarm import rules

    latest = m.storage.latest(fid)
    if fid in m.errors:
        return {"error": m.errors[fid]}
    if not latest:
        return m.poll(fid)
    profile = m.profile(fid)
    recent = m.storage.readings(fid, hours=1)
    risks = rules.disease_risk(latest, recent[-30:])
    alerts = rules.evaluate(latest, profile, datetime.now().hour)
    for name, sc in risks.items():
        if sc >= 70:
            alerts.append({"metric": f"risk:{name}", "level": "critical" if sc >= 85 else "warning",
                           "message": f"{name} 위험 {sc}점"})
    return {"reading": latest, "alerts": alerts, "risks": risks, "actuators": m.actuator_state(fid),
            "day_night_diff": rules.day_night_diff(m.storage.readings(fid, hours=24)),
            "day_night_required": profile.get("day_night_diff"), "ts": latest["ts"]}


# ---------------------------------------------------------------- 통합 대시보드
def render_dashboard(m: FarmManager):
    st.write("### 🏠 전체 농장 통합 현황")
    if not m.farms:
        st.info("등록된 농장이 없습니다. '⚙️ 농장 관리'에서 컨테이너/비닐하우스를 추가하세요.")
        return

    @st.fragment(run_every=m.config.get("poll_interval_sec", 10))
    def cards():
        results = {f["id"]: _latest_result(m, f["id"]) for f in m.farms}
        levels = [m.status_level(r) for r in results.values()]
        c = st.columns(4)
        c[0].metric("등록 농장", f"{len(m.farms)}개")
        c[1].metric("정상", f"{levels.count('normal')}개")
        c[2].metric("주의", f"{levels.count('warning')}개")
        c[3].metric("위험", f"{levels.count('critical')}개")

        cols = st.columns(min(3, len(m.farms)))
        for i, farm in enumerate(m.farms):
            r = results[farm["id"]]
            color, word = LEVEL_STYLE[m.status_level(r)]
            stage = STAGE_PROFILES.get(farm.get("stage"), {}).get("label", "-")
            with cols[i % len(cols)]:
                if r.get("error"):
                    body = f"<span style='color:{color}'>센서 오류: {r['error']}</span>"
                else:
                    rd = r["reading"]
                    on = [DEVICES[d] for d, v in r["actuators"].items() if v]
                    top_risk = max(r["risks"].items(), key=lambda x: x[1]) if r["risks"] else ("-", 0)
                    body = (f"🌡️ {_fmt(rd['temperature'], '℃')} · 💧 {_fmt(rd['humidity'], '%')}<br>"
                            f"🫧 CO₂ {_fmt(rd['co2'], 'ppm')} · 💡 {_fmt(rd['lux'], 'lux')}<br>"
                            f"🦠 최고 병해위험: {top_risk[0]} {top_risk[1]}점<br>"
                            f"⚙️ {'자동' if farm.get('auto_control', True) else '수동'} | 가동: {', '.join(on) or '없음'}<br>"
                            f"<small>측정: {r.get('ts', '-')}</small>")
                st.markdown(f"""<div class="result-card" style="border-left:6px solid {color};">
                    <b>{FARM_TYPES.get(farm.get('type'), '🏭').split()[0]} {farm['name']}</b>
                    <span style="float:right;color:{color};font-weight:bold;">● {word}</span><br>
                    <small>{stage} · {farm.get('location', '')}</small><hr style="margin:6px 0">{body}</div>""",
                            unsafe_allow_html=True)

        st.write("#### 🔔 최근 경보")
        al = m.storage.alerts(limit=15)
        if al:
            names = {f["id"]: f["name"] for f in m.farms}
            df = pd.DataFrame(al)[["ts", "farm_id", "level", "message"]]
            df["farm_id"] = df["farm_id"].map(lambda x: names.get(x, x))
            df["level"] = df["level"].map(lambda x: LEVEL_STYLE.get(x, ("", x))[1])
            df.columns = ["일시", "농장", "등급", "내용"]
            st.dataframe(df, hide_index=True, width="stretch")
        else:
            st.caption("경보 없음")

    cards()

    st.write("#### 📈 농장별 비교")
    metric = st.selectbox("비교 항목", list(METRICS), format_func=lambda k: f"{METRICS[k][0]} ({METRICS[k][1]})")
    hours = st.select_slider("기간", [1, 6, 12, 24, 72, 168], value=24, format_func=lambda h: f"{h}시간")
    frames = []
    for farm in m.farms:
        rows = m.storage.readings(farm["id"], hours=hours)
        if rows:
            df = pd.DataFrame(rows)[["ts", metric]].rename(columns={metric: farm["name"]})
            df["ts"] = pd.to_datetime(df["ts"]).dt.floor("min")
            frames.append(df.groupby("ts").mean())
    if frames:
        st.line_chart(pd.concat(frames, axis=1))


# ---------------------------------------------------------------- 농장 상세
def render_farm_detail(m: FarmManager, fid: str):
    farm = m.farm(fid)
    if not farm:
        st.warning("농장을 선택하세요.")
        return
    profile = m.profile(fid)
    st.write(f"### {FARM_TYPES.get(farm['type'], '🏭').split()[0]} {farm['name']} — {profile['label']}")
    st.caption(f"📖 관리 기준: {profile['note']}")

    @st.fragment(run_every=m.config.get("poll_interval_sec", 10))
    def live():
        r = _latest_result(m, fid)
        if r.get("error"):
            st.error(f"센서 통신 오류: {r['error']}")
            return
        rd = r["reading"]
        cols = st.columns(len(METRICS))
        for col, (k, (name, unit)) in zip(cols, METRICS.items()):
            rng = profile.get(k)
            target = "" if not rng else f"기준 {rng[0] if rng[0] is not None else ''}~{rng[1] if rng[1] is not None else ''}"
            col.metric(f"{name}", _fmt(rd.get(k), unit), target, delta_color="off")

        if profile.get("day_night_diff"):
            dnd = r.get("day_night_diff")
            ok = dnd is not None and dnd >= profile["day_night_diff"]
            (st.success if ok else st.warning)(
                f"🌗 24시간 주야 온도차 {dnd if dnd is not None else '-'}℃ (변온관리 목표 {profile['day_night_diff']}℃ 이상)")

        for a in r["alerts"]:
            (st.error if a["level"] == "critical" else st.warning)(a["message"])

        st.write("#### 🦠 병해 발생 위험도")
        rc = st.columns(len(r["risks"]) or 1)
        for col, (name, sc) in zip(rc, r["risks"].items()):
            col.progress(sc / 100, text=f"{name} {sc}점")

    live()

    st.write("#### ⚙️ 환경 제어")
    auto = st.toggle("🤖 자동 제어 (기준값 기반 히스테리시스 제어)", value=farm.get("auto_control", True), key=f"auto_{fid}")
    if auto != farm.get("auto_control", True):
        m.update_farm(fid, auto_control=auto)
        st.rerun()
    state = m.actuator_state(fid)
    dcols = st.columns(len(DEVICES))
    for col, (dev, label) in zip(dcols, DEVICES.items()):
        val = col.toggle(label, value=state.get(dev, False), key=f"{fid}_{dev}", disabled=auto)
        if not auto and val != state.get(dev, False):
            m.set_actuator(fid, dev, val, source="manual")
    if auto:
        st.caption("자동 제어 중에는 수동 조작이 잠깁니다. 끄면 직접 조작할 수 있습니다.")

    st.write("#### 📈 환경 추이")
    hours = st.select_slider("기간 ", [1, 6, 12, 24, 72, 168], value=6, format_func=lambda h: f"{h}시간",
                             key=f"hrs_{fid}")
    rows = m.storage.readings(fid, hours=hours)
    if rows:
        df = pd.DataFrame(rows)
        df["ts"] = pd.to_datetime(df["ts"])
        df = df.set_index("ts")
        t1, t2, t3 = st.tabs(["🌡️ 온·습도", "🫧 CO₂ / 조도", "⬇️ 데이터"])
        t1.line_chart(df[["temperature", "humidity"]])
        t2.line_chart(df[["co2", "lux"]])
        t3.download_button("CSV 다운로드", df.to_csv().encode("utf-8-sig"), f"{fid}_readings.csv", "text/csv")
        t3.dataframe(df.tail(200), width="stretch")

    c1, c2 = st.columns(2)
    with c1:
        st.write("#### 🧾 제어 이력")
        log = m.storage.actuator_log(fid, 20)
        if log:
            d = pd.DataFrame(log)[["ts", "device", "state", "source"]]
            d["device"] = d["device"].map(lambda x: DEVICES.get(x, x))
            d["state"] = d["state"].map({1: "ON", 0: "OFF"})
            st.dataframe(d, hide_index=True, width="stretch")
    with c2:
        st.write("#### 🍄 AI 판독 이력")
        dg = m.storage.diagnoses(fid, 20)
        if dg:
            st.dataframe(pd.DataFrame(dg)[["ts", "result"]], hide_index=True, width="stretch")
        else:
            st.caption("사진/영상 분석 시 선택된 농장으로 자동 기록됩니다.")


# ---------------------------------------------------------------- 농장 관리
def _sensor_form(prefix: str, current: dict | None = None):
    current = current or {"driver": "simulator"}
    drivers = ["simulator", "http", "mqtt", "serial"]
    labels = {"simulator": "가상 센서(시뮬레이터)", "http": "HTTP (ESP32 등)", "mqtt": "MQTT", "serial": "USB 시리얼"}
    drv = st.selectbox("센서 연결 방식", drivers, index=drivers.index(current.get("driver", "simulator")),
                       format_func=labels.get, key=f"{prefix}_drv")
    sensor, actuator = {"driver": drv}, {"driver": drv}
    if drv == "simulator":
        sensor["base_temperature"] = st.number_input("평균 기온(℃)", value=float(current.get("base_temperature", 17)),
                                                     key=f"{prefix}_bt")
    elif drv == "http":
        sensor["url"] = st.text_input("센서 URL", current.get("url", "http://192.168.0.50/sensor"), key=f"{prefix}_url")
        actuator["url"] = st.text_input("구동기 URL (선택)", current.get("actuator_url", "http://192.168.0.50/actuator"),
                                        key=f"{prefix}_aurl")
    elif drv == "mqtt":
        sensor["host"] = st.text_input("브로커 주소", current.get("host", "localhost"), key=f"{prefix}_host")
        sensor["port"] = st.number_input("포트", value=int(current.get("port", 1883)), key=f"{prefix}_port")
        sensor["topic"] = st.text_input("센서 토픽", current.get("topic", "farm/container-a/sensor"), key=f"{prefix}_tp")
        actuator["topic"] = st.text_input("구동기 토픽", current.get("actuator_topic", "farm/container-a/actuator"),
                                          key=f"{prefix}_atp")
    elif drv == "serial":
        sensor["port"] = st.text_input("포트", current.get("port", "COM3"), key=f"{prefix}_sp")
        sensor["baudrate"] = st.number_input("보드레이트", value=int(current.get("baudrate", 9600)), key=f"{prefix}_br")
    return sensor, actuator


def render_farm_admin(m: FarmManager):
    st.write("### ⚙️ 농장(컨테이너·비닐하우스) 관리")

    with st.expander("➕ 새 농장 추가", expanded=not m.farms):
        name = st.text_input("농장 이름", placeholder="예) 컨테이너 C동, 비닐하우스 3동")
        c1, c2, c3 = st.columns(3)
        ftype = c1.selectbox("유형", list(FARM_TYPES), format_func=FARM_TYPES.get)
        stage = c2.selectbox("재배 단계", list(STAGE_PROFILES), format_func=lambda s: STAGE_PROFILES[s]["label"])
        loc = c3.text_input("위치/메모")
        sensor, actuator = _sensor_form("new")
        if st.button("농장 추가", type="primary", disabled=not name):
            f = m.add_farm(name, ftype, stage, sensor, actuator, loc)
            st.success(f"'{f['name']}' 추가 완료 (ID: {f['id']})")
            st.rerun()

    for farm in list(m.farms):
        with st.expander(f"{FARM_TYPES.get(farm['type'], '🏭').split()[0]} {farm['name']}  ·  {farm['id']}"):
            c1, c2, c3 = st.columns(3)
            name = c1.text_input("이름", farm["name"], key=f"n_{farm['id']}")
            ftype = c2.selectbox("유형", list(FARM_TYPES), index=list(FARM_TYPES).index(farm.get("type", "container")),
                                 format_func=FARM_TYPES.get, key=f"t_{farm['id']}")
            stage = c3.selectbox("재배 단계", list(STAGE_PROFILES),
                                 index=list(STAGE_PROFILES).index(farm.get("stage", "growth")),
                                 format_func=lambda s: STAGE_PROFILES[s]["label"], key=f"s_{farm['id']}")
            loc = st.text_input("위치/메모", farm.get("location", ""), key=f"l_{farm['id']}")
            cur = dict(farm.get("sensor", {}))
            cur["actuator_url"] = farm.get("actuator", {}).get("url", "")
            cur["actuator_topic"] = farm.get("actuator", {}).get("topic", "")
            sensor, actuator = _sensor_form(f"e_{farm['id']}", cur)

            st.markdown("**농장별 기준값 조정** (비워두면 재배 단계 기본값 사용)")
            base = STAGE_PROFILES[stage]
            th = dict(farm.get("thresholds", {}))
            tcols = st.columns(2)
            for i, k in enumerate(("temperature", "humidity")):
                lo, hi = th.get(k, base[k])
                v = tcols[i].slider(f"{METRICS[k][0]} 범위({METRICS[k][1]})", 0.0, 100.0 if k == "humidity" else 40.0,
                                    (float(lo or 0), float(hi or 40)), 0.5, key=f"th_{k}_{farm['id']}")
                if v != tuple(base[k]):
                    th[k] = list(v)
                else:
                    th.pop(k, None)

            b1, b2, b3 = st.columns(3)
            if b1.button("💾 저장", key=f"save_{farm['id']}", type="primary"):
                m.update_farm(farm["id"], name=name, type=ftype, stage=stage, location=loc,
                              sensor=sensor, actuator=actuator, thresholds=th)
                st.success("저장되었습니다.")
                st.rerun()
            if b2.button("↺ 기준값 초기화", key=f"reset_{farm['id']}"):
                m.update_farm(farm["id"], thresholds={})
                st.rerun()
            if b3.button("🗑️ 삭제", key=f"del_{farm['id']}"):
                m.remove_farm(farm["id"])
                st.rerun()

    with st.expander("🔔 알림 / 수집 설정"):
        url = st.text_input("위험 경보 Webhook URL (Slack·Discord·카카오워크 등)",
                            m.config.get("notify", {}).get("webhook_url", ""))
        interval = st.number_input("측정 주기(초)", 2, 3600, int(m.config.get("poll_interval_sec", 10)))
        if st.button("설정 저장"):
            m.config["notify"] = {"webhook_url": url}
            m.config["poll_interval_sec"] = int(interval)
            m.save()
            st.success("저장되었습니다. (측정 주기는 다음 주기부터 적용)")
        st.download_button("farms.json 백업", json.dumps(m.config, ensure_ascii=False, indent=2).encode("utf-8"),
                           "farms.json", "application/json")
