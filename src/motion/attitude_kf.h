/*
 * 六轴姿态误差状态卡尔曼滤波（ESKF）
 *
 * 状态：四元数 q（机体->世界） + 陀螺零偏 b
 * 误差状态 x = [δθ(3), δb(3)]，6 维
 *
 * 预测：用 (gyro - b) 积分四元数，协方差按误差动力学传播
 * 观测 1：加速度计方向（只在 ||a|-1| 小于阈值时使用）
 *   —— 修正 roll/pitch，并让 x/y 轴零偏可观测
 * 观测 2：静止时“角速度应为 0”
 *   —— 把实测 gyro 当零角速度观测，z 轴零偏也变成可观测，
 *      yaw 的缓慢爬升才会真正停下来
 *
 * 说明：没有磁力计时 yaw 没有绝对参考。静止时靠观测 2 把零偏压住；
 * 运动过程中 yaw 仍只靠积分，这是六轴的物理限制，卡尔曼滤波也改变不了。
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    /* 过程噪声（连续时间方差率） */
    float q_angle;          /* 姿态随机游走，默认 4e-6 rad^2/s */
    float q_bias;           /* 零偏随机游走，默认 1e-8 (rad/s)^2/s */

    /* 观测噪声 */
    float r_accel;          /* 加速度方向观测，默认 1e-3 */
    float r_gyro;           /* 零角速度观测，默认 4e-6 (rad/s)^2 */

    /* 使用条件 */
    float accel_reject_g;   /* ||a|-1| 超过该值不用加速度计，默认 0.15 */
    float rest_gyro_dps;    /* 静止判据：平滑后 |gyro-bias|，默认 0.35 dps */
    float rest_accel_g;     /* 静止判据：||a|-1|，默认 0.08（容忍加速度零偏） */
    float rest_time_s;      /* 连续静止多久才开始零角速度观测，默认 1.0 s */

    /* 静止时慢速修正加速度比例（|a| 应为 1 g），0 表示关闭 */
    float accel_scale_tau_s; /* 默认 10 s */
} kf_config;

typedef struct {
    kf_config cfg;

    float q[4];             /* 四元数 w,x,y,z，机体 -> 世界 */
    float bias[3];          /* 陀螺零偏，rad/s */
    float P[6][6];          /* 误差状态协方差 */

    float accel_scale;      /* 加速度比例修正 */

    /* 静止判据的平滑状态 */
    float rest_gyro_lp[3];
    float rest_accel_lp;
    bool rest_lp_ready;
    float rest_time;
    bool rest;

    /* 输出 */
    float roll_deg, pitch_deg, yaw_deg;
    float earth_accel_mps2[3]; /* 世界系线加速度，已去除重力 */
    float res_dps[3];       /* 本帧补偿后的机体角速度残差（诊断用） */
    float yaw_rate_world_dps; /* 世界系 Z 轴角速度（诊断用） */
    bool accel_used;        /* 本帧是否用了加速度观测 */
    bool ready;
} kf_t;

kf_config kf_default_config(void);

void kf_init(kf_t *kf, const kf_config *cfg);

/* 上电标定结果：单位 dps */
void kf_set_gyro_bias_dps(kf_t *kf, float bx, float by, float bz);
void kf_set_accel_scale(kf_t *kf, float scale);

/* 用静止加速度均值初始化倾角（yaw 置 0） */
void kf_align_tilt(kf_t *kf, float ax, float ay, float az);

/* 每个新样本调用一次。加速度 g，角速度 dps，dt 秒。返回 1 表示输出有效。 */
int kf_update(kf_t *kf, float ax, float ay, float az,
              float gx, float gy, float gz, float dt);

#ifdef __cplusplus
}
#endif
