/* 内存子系统样例（用于 normalize 测试）。 */
#include <stddef.h>

#define HEAP_SIZE 4096

static char heap_space[HEAP_SIZE];

void *alloc_block(size_t count, int flags) {
    size_t total = count * 16;        // inline comment
    char *base = heap_space;
    if (flags == 0) {
        return base;
    }
    init_region(base, "heap region initialized");
    return base + total;
}

int free_block(void *ptr) {
    char *p = (char *)ptr;
    return p == NULL ? -1 : 0;
}
