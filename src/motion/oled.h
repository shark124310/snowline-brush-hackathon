/*
 * SSD1306 128x64 OLED 驱动（4 针 I2C 版本）
 *
 * 移植自 STM32 例程 "4针脚I2C版本"，改动：
 *   - 用 ESP-IDF i2c_master 硬件总线代替 GPIO 模拟时序
 *   - 增加 1 KB 显存，整屏刷新只用一次 I2C 传输（原例程每字节一次事务）
 *   - 保留原例程的 8x16 字库和 “行 1~4 / 列 1~16” 的 API 习惯
 *
 * 默认地址 0x3C（8 位写地址 0x78），部分模块是 0x3D，传 0 时自动探测。
 */
#pragma once

#include <stdint.h>

#include "driver/i2c_master.h"
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

#define OLED_WIDTH       128
#define OLED_HEIGHT      64
#define OLED_PAGES       (OLED_HEIGHT / 8)
#define OLED_LINES       4      /* 8x16 字体：4 行 */
#define OLED_COLS        16     /* 8x16 字体：每行 16 个字符 */

typedef struct {
    i2c_master_dev_handle_t dev;
    uint8_t addr;
    uint8_t buf[OLED_PAGES][OLED_WIDTH];       /* 显存，1 = 点亮 */
    uint8_t tx[1 + OLED_PAGES * OLED_WIDTH];   /* 控制字节 0x40 + 显存 */
} oled_t;

/* addr 传 0 时自动尝试 0x3C / 0x3D */
esp_err_t oled_init(oled_t *o, i2c_master_bus_handle_t bus, uint8_t addr);

void oled_clear(oled_t *o);
/* line: 1~4, col: 1~16 */
void oled_show_char(oled_t *o, uint8_t line, uint8_t col, char c);
void oled_show_string(oled_t *o, uint8_t line, uint8_t col, const char *str);

/* 把显存推到屏幕，一次 I2C 传输写完 1024 字节 */
esp_err_t oled_flush(oled_t *o);

#ifdef __cplusplus
}
#endif
