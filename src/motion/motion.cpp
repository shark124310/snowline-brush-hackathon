#include "motion.h"

#include <Arduino.h>
#include <algorithm>
#include <atomic>
#include <cmath>
#include <esp_timer.h>

#include "attitude_kf.h"
#include "mpu6050.h"
#include "oled.h"

#ifndef SNOWLINE_MPU_SDA
#define SNOWLINE_MPU_SDA 17
#endif

namespace motion {
namespace {

constexpr int kMpuSda = SNOWLINE_MPU_SDA;
constexpr int kMpuScl = 18;
constexpr int kOledSda = 9;
constexpr int kOledScl = 8;
constexpr int kSettleSamples = 20;
constexpr int kBiasSamples = 200;
constexpr int64_t kSampleTimeoutUs = 100000;
constexpr int64_t kTraceQuietUs = 400000;
constexpr float kTraceQuietGyroDps = 2.0f;
constexpr float kTraceQuietAccelMps2 = 0.25f;

portMUX_TYPE snapshotLock = portMUX_INITIALIZER_UNLOCKED;
Snapshot latest;
TraceSample traceBuffer[kTraceSampleCount];
size_t traceCount = 0;
bool traceActive = false;
bool traceQuiet = false;
std::atomic<bool> captureEnabled{true};
std::atomic<bool> screenOnline{false};

void publish(const Snapshot &reading) {
    portENTER_CRITICAL(&snapshotLock);
    latest = reading;
    if (reading.state != State::Ready) {
        traceQuiet = false;
    }
    portEXIT_CRITICAL(&snapshotLock);
}

esp_err_t createBus(i2c_port_num_t port, int sda, int scl, i2c_master_bus_handle_t *bus) {
    i2c_master_bus_config_t config = {};
    config.i2c_port = port;
    config.sda_io_num = static_cast<gpio_num_t>(sda);
    config.scl_io_num = static_cast<gpio_num_t>(scl);
    config.clk_source = I2C_CLK_SRC_DEFAULT;
    config.glitch_ignore_cnt = 7;
    config.flags.enable_internal_pullup = true;
    return i2c_new_master_bus(&config, bus);
}

esp_err_t readFreshSample(mpu6050_t &sensor, mpu6050_sample_t &sample) {
    const int64_t deadline = esp_timer_get_time() + kSampleTimeoutUs;
    do {
        if (!captureEnabled.load()) {
            return ESP_ERR_INVALID_STATE;
        }
        const esp_err_t result = mpu6050_read(&sensor, &sample);
        if (result != ESP_ERR_NOT_FINISHED) {
            return result;
        }
        vTaskDelay(pdMS_TO_TICKS(3));
    } while (esp_timer_get_time() < deadline);
    return ESP_ERR_TIMEOUT;
}

float norm(float x, float y, float z) {
    return std::sqrt(x * x + y * y + z * z);
}

void appendTrace(const mpu6050_sample_t &sample, const kf_t &filter,
                 float gyroNorm, bool quiet) {
    portENTER_CRITICAL(&snapshotLock);
    traceQuiet = quiet;
    if (traceActive && traceCount < kTraceSampleCount) {
        traceBuffer[traceCount++] = {
            sample.timestamp_us,
            {filter.q[0], filter.q[1], filter.q[2], filter.q[3]},
            {filter.earth_accel_mps2[0], filter.earth_accel_mps2[1], filter.earth_accel_mps2[2]},
            gyroNorm,
            filter.rest,
            sample.clipped,
            {sample.gyro_raw[0], sample.gyro_raw[1], sample.gyro_raw[2]},
        };
    }
    portEXIT_CRITICAL(&snapshotLock);
}

esp_err_t calibrate(mpu6050_t &sensor, kf_t &filter, bool &accepted) {
    static float gyroReadings[kBiasSamples][3];
    float gyroSum[3] = {};
    float accelSum[3] = {};
    mpu6050_sample_t sample = {};
    Snapshot progress{State::Calibrating};
    publish(progress);

    for (int i = -kSettleSamples; i < kBiasSamples; ++i) {
        const esp_err_t result = readFreshSample(sensor, sample);
        if (result != ESP_OK) {
            return result;
        }
        if (i < 0) {
            continue;
        }
        const float gravity = norm(sample.ax, sample.ay, sample.az);
        if (sample.clipped || std::fabs(gravity - 1.0f) > 0.2f ||
            norm(sample.gx, sample.gy, sample.gz) > 20.0f) {
            accepted = false;
            return ESP_OK;
        }
        gyroReadings[i][0] = sample.gx;
        gyroReadings[i][1] = sample.gy;
        gyroReadings[i][2] = sample.gz;
        for (int axis = 0; axis < 3; ++axis) {
            gyroSum[axis] += gyroReadings[i][axis];
        }
        accelSum[0] += sample.ax;
        accelSum[1] += sample.ay;
        accelSum[2] += sample.az;
        progress.calibrationPercent = 100 * (i + 1) / kBiasSamples;
        publish(progress);
    }
    for (int axis = 0; axis < 3; ++axis) {
        gyroSum[axis] /= kBiasSamples;
        accelSum[axis] /= kBiasSamples;
    }
    const float gravity = norm(accelSum[0], accelSum[1], accelSum[2]);
    accepted = std::fabs(gravity - 1.0f) <= 0.2f;
    for (const auto &gyro : gyroReadings) {
        if (norm(gyro[0] - gyroSum[0], gyro[1] - gyroSum[1], gyro[2] - gyroSum[2]) > 3.0f) {
            accepted = false;
            break;
        }
    }
    if (accepted) {
        kf_init(&filter, nullptr);
        kf_set_gyro_bias_dps(&filter, gyroSum[0], gyroSum[1], gyroSum[2]);
        kf_set_accel_scale(&filter, 1.0f / gravity);
        kf_align_tilt(&filter, accelSum[0], accelSum[1], accelSum[2]);
    }
    return ESP_OK;
}

esp_err_t trackMotion(mpu6050_t &sensor) {
    kf_t filter = {};
    bool calibrated = false;
    while (!calibrated) {
        const esp_err_t result = calibrate(sensor, filter, calibrated);
        if (result != ESP_OK) {
            return result;
        }
    }
    Serial.println("IMU_CALIBRATED");

    int64_t lastUs = 0;
    int64_t quietUs = 0;
    for (;;) {
        mpu6050_sample_t sample;
        const esp_err_t result = readFreshSample(sensor, sample);
        if (result != ESP_OK) {
            return result;
        }
        const int64_t interval = lastUs ? sample.timestamp_us - lastUs : 10000;
        lastUs = sample.timestamp_us;
        if (interval <= 0 || interval > kSampleTimeoutUs) {
            return ESP_ERR_TIMEOUT;
        }
        if (!kf_update(&filter, sample.ax, sample.ay, sample.az,
                       sample.gx, sample.gy, sample.gz, interval * 1e-6f) ||
            !std::isfinite(filter.roll_deg) || !std::isfinite(filter.pitch_deg) ||
            !std::isfinite(filter.yaw_deg)) {
            return ESP_ERR_INVALID_STATE;
        }
        publish({
            State::Ready, 100, sample.timestamp_us,
            {sample.ax, sample.ay, sample.az}, {sample.gx, sample.gy, sample.gz},
            filter.roll_deg, filter.pitch_deg, filter.yaw_deg, sample.temp_c,
            filter.rest, filter.accel_used, sample.clipped,
        });
        const float gyroNorm = norm(filter.res_dps[0], filter.res_dps[1], filter.res_dps[2]);
        const float linearNorm = norm(filter.earth_accel_mps2[0], filter.earth_accel_mps2[1],
                                      filter.earth_accel_mps2[2]);
        // The handheld trace gate tolerates more motion than the gyro bias update.
        quietUs = !sample.clipped && gyroNorm < kTraceQuietGyroDps &&
                  linearNorm < kTraceQuietAccelMps2
            ? std::min(kTraceQuietUs, quietUs + interval) : 0;
        appendTrace(sample, filter, gyroNorm, quietUs >= kTraceQuietUs);
    }
}

void imuTask(void *) {
    i2c_master_bus_handle_t bus = nullptr;
    for (;;) {
        if (!captureEnabled.load()) {
            publish({State::Paused});
            while (!captureEnabled.load()) {
                vTaskDelay(pdMS_TO_TICKS(25));
            }
        }
        publish({State::Starting});
        mpu6050_t sensor = {};
        esp_err_t result = bus ? ESP_OK : createBus(I2C_NUM_0, kMpuSda, kMpuScl, &bus);
        if (result == ESP_OK) {
            result = mpu6050_init(&sensor, bus, 0);
        }
        if (result == ESP_OK) {
            Serial.printf("IMU_FOUND addr=0x%02X sda=%d scl=%d\n", sensor.addr, kMpuSda, kMpuScl);
            result = trackMotion(sensor);
            if (!captureEnabled.load()) {
                mpu6050_write_reg(&sensor, MPU6050_REG_PWR_MGMT_1, MPU6050_SLEEP);
            }
            i2c_master_bus_rm_device(sensor.dev);
        }
        if (captureEnabled.load()) {
            publish({State::Unavailable});
            Serial.printf("IMU_UNAVAILABLE %s\n", esp_err_to_name(result));
            vTaskDelay(pdMS_TO_TICKS(2000));
        }
    }
}

void draw(oled_t &display, const Snapshot &reading) {
    char line[24];
    oled_clear(&display);
    oled_show_string(&display, 1, 1, "SNOWLINE MPU6050");
    switch (reading.state) {
        case State::Starting:
            oled_show_string(&display, 2, 1, "Finding IMU...");
            break;
        case State::Calibrating:
            oled_show_string(&display, 2, 1, "Keep still");
            snprintf(line, sizeof(line), "Calibrating %u%%", reading.calibrationPercent);
            oled_show_string(&display, 3, 1, line);
            break;
        case State::Ready:
            snprintf(line, sizeof(line), "R%+6.1f P%+6.1f", reading.rollDeg, reading.pitchDeg);
            oled_show_string(&display, 2, 1, line);
            snprintf(line, sizeof(line), "Y%+6.1f rel", reading.yawDeg);
            oled_show_string(&display, 3, 1, line);
            snprintf(line, sizeof(line), "T%4.1fC %s", reading.temperatureC,
                     reading.clipped ? "CLIP" : reading.rest ? "REST" : "");
            oled_show_string(&display, 4, 1, line);
            break;
        case State::Unavailable:
            oled_show_string(&display, 2, 1, "IMU unavailable");
            snprintf(line, sizeof(line), "SDA%d SCL%d", kMpuSda, kMpuScl);
            oled_show_string(&display, 3, 1, line);
            break;
        case State::Paused:
            oled_show_string(&display, 2, 1, "Capture paused");
            break;
    }
}

void oledTask(void *) {
    i2c_master_bus_handle_t bus = nullptr;
    const esp_err_t result = createBus(I2C_NUM_1, kOledSda, kOledScl, &bus);
    if (result != ESP_OK) {
        Serial.printf("OLED_BUS_ERROR %s\n", esp_err_to_name(result));
        vTaskDelete(nullptr);
        return;
    }
    static oled_t display;
    for (;;) {
        if (oled_init(&display, bus, 0) == ESP_OK) {
            screenOnline.store(true);
            Serial.printf("OLED_FOUND addr=0x%02X sda=%d scl=%d\n", display.addr, kOledSda, kOledScl);
            do {
                vTaskDelay(pdMS_TO_TICKS(200));
                draw(display, snapshot());
            } while (oled_flush(&display) == ESP_OK);
            screenOnline.store(false);
            i2c_master_bus_rm_device(display.dev);
        }
        vTaskDelay(pdMS_TO_TICKS(2000));
    }
}

}  // namespace

void begin() {
    if (xTaskCreate(imuTask, "imu", 6144, nullptr, 2, nullptr) != pdPASS) {
        publish({State::Unavailable});
    }
    xTaskCreate(oledTask, "oled", 4096, nullptr, 1, nullptr);
}

void setCaptureEnabled(bool enabled) {
    captureEnabled.store(enabled);
}

Snapshot snapshot() {
    portENTER_CRITICAL(&snapshotLock);
    const Snapshot reading = latest;
    portEXIT_CRITICAL(&snapshotLock);
    return reading;
}

bool startTrace(bool requireRest) {
    portENTER_CRITICAL(&snapshotLock);
    const bool ready = !traceActive && latest.state == State::Ready && (!requireRest || traceQuiet);
    if (ready) {
        traceCount = 0;
        traceActive = true;
    }
    portEXIT_CRITICAL(&snapshotLock);
    return ready;
}

const TraceSample *completedTrace() {
    portENTER_CRITICAL(&snapshotLock);
    // Only the serial loop can clear or restart a trace once the buffer is full.
    const TraceSample *complete = traceActive && traceCount == kTraceSampleCount
        ? traceBuffer : nullptr;
    portEXIT_CRITICAL(&snapshotLock);
    return complete;
}

void clearTrace() {
    portENTER_CRITICAL(&snapshotLock);
    traceActive = false;
    traceCount = 0;
    portEXIT_CRITICAL(&snapshotLock);
}

bool oledOnline() {
    return screenOnline.load();
}

const char *stateName(State state) {
    switch (state) {
        case State::Starting: return "starting";
        case State::Calibrating: return "calibrating";
        case State::Ready: return "ready";
        case State::Unavailable: return "unavailable";
        case State::Paused: return "paused";
    }
    return "unavailable";
}

}  // namespace motion
