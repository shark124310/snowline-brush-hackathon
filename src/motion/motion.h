#pragma once

#include <stddef.h>
#include <stdint.h>

namespace motion {

enum class State { Starting, Calibrating, Ready, Unavailable, Paused };

struct Snapshot {
    State state = State::Starting;
    unsigned calibrationPercent = 0;
    int64_t timestampUs = 0;
    float accelG[3] = {};
    float gyroDps[3] = {};
    float rollDeg = 0;
    float pitchDeg = 0;
    float yawDeg = 0;
    float temperatureC = 0;
    bool rest = false;
    bool accelUsed = false;
    bool clipped = false;
};

constexpr size_t kTraceSampleCount = 900;

struct TraceSample {
    int64_t timestampUs;
    float quaternion[4];
    float earthAccelMps2[3];
    float gyroNormDps;
    bool rest;
    bool clipped;
    int16_t gyroRaw[3];
};

void begin();
void setCaptureEnabled(bool enabled);
Snapshot snapshot();
bool startTrace(bool requireRest = false);
const TraceSample *completedTrace();
void clearTrace();
bool oledOnline();
const char *stateName(State state);

}  // namespace motion
