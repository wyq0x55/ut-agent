/* ut-agent 桩：OSEK OS 头遮蔽（单核函数隔离测试不需要真 OS） */
#ifndef OS_STUB_H
#define OS_STUB_H
typedef unsigned char StatusType;
typedef unsigned long TickType;
typedef unsigned char CounterType;
typedef TickType* TickRefType;
typedef unsigned char TaskType;
typedef unsigned long AlarmType;
typedef unsigned char ResourceType;
typedef unsigned char EventType;
typedef unsigned char AppModeType;
#define E_OK 0
#define E_NOT_OK 1
StatusType SuspendOSInterrupts(void);
StatusType ResumeOSInterrupts(void);
StatusType GetCounterValue(CounterType, TickRefType);
StatusType GetElapsedValue(CounterType, TickRefType, TickRefType);
#endif
