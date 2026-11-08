#include "oled.h"

#include <stdint.h>
#include <string.h>

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "oled_font.h"

static const char *TAG = "oled";

static esp_err_t write_cmds(oled_t *o, const uint8_t *cmds, size_t len)
{
    uint8_t tx[1 + 64];
    if (len > sizeof(tx) - 1) {
        return ESP_ERR_INVALID_SIZE;
    }
    tx[0] = 0x00;   /* Co=0, D/C#=0：后续字节都是命令 */
    memcpy(&tx[1], cmds, len);
    return i2c_master_transmit(o->dev, tx, len + 1, 75);
}

static esp_err_t write_data(oled_t *o, const uint8_t *data, size_t len)
{
    o->tx[0] = 0x40;   /* Co=0, D/C#=1：后续字节都是显存数据 */
    memcpy(&o->tx[1], data, len);
    return i2c_master_transmit(o->dev, o->tx, len + 1, 75);
}

static esp_err_t init_sequence(oled_t *o)
{
    static const uint8_t cmds[] = {
        0xAE,               /* 关显示 */
        0xD5, 0x80,         /* 时钟分频 */
        0xA8, 0x3F,         /* 复用率 1/64 */
        0xD3, 0x00,         /* 显示偏移 0 */
        0x40,               /* 起始行 0 */
        0xA1,               /* 左右正常 */
        0xC8,               /* 上下正常 */
        0xDA, 0x12,         /* COM 引脚配置 */
        0x81, 0xCF,         /* 对比度 */
        0xD9, 0xF1,         /* 预充电 */
        0xDB, 0x30,         /* VCOMH */
        0xA4,               /* 按显存显示 */
        0xA6,               /* 正常显示（非反色） */
        0x8D, 0x14,         /* 打开电荷泵 */
        0x20, 0x00,         /* 水平寻址模式：整屏一次写 */
        0x21, 0x00, 0x7F,   /* 列 0~127 */
        0x22, 0x00, 0x07,   /* 页 0~7 */
        0xAF,               /* 开显示 */
    };
    return write_cmds(o, cmds, sizeof(cmds));
}

esp_err_t oled_init(oled_t *o, i2c_master_bus_handle_t bus, uint8_t addr)
{
    if (o == NULL || bus == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    memset(o, 0, sizeof(*o));

    const uint8_t candidates[] = { 0x3C, 0x3D };
    uint8_t found = 0;

    if (addr != 0) {
        if (i2c_master_probe(bus, addr, 100) != ESP_OK) {
            return ESP_ERR_NOT_FOUND;
        }
        found = addr;
    } else {
        for (size_t i = 0; i < sizeof(candidates) && !found; i++) {
            if (i2c_master_probe(bus, candidates[i], 100) == ESP_OK) {
                found = candidates[i];
            }
        }
        if (!found) {
            return ESP_ERR_NOT_FOUND;
        }
    }

    i2c_device_config_t cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = found,
        .scl_speed_hz = 400000,
    };
    esp_err_t err = i2c_master_bus_add_device(bus, &cfg, &o->dev);
    if (err != ESP_OK) {
        return err;
    }
    o->addr = found;

    vTaskDelay(pdMS_TO_TICKS(50));   /* 上电稳定 */
    err = init_sequence(o);
    if (err != ESP_OK) {
        i2c_master_bus_rm_device(o->dev);
        o->dev = NULL;
        return err;
    }

    oled_clear(o);
    err = oled_flush(o);
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "SSD1306 ready at 0x%02X", found);
    } else {
        i2c_master_bus_rm_device(o->dev);
        o->dev = NULL;
    }
    return err;
}

void oled_clear(oled_t *o)
{
    memset(o->buf, 0x00, sizeof(o->buf));
}

void oled_show_char(oled_t *o, uint8_t line, uint8_t col, char c)
{
    if (line < 1 || line > OLED_LINES || col < 1 || col > OLED_COLS) {
        return;
    }
    uint8_t idx = (uint8_t)c;
    if (idx < ' ' || idx > '~') {
        idx = ' ';
    }
    idx -= ' ';

    uint8_t page = (uint8_t)((line - 1) * 2);
    uint8_t x = (uint8_t)((col - 1) * 8);
    for (uint8_t i = 0; i < 8; i++) {
        o->buf[page][x + i] = OLED_F8x16[idx][i];
        o->buf[page + 1][x + i] = OLED_F8x16[idx][i + 8];
    }
}

void oled_show_string(oled_t *o, uint8_t line, uint8_t col, const char *str)
{
    for (uint8_t i = 0; str[i] != '\0'; i++) {
        if (col + i > OLED_COLS) {
            break;
        }
        oled_show_char(o, line, (uint8_t)(col + i), str[i]);
    }
}

esp_err_t oled_flush(oled_t *o)
{
    if (o->dev == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    /* 水平寻址模式下，写完一页自动换下一页，所以整屏一次传完 */
    return write_data(o, &o->buf[0][0], sizeof(o->buf));
}
