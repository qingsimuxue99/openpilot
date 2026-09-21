// minimal code to fake a panda for tests
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>

#include "utils.h"

#define ALLOW_DEBUG
#define PANDA

void print(const char *a) {
  printf("%s", a);
}

// ★ 2026-09-18: safety 层多处调试打印用到 putui()（真实固件在 panda/board/drivers/uart.h:137），
// 但本测试替身只提供了 print()，导致 libsafety.so 留有未定义符号 putui，
// dlopen 时报 "undefined symbol: putui" —— 整个 safety 测试套件因此从来跑不起来。
// 这里补一个空实现，让测试可运行（不改变任何安全逻辑）。
void putui(uint32_t i) {
  printf("%u", i);
}


void puth(unsigned int i) {
  printf("%u", i);
}

typedef struct {
  uint32_t CNT;
} TIM_TypeDef;

TIM_TypeDef timer;
TIM_TypeDef *MICROSECOND_TIMER = &timer;
uint32_t microsecond_timer_get(void);

uint32_t microsecond_timer_get(void) {
  return MICROSECOND_TIMER->CNT;
}
