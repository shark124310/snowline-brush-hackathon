#include "mpu6050.h"

#include <stdbool.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "mpu6050";
static const int kTransferTimeoutMs = 25;

/* 量程对应的换算系数 */
static const float s_accel_lsb_per_g[] = {
    [0] = 16384.0f,  /* ±2g  */
    [1] = 8192.0f,   /* ±4g  */
    [2] = 4096.0f,   /* ±8g  */
    [3] = 2048.0f,   /* ±16g */
};

static const float s_gyro_lsb_per_dps[] = {
    [0] = 131.0f,    /* ±250 dps  */
    [1] = 65.5f,     /* ±500 dps  */
    [2] = 32.8f,     /* ±1000 dps */
    [3] = 16.4f,     /* ±2000 dps */
};

static int16_t be16(const uint8_t *p)
{
    return (int16_t)(((uint16_t)p[0] << 8) | p[1]);
}

esp_err_t mpu6050_write_reg(mpu6050_t *m, uint8_t reg, uint8_t val)
{
    uint8_t buf[2] = { reg, val };
    return i2c_master_transmit(m->dev, buf, sizeof(buf), kTransferTimeoutMs);
}

esp_err_t mpu6050_read_reg(mpu6050_t *m, uint8_t reg, uint8_t *val)
{
    return i2c_master_transmit_receive(m->dev, &reg, 1, val, 1, kTransferTimeoutMs);
}

static esp_err_t add_device(mpu6050_t *m, i2c_master_bus_handle_t bus, uint8_t addr)
{
    i2c_device_config_t cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = addr,
        /* 100 kHz：模块上拉较弱（内部上拉约 45 kΩ + 杜邦线电容），
         * 400 kHz 时偶发误码会让陀螺数据出现几 dps 的尖峰。 */
        .scl_speed_hz = 100000,
    };
    return i2c_master_bus_add_device(bus, &cfg, &m->dev);
}

static esp_err_t config_sensor(mpu6050_t *m)
{
    esp_err_t err;
    uint8_t id = 0;

    err = mpu6050_read_reg(m, MPU6050_REG_WHO_AM_I, &id);
    if (err != ESP_OK) {
        return err;
    }
    if (id != MPU6050_WHO_AM_I_VALUE) {
        /* 兼容 MPU6500/MPU9250 一类的克隆片，仍然可以继续配置 */
        ESP_LOGW(TAG, "WHO_AM_I = 0x%02X (expected 0x%02X), continuing",
                 id, MPU6050_WHO_AM_I_VALUE);
    } else {
        ESP_LOGI(TAG, "WHO_AM_I = 0x%02X", id);
    }

    /* 软复位，等内部寄存器恢复默认值 */
    err = mpu6050_write_reg(m, MPU6050_REG_PWR_MGMT_1, 0x80);
    if (err != ESP_OK) {
        return err;
    }
    vTaskDelay(pdMS_TO_TICKS(100));

    /* 唤醒，时钟源选 PLL(gyro X)，比内部 RC 稳 */
    err = mpu6050_write_reg(m, MPU6050_REG_PWR_MGMT_1, 0x01);
    if (err != ESP_OK) {
        return err;
    }

    /* DLPF 使能时陀螺输出 1 kHz：div=9 -> 100 Hz，和采样任务同频 */
    err = mpu6050_write_reg(m, MPU6050_REG_SMPLRT_DIV, 9);
    if (err != ESP_OK) {
        return err;
    }

    /* DLPF_CFG=3：accel 44 Hz / gyro 42 Hz，抑制高频噪声且不引入明显滞后 */
    err = mpu6050_write_reg(m, MPU6050_REG_CONFIG, 0x03);
    if (err != ESP_OK) {
        return err;
    }

    err = mpu6050_write_reg(m, MPU6050_REG_GYRO_CONFIG, MPU6050_GYRO_FS_SEL << 3);
    if (err != ESP_OK) {
        return err;
    }

    err = mpu6050_write_reg(m, MPU6050_REG_ACCEL_CONFIG, MPU6050_ACCEL_FS_SEL << 3);
    if (err != ESP_OK) {
        return err;
    }

    vTaskDelay(pdMS_TO_TICKS(50));
    return mpu6050_write_reg(m, MPU6050_REG_INT_ENABLE, MPU6050_DATA_READY);
}

esp_err_t mpu6050_init(mpu6050_t *m, i2c_master_bus_handle_t bus, uint8_t addr)
{
    if (m == NULL || bus == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    m->accel_lsb_per_g = s_accel_lsb_per_g[MPU6050_ACCEL_FS_SEL];
    m->gyro_lsb_per_dps = s_gyro_lsb_per_dps[MPU6050_GYRO_FS_SEL];

    if (addr != 0) {
        esp_err_t err = add_device(m, bus, addr);
        if (err != ESP_OK) {
            return err;
        }
        m->addr = addr;
        err = config_sensor(m);
        if (err != ESP_OK) {
            i2c_master_bus_rm_device(m->dev);
            m->dev = NULL;
        }
        return err;
    }

    /* 自动探测 AD0 接地(0x68) / 接高(0x69) 两种地址 */
    const uint8_t candidates[] = { MPU6050_ADDR_AD0_LOW, MPU6050_ADDR_AD0_HIGH };
    esp_err_t last_err = ESP_ERR_NOT_FOUND;

    for (size_t i = 0; i < sizeof(candidates); i++) {
        esp_err_t err = add_device(m, bus, candidates[i]);
        if (err != ESP_OK) {
            last_err = err;
            continue;
        }
        m->addr = candidates[i];
        err = config_sensor(m);
        if (err == ESP_OK) {
            ESP_LOGI(TAG, "device ready at 0x%02X", m->addr);
            return ESP_OK;
        }
        last_err = err;
        i2c_master_bus_rm_device(m->dev);
        m->dev = NULL;
    }

    return last_err;
}

esp_err_t mpu6050_read(mpu6050_t *m, mpu6050_sample_t *s)
{
    const uint8_t reg = MPU6050_REG_INT_STATUS;
    uint8_t frame[15];
    esp_err_t err = i2c_master_transmit_receive(m->dev, &reg, 1, frame, sizeof(frame),
                                                kTransferTimeoutMs);
    if (err != ESP_OK) {
        return err;
    }
    if (!(frame[0] & MPU6050_DATA_READY)) {
        return ESP_ERR_NOT_FINISHED;
    }

    const uint8_t *raw = &frame[1];
    const int16_t axes[] = { be16(&raw[0]), be16(&raw[2]), be16(&raw[4]),
                             be16(&raw[8]), be16(&raw[10]), be16(&raw[12]) };
    s->ax = axes[0] / m->accel_lsb_per_g;
    s->ay = axes[1] / m->accel_lsb_per_g;
    s->az = axes[2] / m->accel_lsb_per_g;
    s->temp_c = be16(&raw[6]) / 340.0f + 36.53f;
    s->gx = axes[3] / m->gyro_lsb_per_dps;
    s->gy = axes[4] / m->gyro_lsb_per_dps;
    s->gz = axes[5] / m->gyro_lsb_per_dps;
    for (size_t i = 0; i < 3; ++i) {
        s->gyro_raw[i] = axes[3 + i];
    }
    s->clipped = false;
    for (size_t i = 0; i < sizeof(axes) / sizeof(axes[0]); i++) {
        if (axes[i] <= -32760 || axes[i] >= 32760) {
            s->clipped = true;
            break;
        }
    }
    s->timestamp_us = esp_timer_get_time();
    return ESP_OK;
}
