# 🍄 표고버섯 AI 스마트팜

YOLO 병해 판독 + LLM 처방에 **여러 동의 스마트팜 컨테이너·비닐하우스 환경 모니터링/자동제어**를 더한 Streamlit 앱입니다.

## 실행
```bash
pip install -r requirements.txt
streamlit run app/mushroom_detect_app.py
```
> LLM(`llama-cpp-python`, `.gguf`)·임베딩 모델이 없어도 스마트팜 대시보드는 동작합니다.

## 메뉴
| 메뉴 | 기능 |
|---|---|
| 🏠 통합 대시보드 | 전체 농장 상태(정상/주의/위험) 카드, 최근 경보, 농장 간 온도·습도·CO₂ 비교 그래프 (자동 새로고침) |
| 🌡️ 농장 모니터링·제어 | 선택 농장 실시간 센서값, 기준 대비 경보, **병해 발생 위험도**, 주야 온도차(변온관리), 자동/수동 장치 제어, 추이 그래프·CSV, 제어/판독 이력 |
| 📸 사진 분석 / 📹 실시간 영상 | 기존 AI 판독. 결과가 **선택한 농장 이력에 기록**되고, 병해 탐지 시 해당 농장을 '병해 긴급관리' 모드로 전환 가능. LLM 처방에 현재 환경값 반영 |
| ⚙️ 농장 관리 | 농장 추가/수정/삭제, 유형(컨테이너·비닐하우스·실내), 재배 단계, 센서 연결 방식, 농장별 기준값, Webhook 알림 |

## 재배 단계별 기준 (`app/smartfarm/profiles.py`, 매뉴얼 기반)
| 단계 | 온도 | 습도 | CO₂ | 조도 |
|---|---|---|---|---|
| 배양 | 22~26℃ | 85~95% | ≤3000ppm | ≤50lux(암) |
| 발생·생육 | 8~22℃ (주야차 8℃↑) | 80~95% | ≤1000ppm | 50~500lux |
| 휴양 | 18~25℃ | 60~75% | ≤2000ppm | ≤200lux |
| 병해 긴급관리 | 15~18℃ | 70~80% | ≤800ppm | - |

자동제어는 기준 이탈 시 장치 ON → 기준 중앙값 도달 시 OFF(히스테리시스)로 히터·냉방기·가습기·환기팬·LED를 제어합니다.

## 센서 연결 방식 (`app/farms.json`)
| driver | 설명 | 설정 예 |
|---|---|---|
| `simulator` | 하드웨어 없이 시연용 가상 센서 | `{"driver":"simulator","base_temperature":16}` |
| `http` | ESP32 노드 (`firmware/sensor_node`) | `"sensor":{"driver":"http","url":"http://192.168.0.50/sensor"}, "actuator":{"driver":"http","url":"http://192.168.0.50/actuator"}` |
| `mqtt` | MQTT 브로커 (`pip install paho-mqtt`) | `"sensor":{"driver":"mqtt","host":"localhost","topic":"farm/a/sensor"}, "actuator":{"driver":"mqtt","topic":"farm/a/actuator"}` |
| `serial` | USB 시리얼 JSON (`pip install pyserial`) | `{"driver":"serial","port":"COM3","baudrate":9600}` |

센서 JSON 키: `temperature, humidity, co2, lux, substrate_moisture` (`temp`, `hum` 등 별칭 허용)

## 24시간 수집 서비스
앱을 켜두지 않아도 수집·자동제어를 하려면:
```bash
python app/collector.py        # farms.json 에 "ui_collector": false 로 앱의 중복 수집을 끔
```
데이터는 `app/smartfarm.db`(SQLite)에 저장됩니다.

## 안전 기능
- 센서 값 누락·고장·통신 끊김 → 히터·냉방·가습 OFF, 환기 ON (안전모드)
- 히터/냉방기 인터록, 자동제어 최소 전환 간격(`min_switch_sec`, 기본 60초)
- 센서 데이터 지연 감지, 비정상 값(NaN·범위 밖) 자동 제외
- 접속 비밀번호: 환경변수 `SMARTFARM_PASSWORD`
- 데이터 보관 기간: `retention_days`(기본 90일)

QA 점검 내역은 [`docs/QA_REPORT.md`](docs/QA_REPORT.md), 기능별 화면은 [`docs/SCREENSHOTS.md`](docs/SCREENSHOTS.md) 참고.

## 테스트
```bash
pytest tests
```
