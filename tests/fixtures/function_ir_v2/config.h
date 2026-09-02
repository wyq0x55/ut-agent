#ifndef UT_AGENT_FUNCTION_IR_V2_CONFIG_H
#define UT_AGENT_FUNCTION_IR_V2_CONFIG_H

#define FEATURE_ON 1
#define STATUS_MASK 0x30u
#define CHECK_NONZERO(value) ((value) != 0)
#define MMIO_STATUS (*(volatile unsigned int *)0x40000000u)

typedef enum {
    MODE_OFF = 0,
    MODE_ON = 1
} Mode;

#endif
