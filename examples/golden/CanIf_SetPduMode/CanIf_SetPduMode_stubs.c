/*
 * 自动生成 stub —— 被测函数: CanIf_SetPduMode
 * 源码: classic-platform/communication/CanIf/src/CanIf.c L1342
 * 前提配置: CANIF_CHANNEL_CNT=2 | CANIF_PUBLIC_DEV_ERROR_DETECT=STD_ON
 *           CANIF_PUBLIC_PN_SUPPORT=STD_OFF | CANIF_PUBLIC_TX_BUFFERING=STD_OFF
 *
 * 规则: 被测函数内所有调用函数 stub 化; 只留 callcnt 与引数入出力; 无逻辑
 * 命名: stub 编号 k 按调用顺序从 00 起; ARG<k>_<形参名>=入力记录; PTIN<k>_<形参名>=传入指针记录; PTOUT<k>_<形参名>[CALL_MAX]=传出设定; CALLRET<k>[CALL_MAX]=返回值(仅参与分支判定时生成)
 * 调用集(宏展开后): #00 Det_ReportError —— 出自 VALIDATE_RV 展开 (DEV_ERROR_DETECT=STD_ON 时可达)
 *   (PN=STD_ON 时追加 setPnFilterEnable; TX_BUFFERING=STD_ON 时追加 ChannelOnEnterPDUMode)
 */
#include "Std_Types.h"  /* uint8 / uint16 / uint32 / boolean */

#define CALL_MAX 16  /* 单用例内单 stub 最大调用次数上限, 可配置 */

/* ---- 调用#00: Std_ReturnType Det_ReportError(uint16 ModuleId, uint8 InstanceId, uint8 ApiId, uint8 ErrorId) ----
 * (真实声明见 Det.h L156；Arctic Core 的 Det_ReportError 返回 Std_ReturnType，非标准 void) */
uint32 callcnt00 = 0;                /* 每用例执行前置 0 */
uint16 ARG00_ModuleId[CALL_MAX];     /* 入力记录: 第 n 次调用写 [n-1]; 实际值恒 CANIF_MODULE_ID(配置值) */
uint8  ARG00_InstanceId[CALL_MAX];   /* 入力记录: 恒 0 */
uint8  ARG00_ApiId[CALL_MAX];        /* 入力记录: CANIF_SETPDUMODE_ID */
uint8  ARG00_ErrorId[CALL_MAX];      /* 入力记录: CANIF_E_UNINIT / CANIF_E_PARAM_CONTROLLERID */
/* 本 stub 无指针形参 -> 无 PTOUT ; 返回值不控制任何条件语句 -> 不生成 CALLRET(规格 §7-7) */

Std_ReturnType Det_ReportError(uint16 ModuleId, uint8 InstanceId, uint8 ApiId, uint8 ErrorId)
{
    ARG00_ModuleId[callcnt00]   = ModuleId;
    ARG00_InstanceId[callcnt00] = InstanceId;
    ARG00_ApiId[callcnt00]      = ApiId;
    ARG00_ErrorId[callcnt00]    = ErrorId;
    callcnt00++;   /* 先记录后递增, 索引从 0 起 */
    return 0;      /* 返回值未参与分支判定: 返回类型零值 */
}
