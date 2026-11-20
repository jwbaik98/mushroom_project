/*
 * 표고버섯 스마트팜 센서/구동기 노드 (ESP32)
 * - 컨테이너·비닐하우스 1동당 1대 설치 → 앱의 "HTTP" 센서 드라이버로 연결
 *
 * 센서: SHT31(온습도, I2C) / MH-Z19B(CO2, UART2) / BH1750(조도, I2C) / 정전용량 수분센서(배지수분, ADC)
 * 구동기: 릴레이 5채널 (히터, 냉방기, 가습기, 환기팬, LED)
 *
 * GET  http://<노드IP>/sensor    -> {"temperature":17.2,"humidity":88.1,"co2":820,"lux":150,"substrate_moisture":61}
 * POST http://<노드IP>/actuator  <- {"heater":false,"cooler":true,"humidifier":false,"fan":true,"led":false}
 *
 * 라이브러리: Adafruit SHT31, BH1750, MHZ19, ArduinoJson (v6)
 */
#include <WiFi.h>
#include <WebServer.h>
#include <Wire.h>
#include <ArduinoJson.h>
#include <Adafruit_SHT31.h>
#include <BH1750.h>
#include <MHZ19.h>

const char* WIFI_SSID = "YOUR_WIFI";
const char* WIFI_PASS = "YOUR_PASSWORD";

const int PIN_MOISTURE = 34;      // ADC
const int MOIST_DRY = 3200, MOIST_WET = 1300;  // 현장 보정값
// 릴레이 핀 (LOW 트리거 모듈이면 RELAY_ON 을 LOW 로)
const char* DEV_NAMES[] = {"heater", "cooler", "humidifier", "fan", "led"};
const int DEV_PINS[]    = {25, 26, 27, 32, 33};
const int RELAY_ON = HIGH;

Adafruit_SHT31 sht31;
BH1750 lightMeter;
MHZ19 mhz19;
WebServer server(80);
unsigned long lastCmd = 0;

void handleSensor() {
  StaticJsonDocument<256> doc;
  float t = sht31.readTemperature(), h = sht31.readHumidity();
  if (!isnan(t)) doc["temperature"] = t;
  if (!isnan(h)) doc["humidity"] = h;
  doc["co2"] = mhz19.getCO2();
  doc["lux"] = lightMeter.readLightLevel();
  int raw = analogRead(PIN_MOISTURE);
  doc["substrate_moisture"] = constrain(map(raw, MOIST_DRY, MOIST_WET, 0, 100), 0, 100);
  String out; serializeJson(doc, out);
  server.send(200, "application/json", out);
}

void handleActuator() {
  StaticJsonDocument<256> doc;
  if (deserializeJson(doc, server.arg("plain"))) { server.send(400, "text/plain", "bad json"); return; }
  for (int i = 0; i < 5; i++) {
    if (doc.containsKey(DEV_NAMES[i]))
      digitalWrite(DEV_PINS[i], doc[DEV_NAMES[i]].as<bool>() ? RELAY_ON : !RELAY_ON);
  }
  lastCmd = millis();
  server.send(200, "application/json", "{\"ok\":true}");
}

void setup() {
  Serial.begin(115200);
  for (int i = 0; i < 5; i++) { pinMode(DEV_PINS[i], OUTPUT); digitalWrite(DEV_PINS[i], !RELAY_ON); }
  Wire.begin();
  if (!sht31.begin(0x44)) Serial.println("SHT31 not found - 배선 확인");
  lightMeter.begin();
  Serial2.begin(9600);
  mhz19.begin(Serial2);
  mhz19.autoCalibration(false);

  WiFi.setAutoReconnect(true);   // 공유기 재시작·순간 끊김 후 자동 재접속
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) { delay(500); Serial.print("."); }
  Serial.printf("\nIP: %s\n", WiFi.localIP().toString().c_str());

  server.on("/sensor", HTTP_GET, handleSensor);
  server.on("/actuator", HTTP_POST, handleActuator);
  server.begin();
}

void loop() {
  server.handleClient();
  // 안전장치: PC/서버와 10분간 통신이 끊기면 히터·가습기 OFF, 환기팬 ON
  if (WiFi.status() != WL_CONNECTED) {
    static unsigned long lastTry = 0;
    if (millis() - lastTry > 30000UL) { lastTry = millis(); WiFi.reconnect(); }
  }
  if (lastCmd && millis() - lastCmd > 600000UL) {
    digitalWrite(DEV_PINS[0], !RELAY_ON);
    digitalWrite(DEV_PINS[2], !RELAY_ON);
    digitalWrite(DEV_PINS[3], RELAY_ON);
  }
}
