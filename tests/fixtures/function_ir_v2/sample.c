#include "config.h"

typedef void (*Callback)(int value);
volatile unsigned int REG_STATUS;
Callback g_callback;

int target(int value, int *out)
{
    if (CHECK_NONZERO(value) && ((value & STATUS_MASK) == 0)) {
        *out = (int)REG_STATUS;
    } else if (value == MODE_ON) {
        *out = FEATURE_ON;
    } else {
        *out = 0;
    }

    switch (value) {
    case MODE_OFF:
        REG_STATUS = 1;
        break;
    default:
        if (g_callback != 0) {
            g_callback(value);
        }
        break;
    }
    return *out;
}

void mmio_touch(unsigned int value)
{
    if (MMIO_STATUS == 0) {
        MMIO_STATUS = value;
    }
}
