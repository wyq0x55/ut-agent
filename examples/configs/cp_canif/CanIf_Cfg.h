/* ut-agent 注入的最小配置头 —— 替代真实项目中由配置工具生成的 CanIf_Cfg.h
 * 结构性配置与 examples/golden/CanIf_SetPduMode 的前提配置一致。
 * 注意：DET 上报 ID（CANIF_E_xx、CANIF_xx_ID、CANIF_MODULE_ID）由仓库 CanIf.h /
 * CanIf_Types.h 真实定义，此处不得重复定义。 */
#ifndef CANIF_CFG_MIN_H
#define CANIF_CFG_MIN_H

/* ---- 结构性配置 ---- */
#define CANIF_CHANNEL_CNT 2
#define CANIF_TRANSCEIVER_CHANNEL_CNT 1
#define CANIF_ARC_TRANSCEIVER_API 0
#define CANIF_CANFD_SUPPORT 0
#define CANIF_ARC_MAX_NOF_TX_BUFFERS 4
#define CANIF_ARC_MAX_NUM_LPDU_TX_BUF 4
#define CANIF_PUBLIC_DEV_ERROR_DETECT 1
#define CANIF_PUBLIC_PN_SUPPORT 0
#define CANIF_PUBLIC_TX_BUFFERING 0
#define CANIF_PUBLIC_CANCEL_TRANSMIT_SUPPORT 0
#define CANIF_PUBLIC_CDD_HEADERFILE 0
#define CANIF_PUBLIC_CHANGE_BAUDRATE_SUPPORT 0
#define CANIF_PUBLIC_HANDLE_TYPE_ENUM 0
#define CANIF_PUBLIC_READRXPDU_DATA_API 1
#define CANIF_PUBLIC_READRXPDU_NOTIFY_STATUS_API 1
#define CANIF_PUBLIC_READTXPDU_NOTIFY_STATUS_API 1
#define CANIF_PUBLIC_SETDYNAMICTXID_API 1
#define CANIF_PUBLIC_TXCONFIRM_POLLING_SUPPORT 0
#define CANIF_PUBLIC_VERSION_INFO_API 0
#define CANIF_PUBLIC_WAKEUP_CHECK_VALIDATION_API 0
#define CANIF_PUBLIC_WAKEUP_CHECK_VALIDATION_SUPPORT 0
#define CANIF_PUBLIC_WAKEUP_CHECK_VALID_BY_NM 0
#define CANIF_PRIVATE_SOFTWARE_FILTER_TYPE_LINEAR 1
#define NO_CANIF_HRH 0xFF

/* ---- 类型定义（生成配置中才会出现的类型；仓库不含，属配置注入范畴） ---- */
typedef unsigned long long Can_IdType;
typedef unsigned short Can_HwHandleType;
typedef unsigned char CanIf_Arc_ChannelIdType;
typedef unsigned char Can_Arc_ErrorType;

/* Can 驱动接口（drivers/Can 不在本仓库，属配置注入范畴） */
typedef struct {
    unsigned long id;
    unsigned char length;
    unsigned char* sdu;
    unsigned char swPduHandle;
} Can_PduType;
typedef unsigned char Can_ReturnType;
typedef unsigned char Can_StateTransitionType;
#define CAN_OK 0
#define CAN_NOT_OK 1
#define CAN_BUSY 2
#define CAN_T_STOP 0
#define CAN_T_SLEEP 1
#define CAN_T_START 2
#define CAN_T_WAKEUP 3
unsigned char Can_SetControllerMode(unsigned char, unsigned char);
unsigned char Can_Write(unsigned char, const Can_PduType*);

#include "CanIf_ConfigTypes.h"

#endif
