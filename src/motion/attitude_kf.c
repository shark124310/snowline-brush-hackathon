#include "attitude_kf.h"

#include <math.h>
#include <string.h>

#define PI_F        3.14159265358979323846f
#define DEG2RAD     (PI_F / 180.0f)
#define RAD2DEG     (180.0f / PI_F)
#define G0          9.80665f

static float clampf(float x, float lo, float hi)
{
    return x < lo ? lo : (x > hi ? hi : x);
}

static float norm3(float x, float y, float z)
{
    return sqrtf(x * x + y * y + z * z);
}

/* out = a ⊗ b（Hamilton，标量在前） */
static void q_mul(const float a[4], const float b[4], float out[4])
{
    float w = a[0] * b[0] - a[1] * b[1] - a[2] * b[2] - a[3] * b[3];
    float x = a[0] * b[1] + a[1] * b[0] + a[2] * b[3] - a[3] * b[2];
    float y = a[0] * b[2] - a[1] * b[3] + a[2] * b[0] + a[3] * b[1];
    float z = a[0] * b[3] + a[1] * b[2] - a[2] * b[1] + a[3] * b[0];
    out[0] = w; out[1] = x; out[2] = y; out[3] = z;
}

static void q_normalize(float q[4])
{
    float n = sqrtf(q[0] * q[0] + q[1] * q[1] + q[2] * q[2] + q[3] * q[3]);
    if (n < 1e-9f) {
        q[0] = 1.0f; q[1] = q[2] = q[3] = 0.0f;
        return;
    }
    float k = 1.0f / n;
    for (int i = 0; i < 4; i++) {
        q[i] *= k;
    }
}

/* 机体系下的“上”方向：R(q)^T · (0,0,1) */
static void up_body(const float q[4], float out[3])
{
    out[0] = 2.0f * (q[1] * q[3] - q[0] * q[2]);
    out[1] = 2.0f * (q[2] * q[3] + q[0] * q[1]);
    out[2] = 1.0f - 2.0f * (q[1] * q[1] + q[2] * q[2]);
}

/* 机体系向量转世界系 */
static void rotate_forward(const float q[4], const float v[3], float out[3])
{
    float u[3] = { q[1], q[2], q[3] };
    float t[3] = {
        2.0f * (u[1] * v[2] - u[2] * v[1]),
        2.0f * (u[2] * v[0] - u[0] * v[2]),
        2.0f * (u[0] * v[1] - u[1] * v[0]),
    };
    out[0] = v[0] + q[0] * t[0] + (u[1] * t[2] - u[2] * t[1]);
    out[1] = v[1] + q[0] * t[1] + (u[2] * t[0] - u[0] * t[2]);
    out[2] = v[2] + q[0] * t[2] + (u[0] * t[1] - u[1] * t[0]);
}

static void quat_from_tilt(float ax, float ay, float az, float q[4])
{
    float roll = atan2f(ay, az);
    float pitch = atan2f(-ax, sqrtf(ay * ay + az * az));
    float cr = cosf(roll * 0.5f), sr = sinf(roll * 0.5f);
    float cp = cosf(pitch * 0.5f), sp = sinf(pitch * 0.5f);
    q[0] = cr * cp;
    q[1] = sr * cp;
    q[2] = cr * sp;
    q[3] = -sr * sp;
    q_normalize(q);
}

/* m = [v×]，即 [v×]x = v × x */
static void skew3(const float v[3], float m[3][3])
{
    m[0][0] = 0.0f;   m[0][1] = -v[2];  m[0][2] = v[1];
    m[1][0] = v[2];   m[1][1] = 0.0f;   m[1][2] = -v[0];
    m[2][0] = -v[1];  m[2][1] = v[0];   m[2][2] = 0.0f;
}

static int mat3_inv(const float m[3][3], float inv[3][3])
{
    float det = m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
              - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
              + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]);
    /* 相对判据：S 在零角速度观测下只有 1e-6 量级，行列式 1e-17，
     * 用绝对阈值会把它误判成奇异而静默跳过更新。 */
    float scl = fabsf(m[0][0]) + fabsf(m[1][1]) + fabsf(m[2][2]);
    if (!(scl > 0.0f) || fabsf(det) < 1e-10f * scl * scl * scl) {
        return 0;
    }
    float id = 1.0f / det;
    inv[0][0] = (m[1][1] * m[2][2] - m[1][2] * m[2][1]) * id;
    inv[0][1] = (m[0][2] * m[2][1] - m[0][1] * m[2][2]) * id;
    inv[0][2] = (m[0][1] * m[1][2] - m[0][2] * m[1][1]) * id;
    inv[1][0] = (m[1][2] * m[2][0] - m[1][0] * m[2][2]) * id;
    inv[1][1] = (m[0][0] * m[2][2] - m[0][2] * m[2][0]) * id;
    inv[1][2] = (m[0][2] * m[1][0] - m[0][0] * m[1][2]) * id;
    inv[2][0] = (m[1][0] * m[2][1] - m[1][1] * m[2][0]) * id;
    inv[2][1] = (m[0][1] * m[2][0] - m[0][0] * m[2][1]) * id;
    inv[2][2] = (m[0][0] * m[1][1] - m[0][1] * m[1][0]) * id;
    return 1;
}

kf_config kf_default_config(void)
{
    kf_config c;
    memset(&c, 0, sizeof(c));
    c.q_angle = 4e-6f;          /* (0.0023 rad/s)^2 */
    c.q_bias = 1e-8f;
    c.r_accel = 1e-3f;          /* 方向噪声约 0.032 */
    c.r_gyro = 4e-6f;           /* 0.002 rad/s ≈ 0.11 dps */
    c.accel_reject_g = 0.15f;
    c.rest_gyro_dps = 0.35f;    /* 手持慢速搬动多在 0.3 dps 以上，门槛太高会把转动吸进零偏 */
    c.rest_accel_g = 0.08f;     /* 容忍加速度零偏导致的模长偏差 */
    c.rest_time_s = 1.0f;
    c.accel_scale_tau_s = 10.0f;
    return c;
}

void kf_init(kf_t *kf, const kf_config *cfg)
{
    kf_config c = cfg ? *cfg : kf_default_config();
    memset(kf, 0, sizeof(*kf));
    kf->cfg = c;
    kf->q[0] = 1.0f;
    kf->accel_scale = 1.0f;

    /* 初始方差：倾角由加速度对齐（约 1°），零偏由上电标定（约 0.3 dps） */
    for (int i = 0; i < 3; i++) {
        kf->P[i][i] = 3e-4f;            /* (0.017 rad)^2 ≈ 1° */
        kf->P[i + 3][i + 3] = 4e-6f;    /* (0.002 rad/s)^2 ≈ 0.11 dps */
    }
}

void kf_set_gyro_bias_dps(kf_t *kf, float bx, float by, float bz)
{
    kf->bias[0] = bx * DEG2RAD;
    kf->bias[1] = by * DEG2RAD;
    kf->bias[2] = bz * DEG2RAD;
    kf->rest_time = 0.0f;
}

void kf_set_accel_scale(kf_t *kf, float scale)
{
    if (scale > 0.5f && scale < 2.0f) {
        kf->accel_scale = scale;
    }
}

void kf_align_tilt(kf_t *kf, float ax, float ay, float az)
{
    float n = norm3(ax, ay, az);
    if (n < 0.2f) {
        return;
    }
    quat_from_tilt(ax / n, ay / n, az / n, kf->q);
    kf->ready = true;
}

/* 误差状态注入：q ← q ⊗ δq，bias ← bias + δb */
static void kf_inject(kf_t *kf, const float dx[6])
{
    float ang = norm3(dx[0], dx[1], dx[2]);
    float dq[4];
    if (ang > 1e-7f) {
        float s = sinf(0.5f * ang) / ang;
        dq[0] = cosf(0.5f * ang);
        dq[1] = dx[0] * s;
        dq[2] = dx[1] * s;
        dq[3] = dx[2] * s;
    } else {
        dq[0] = 1.0f;
        dq[1] = 0.5f * dx[0];
        dq[2] = 0.5f * dx[1];
        dq[3] = 0.5f * dx[2];
    }
    float qn[4];
    q_mul(kf->q, dq, qn);
    q_normalize(qn);
    memcpy(kf->q, qn, sizeof(qn));

    for (int i = 0; i < 3; i++) {
        kf->bias[i] += dx[3 + i];
    }
}

/* 通用 3 维观测更新：z = H x，观测噪声方差 rvar（各轴相同） */
static void kf_apply_update(kf_t *kf, const float H[3][6], const float r[3], float rvar)
{
    float PHt[6][3];
    for (int i = 0; i < 6; i++) {
        for (int c = 0; c < 3; c++) {
            float s = 0.0f;
            for (int j = 0; j < 6; j++) {
                s += kf->P[i][j] * H[c][j];
            }
            PHt[i][c] = s;
        }
    }

    float S[3][3];
    for (int a = 0; a < 3; a++) {
        for (int b = 0; b < 3; b++) {
            float s = 0.0f;
            for (int j = 0; j < 3; j++) {
                s += H[a][j] * PHt[j][b];
            }
            S[a][b] = s + (a == b ? rvar : 0.0f);
        }
    }

    float Sinv[3][3];
    if (!mat3_inv(S, Sinv)) {
        return;
    }

    float K[6][3];
    for (int i = 0; i < 6; i++) {
        for (int c = 0; c < 3; c++) {
            float s = 0.0f;
            for (int j = 0; j < 3; j++) {
                s += PHt[i][j] * Sinv[j][c];
            }
            K[i][c] = s;
        }
    }

    float dx[6];
    for (int i = 0; i < 6; i++) {
        dx[i] = K[i][0] * r[0] + K[i][1] * r[1] + K[i][2] * r[2];
    }

    /* P ← (I - K H) P */
    float HP[3][6];
    for (int a = 0; a < 3; a++) {
        for (int j = 0; j < 6; j++) {
            float s = 0.0f;
            for (int k = 0; k < 6; k++) {
                s += H[a][k] * kf->P[k][j];
            }
            HP[a][j] = s;
        }
    }
    for (int i = 0; i < 6; i++) {
        for (int j = 0; j < 6; j++) {
            float s = 0.0f;
            for (int c = 0; c < 3; c++) {
                s += K[i][c] * HP[c][j];
            }
            kf->P[i][j] -= s;
        }
    }

    /* 数值上强制对称 */
    for (int i = 0; i < 6; i++) {
        for (int j = i + 1; j < 6; j++) {
            float m = 0.5f * (kf->P[i][j] + kf->P[j][i]);
            kf->P[i][j] = kf->P[j][i] = m;
        }
    }

    kf_inject(kf, dx);
}

/* 状态预测：四元数积分 + 误差协方差传播 */
static void kf_predict(kf_t *kf, const float w[3], float dt)
{
    float rate = norm3(w[0], w[1], w[2]);
    float h = 0.5f * rate * dt;
    float k = (rate > 1e-8f) ? sinf(h) / rate : 0.5f * dt;
    float dq[4] = { cosf(h), w[0] * k, w[1] * k, w[2] * k };
    float qn[4];
    q_mul(kf->q, dq, qn);
    q_normalize(qn);
    memcpy(kf->q, qn, sizeof(qn));

    /* δθ̇ = -[ω×]δθ - δb, δḃ = 0 */
    float F[6][6];
    memset(F, 0, sizeof(F));
    for (int i = 0; i < 6; i++) {
        F[i][i] = 1.0f;
    }
    float W[3][3];
    skew3(w, W);
    for (int i = 0; i < 3; i++) {
        for (int j = 0; j < 3; j++) {
            F[i][j] -= W[i][j] * dt;
        }
        F[i][i + 3] = -dt;
    }

    /* P ← F P F^T + Q dt */
    float FP[6][6];
    for (int i = 0; i < 6; i++) {
        for (int j = 0; j < 6; j++) {
            float s = 0.0f;
            for (int k = 0; k < 6; k++) {
                s += F[i][k] * kf->P[k][j];
            }
            FP[i][j] = s;
        }
    }
    float Pn[6][6];
    for (int i = 0; i < 6; i++) {
        for (int j = 0; j < 6; j++) {
            float s = 0.0f;
            for (int k = 0; k < 6; k++) {
                s += FP[i][k] * F[j][k];    /* F^T[k][j] = F[j][k] */
            }
            Pn[i][j] = s;
        }
    }
    for (int i = 0; i < 3; i++) {
        Pn[i][i] += kf->cfg.q_angle * dt;
        Pn[i + 3][i + 3] += kf->cfg.q_bias * dt;
    }
    memcpy(kf->P, Pn, sizeof(Pn));
}

int kf_update(kf_t *kf, float ax, float ay, float az,
              float gx, float gy, float gz, float dt)
{
    if (!kf || !isfinite(dt) || dt <= 0.0f || dt > 0.1f ||
        !isfinite(ax) || !isfinite(ay) || !isfinite(az) ||
        !isfinite(gx) || !isfinite(gy) || !isfinite(gz)) {
        return 0;
    }

    const kf_config *c = &kf->cfg;
    float a[3] = { ax * kf->accel_scale, ay * kf->accel_scale, az * kf->accel_scale };
    float wm[3] = { gx * DEG2RAD, gy * DEG2RAD, gz * DEG2RAD };
    float w[3] = {
        wm[0] - kf->bias[0],
        wm[1] - kf->bias[1],
        wm[2] - kf->bias[2],
    };

    /* ---- 1. 预测 ---- */
    kf_predict(kf, w, dt);

    /* ---- 2. 加速度计观测：重力方向 ----
     * 线性加速度太大时（||a|-1| 超过阈值）直接不用，避免"推板子把姿态掰歪"。
     */
    float an = norm3(a[0], a[1], a[2]);
    kf->accel_used = false;
    if (an > 0.2f && fabsf(an - 1.0f) < c->accel_reject_g) {
        float um[3] = { a[0] / an, a[1] / an, a[2] / an };
        float ue[3];
        up_body(kf->q, ue);

        float r[3] = { um[0] - ue[0], um[1] - ue[1], um[2] - ue[2] };
        float U[3][3];
        skew3(ue, U);

        float H[3][6];
        memset(H, 0, sizeof(H));
        for (int i = 0; i < 3; i++) {
            for (int j = 0; j < 3; j++) {
                H[i][j] = U[i][j];
            }
        }
        kf_apply_update(kf, H, r, c->r_accel);
        kf->accel_used = true;
    }

    /* ---- 3. 静止判据（平滑 0.25 s，抗单点尖峰）---- */
    float res[3] = {
        (wm[0] - kf->bias[0]) * RAD2DEG,
        (wm[1] - kf->bias[1]) * RAD2DEG,
        (wm[2] - kf->bias[2]) * RAD2DEG,
    };
    float ar = clampf(1.0f - expf(-dt / 0.25f), 0.0f, 1.0f);
    if (!kf->rest_lp_ready) {
        for (int i = 0; i < 3; i++) {
            kf->rest_gyro_lp[i] = res[i];
        }
        kf->rest_accel_lp = an;
        kf->rest_lp_ready = true;
    }
    for (int i = 0; i < 3; i++) {
        kf->rest_gyro_lp[i] += ar * (res[i] - kf->rest_gyro_lp[i]);
    }
    kf->rest_accel_lp += ar * (an - kf->rest_accel_lp);

    bool still_now =
        norm3(kf->rest_gyro_lp[0], kf->rest_gyro_lp[1], kf->rest_gyro_lp[2]) <
            c->rest_gyro_dps &&
        fabsf(kf->rest_accel_lp - 1.0f) < c->rest_accel_g;

    if (still_now) {
        kf->rest_time += dt;
    } else {
        kf->rest_time = 0.0f;
    }
    kf->rest = kf->rest_time >= c->rest_time_s;

    /* ---- 4. 静止时用"零角速度"做观测：z 轴零偏由此可观测 ---- */
    if (kf->rest) {
        /* 静止时 |a| 应为 1 g：顺便慢速修正加速度比例，
         * 消除换姿态后零偏导致的模长偏差（10 s 量级，不会吃掉真实运动）。 */
        if (c->accel_scale_tau_s > 0.0f && an > 0.2f) {
            float k = clampf(1.0f - expf(-dt / c->accel_scale_tau_s), 0.0f, 1.0f);
            kf->accel_scale *= 1.0f + k * (1.0f / an - 1.0f);
            kf->accel_scale = clampf(kf->accel_scale, 0.5f, 2.0f);
        }

        float r[3] = {
            wm[0] - kf->bias[0],
            wm[1] - kf->bias[1],
            wm[2] - kf->bias[2],
        };
        float H[3][6];
        memset(H, 0, sizeof(H));
        for (int i = 0; i < 3; i++) {
            H[i][3 + i] = 1.0f;     /* 只观测零偏 */
        }
        kf_apply_update(kf, H, r, c->r_gyro);
    }

    /* ---- 5. 输出 ---- */
    for (int i = 0; i < 3; i++) {
        kf->res_dps[i] = (wm[i] - kf->bias[i]) * RAD2DEG;
    }
    float res_rad[3] = {
        wm[0] - kf->bias[0],
        wm[1] - kf->bias[1],
        wm[2] - kf->bias[2],
    };
    float res_world[3];
    rotate_forward(kf->q, res_rad, res_world);
    kf->yaw_rate_world_dps = res_world[2] * RAD2DEG;

    const float *q = kf->q;
    kf->roll_deg = atan2f(2.0f * (q[0] * q[1] + q[2] * q[3]),
                          1.0f - 2.0f * (q[1] * q[1] + q[2] * q[2])) * RAD2DEG;
    kf->pitch_deg = asinf(clampf(2.0f * (q[0] * q[2] - q[3] * q[1]), -1.0f, 1.0f)) * RAD2DEG;
    kf->yaw_deg = atan2f(2.0f * (q[0] * q[3] + q[1] * q[2]),
                         1.0f - 2.0f * (q[2] * q[2] + q[3] * q[3])) * RAD2DEG;

    float lin_w[3];
    rotate_forward(kf->q, a, lin_w);
    kf->earth_accel_mps2[0] = lin_w[0] * G0;
    kf->earth_accel_mps2[1] = lin_w[1] * G0;
    kf->earth_accel_mps2[2] = (lin_w[2] - 1.0f) * G0;

    return 1;
}
