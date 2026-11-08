#include <Arduino.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <esp_random.h>

#include "motion/motion.h"

namespace {

constexpr uint32_t kPacketIntervalMs = 1000;
constexpr uint32_t kMotionPacketIntervalMs = 200;
constexpr uint32_t kStatusIntervalMs = 3000;

String receiverUrl;
uint32_t bootId;
uint32_t sequence = 0;
uint32_t probeAck = 0;
uint32_t captureCommandId = 0;
bool captureRequested = true;
uint32_t lastPacketAt = 0;
uint32_t lastStatusAt = 0;
bool wasConnected = false;
enum class TraceRun { Idle, Armed, Recording };
TraceRun traceRun = TraceRun::Idle;
uint32_t traceStartedAt = 0;

bool writeTraceLine(const char *line) {
    const auto *bytes = reinterpret_cast<const uint8_t *>(line);
    const size_t size = strlen(line);
    size_t written = 0;
    const uint32_t started = millis();
    while (written < size && millis() - started < 500) {
        written += Serial.write(bytes + written, size - written);
        if (written < size) {
            delay(1);
        }
    }
    return written == size;
}

void beginTrace() {
    traceRun = TraceRun::Recording;
    traceStartedAt = millis();
    char header[32];
    snprintf(header, sizeof(header), "TRACE_BEGIN,%u\n", static_cast<unsigned>(motion::kTraceSampleCount));
    writeTraceLine(header);
}

void serviceTrace() {
    if (traceRun == TraceRun::Armed) {
        if (motion::startTrace(true)) {
            beginTrace();
        } else if (millis() - traceStartedAt >= 20000) {
            traceRun = TraceRun::Idle;
            writeTraceLine("TRACE_ERROR no stationary start\n");
        }
        return;
    }
    if (traceRun != TraceRun::Recording) {
        return;
    }
    const motion::TraceSample *trace = motion::completedTrace();
    if (!trace && millis() - traceStartedAt < 13000) {
        return;
    }
    traceRun = TraceRun::Idle;
    if (!trace) {
        motion::clearTrace();
        writeTraceLine("TRACE_ERROR sampling timeout\n");
        return;
    }
    bool sent = true;
    for (size_t i = 0; i < motion::kTraceSampleCount && sent; ++i) {
        const motion::TraceSample &s = trace[i];
        char line[200];
        snprintf(line, sizeof(line), "TRACE,%u,%llu,%.7f,%.7f,%.7f,%.7f,"
                 "%.6f,%.6f,%.6f,%.4f,%u,%u,%d,%d,%d\n",
                 static_cast<unsigned>(i), static_cast<unsigned long long>(s.timestampUs),
                 s.quaternion[0], s.quaternion[1], s.quaternion[2], s.quaternion[3],
                 s.earthAccelMps2[0], s.earthAccelMps2[1], s.earthAccelMps2[2],
                 s.gyroNormDps, static_cast<unsigned>(s.rest), static_cast<unsigned>(s.clipped),
                 s.gyroRaw[0], s.gyroRaw[1], s.gyroRaw[2]);
        sent = writeTraceLine(line);
    }
    motion::clearTrace();
    writeTraceLine(sent ? "TRACE_DONE\n" : "TRACE_ERROR USB output stalled\n");
}

void configureFromSerial() {
    if (!Serial.available()) {
        return;
    }

    String command = Serial.readStringUntil('\n');
    if (command.endsWith("\r")) {
        command.remove(command.length() - 1);
    }
    if (command == "TRACE" || command == "TRACE_ARM") {
        if (traceRun != TraceRun::Idle) {
            writeTraceLine("TRACE_ERROR trace busy\n");
        } else if (command == "TRACE_ARM") {
            traceStartedAt = millis();
            traceRun = TraceRun::Armed;
            writeTraceLine("TRACE_ARMED\n");
        } else if (motion::startTrace()) {
            beginTrace();
        } else {
            writeTraceLine("TRACE_ERROR IMU not ready or trace busy\n");
        }
        return;
    }
    const int ssidEnd = command.indexOf('\t', 5);
    const int passwordEnd = command.indexOf('\t', ssidEnd + 1);
    if (!command.startsWith("WIFI\t") || ssidEnd < 0 || passwordEnd < 0) {
        Serial.println("CONFIG_ERROR expected WIFI command");
        return;
    }

    const String ssid = command.substring(5, ssidEnd);
    const String password = command.substring(ssidEnd + 1, passwordEnd);
    const String url = command.substring(passwordEnd + 1);
    if (ssid.isEmpty() || ssid.length() > 32 || password.length() > 64 ||
        !url.startsWith("http://") || !url.endsWith("/api/wifi-test")) {
        Serial.println("CONFIG_ERROR invalid SSID, password length, or receiver URL");
        return;
    }

    receiverUrl = url;
    probeAck = 0;
    captureCommandId = 0;
    wasConnected = false;
    WiFi.disconnect();
    WiFi.begin(ssid.c_str(), password.c_str());
    Serial.println("CONFIG_OK connecting to Wi-Fi");
}

void applyCaptureCommand(const String &header) {
    const int separator = header.indexOf(':');
    if (separator < 1 || separator > 9) {
        return;
    }
    const String id = header.substring(0, separator);
    const unsigned long commandId = strtoul(id.c_str(), nullptr, 10);
    const String action = header.substring(separator + 1);
    if (!commandId || String(commandId) != id || (action != "start" && action != "stop")) {
        return;
    }
    captureCommandId = commandId;
    captureRequested = action == "start";
    motion::setCaptureEnabled(captureRequested);
}

void sendTestPacket(const motion::Snapshot &reading) {
    ++sequence;
    char imu[300];
    if (reading.state == motion::State::Ready) {
        snprintf(imu, sizeof(imu),
                 "{\"state\":\"ready\",\"t_us\":%llu,"
                 "\"a_g\":[%.4f,%.4f,%.4f],\"w_dps\":[%.2f,%.2f,%.2f],"
                 "\"rpy_deg\":[%.2f,%.2f,%.2f],\"temp_c\":%.2f,"
                 "\"rest\":%s,\"accel_used\":%s,\"clipped\":%s}",
                 static_cast<unsigned long long>(reading.timestampUs),
                 reading.accelG[0], reading.accelG[1], reading.accelG[2],
                 reading.gyroDps[0], reading.gyroDps[1], reading.gyroDps[2],
                 reading.rollDeg, reading.pitchDeg, reading.yawDeg, reading.temperatureC,
                 reading.rest ? "true" : "false", reading.accelUsed ? "true" : "false",
                 reading.clipped ? "true" : "false");
    } else {
        snprintf(imu, sizeof(imu), "{\"state\":\"%s\",\"cal_pct\":%u}",
                 motion::stateName(reading.state), reading.calibrationPercent);
    }
    const bool captureActive = reading.state != motion::State::Paused;
    char packet[512];
    snprintf(packet, sizeof(packet),
             "{\"source\":\"wifi-test\",\"boot_id\":%lu,\"seq\":%lu,"
             "\"uptime_ms\":%lu,\"rssi_dbm\":%ld,\"probe_ack\":%lu,"
             "\"capture_ack\":%lu,\"oled\":%s,\"imu\":%s}",
             static_cast<unsigned long>(bootId), static_cast<unsigned long>(sequence),
             millis(), static_cast<long>(WiFi.RSSI()), static_cast<unsigned long>(probeAck),
             static_cast<unsigned long>(captureActive == captureRequested ? captureCommandId : 0),
             motion::oledOnline() ? "true" : "false", imu);

    WiFiClient connection;
    HTTPClient http;
    if (!http.begin(connection, receiverUrl)) {
        Serial.println("HTTP_ERROR invalid receiver URL");
        return;
    }

    http.setConnectTimeout(1500);
    http.setTimeout(1500);
    http.addHeader("Content-Type", "application/json");
    const char *responseHeaders[] = {"X-Link-Probe", "X-Capture-Control"};
    http.collectHeaders(responseHeaders, 2);
    const int status = http.POST(String(packet));
    if (status == HTTP_CODE_OK && http.getString() == String(sequence)) {
        probeAck = 0;
        const String probe = http.header(responseHeaders[0]);
        if (probe.length() > 0 && probe.length() <= 9) {
            const unsigned long value = strtoul(probe.c_str(), nullptr, 10);
            if (value > 0 && String(value) == probe) {
                probeAck = value;
                Serial.printf("PROBE_RECEIVED id=%lu\n", value);
            }
        }
        applyCaptureCommand(http.header(responseHeaders[1]));
        Serial.printf("ACK seq=%lu rssi=%ld dBm\n", static_cast<unsigned long>(sequence),
                      static_cast<long>(WiFi.RSSI()));
    } else {
        Serial.printf("POST_ERROR seq=%lu status=%d\n", static_cast<unsigned long>(sequence),
                      status);
    }
    http.end();
}

}  // namespace

void setup() {
    Serial.begin(115200);
#if ARDUINO_USB_CDC_ON_BOOT && ARDUINO_USB_MODE
    // Diagnostic output must not block Wi-Fi while no host reads USB Serial/JTAG.
    Serial.setTxTimeoutMs(0);
#endif
    Serial.setTimeout(500);
    Serial.printf("%s | flash %lu MB | PSRAM %lu MB\n",
                  ESP.getChipModel(),
                  static_cast<unsigned long>(ESP.getFlashChipSize() / (1024 * 1024)),
                  static_cast<unsigned long>(ESP.getPsramSize() / (1024 * 1024)));
    WiFi.persistent(false);
    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(true);
    bootId = esp_random();
    motion::begin();
    Serial.println("WIFI_TEST_READY");
}

void loop() {
    configureFromSerial();
    serviceTrace();
    const uint32_t now = millis();
    const bool connected = !receiverUrl.isEmpty() && WiFi.status() == WL_CONNECTED;

    if (connected && !wasConnected) {
        Serial.printf("WIFI_CONNECTED ip=%s\n", WiFi.localIP().toString().c_str());
    }
    wasConnected = connected;

    const motion::Snapshot reading = motion::snapshot();
    const uint32_t packetInterval = reading.state == motion::State::Ready
        ? kMotionPacketIntervalMs : kPacketIntervalMs;
    if (connected && now - lastPacketAt >= packetInterval) {
        lastPacketAt = now;
        sendTestPacket(reading);
    } else if (!connected && now - lastStatusAt >= kStatusIntervalMs) {
        lastStatusAt = now;
        if (receiverUrl.isEmpty()) {
            Serial.println("WIFI_TEST_READY");
        } else {
            Serial.printf("WIFI_WAIT status=%d\n", WiFi.status());
        }
    }
    delay(10);
}
