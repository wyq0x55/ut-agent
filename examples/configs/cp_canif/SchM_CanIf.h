/* ut-agent 桩：SchM 临界区宏置空（单核函数隔离测试不需要 OS） */
#ifndef SCHM_CANIF_STUB_H
#define SCHM_CANIF_STUB_H
#define SchM_Enter_CanIf_EA_0() ((void)0)
#define SchM_Exit_CanIf_EA_0() ((void)0)
#endif
