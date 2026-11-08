/*
 * MPU6050 轻量驱动（I2C 新版 master API）
 *
 * 只做两件事：上电配置 + 一次性读出 6 轴原始数据（已换算成 g / deg/s）。
 * 姿态解算交给 ESKF，驱动层不做滤波。
 */
#pragma once

#include <stdint.h>
#include <stdbool.h>

#include "driver/i2c_master.h"
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* MPU6050 寄存器 */
#define MPU6050_REG_SMPLRT_DIV    0x19
#define MPU6050_REG_CONFIG        0x1A
#define MPU6050_REG_GYRO_CONFIG   0x1B
#define MPU6050_REG_ACCEL_CONFIG  0x1C
#define MPU6050_REG_INT_ENABLE    0x38
#define MPU6050_REG_INT_STATUS    0x3A
#define MPU6050_DATA_READY        0x01
#define MPU6050_SLEEP             0x40
#define MPU6050_REG_PWR_MGMT_1    0x6B
#define MPU6050_REG_WHO_AM_I      0x75

#define MPU6050_ADDR_AD0_LOW      0x68
#define MPU6050_ADDR_AD0_HIGH     0x69
#define MPU6050_WHO_AM_I_VALUE    0x68

/* 量程选择：accel ±2g，gyro ±500 dps，与换算系数一一对应 */
#define MPU6050_ACCEL_FS_SEL      0
#define MPU6050_GYRO_FS_SEL       1

typedef struct {
    i2c_master_dev_handle_t dev;
    uint8_t addr;
    float accel_lsb_per_g;
    float gyro_lsb_per_dps;
} mpu6050_t;

typedef struct {
    float ax, ay, az;   /* 加速度，单位 g */
    float gx, gy, gz;   /* 角速度，单位 deg/s */
    int16_t gyro_raw[3]; /* 传感器原始 X/Y/Z 寄存器值 */
    float temp_c;       /* 芯片温度，单位摄氏度 */
    int64_t timestamp_us;
    bool clipped;
} mpu6050_sample_t;

/*
 * 在已有 I2C 总线上添加设备并完成上电配置。
 * addr 传 0 时自动尝试 0x68 / 0x69。
 * 返回 ESP_OK 表示 WHO_AM_I 校验通过且配置写入成功。
 */
esp_err_t mpu6050_init(mpu6050_t *m, i2c_master_bus_handle_t bus, uint8_t addr);

/* 读取最新帧。没有新样本时返回 ESP_ERR_NOT_FINISHED。 */
esp_err_t mpu6050_read(mpu6050_t *m, mpu6050_sample_t *s);

/* 读/写单个寄存器，供调试使用。 */
esp_err_t mpu6050_read_reg(mpu6050_t *m, uint8_t reg, uint8_t *val);
esp_err_t mpu6050_write_reg(mpu6050_t *m, uint8_t reg, uint8_t val);

#ifdef __cplusplus
}
#endif
