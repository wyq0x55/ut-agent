/*
 * 自动生成 stub —— 被测函数: CanIf_SetPduMode
 * 源码: /mnt/c/Users/Adminis/.zcode/workspace/default/ut-agent/examples/classic-platform/communication/CanIf/src/CanIf.c L1342
 *
 * 规则: 被测函数内所有调用函数 stub 化; 只留 callcnt 与引数入出力; 无逻辑
 * 命名: stub 编号 k 按调用顺序从 00 起; ARG<k>_<形参名>=入力记录;
 *       PTIN<k>_<形参名>=传入指针记录; PTOUT<k>_<形参名>[CALL_MAX]=传出设定;
 *       CALLRET<k>[CALL_MAX]=返回值(仅参与分支判定时生成)
 */
#include "Std_Types.h"

#define CALL_MAX 16  /* 单用例内单 stub 最大调用次数上限, 可配置 */

/* ---- 调用#00: Det_ReportError ---- */
uint32 callcnt00 = 0;   /* 每用例执行前置 0 */
uint16 ARG00_ModuleId[CALL_MAX];  /* 入力记录 */
uint8 ARG00_InstanceId[CALL_MAX];  /* 入力记录 */
uint8 ARG00_ApiId[CALL_MAX];  /* 入力记录 */
uint8 ARG00_ErrorId[CALL_MAX];  /* 入力记录 */

Std_ReturnType Det_ReportError(uint16 ModuleId, uint8 InstanceId, uint8 ApiId, uint8 ErrorId)
{
    if (callcnt00 >= CALL_MAX) {
        return 0;
    }
    ARG00_ModuleId[callcnt00] = ModuleId;
    ARG00_InstanceId[callcnt00] = InstanceId;
    ARG00_ApiId[callcnt00] = ApiId;
    ARG00_ErrorId[callcnt00] = ErrorId;
    callcnt00++;   /* 先记录后递增, 索引从 0 起 */
    return 0;   /* 返回值未参与分支判定: 不加 CALLRET, 返回类型零值 */
}
