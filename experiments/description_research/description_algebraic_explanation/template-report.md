本模块提供内存对齐宏。以下保留原 C 标量表达式；size_t 为原生 64 位无符号数，整数实参无副作用。a 的有效对齐值为 2 的幂；地址值是数值表示，不是实际访问验证。
MEM_ALIGN_MASK(a)=((size_t)(a) - 1U) [sources/chalign.h:60]
MEM_ALIGN_PREV(p, a)=((size_t)(p) & ~MEM_ALIGN_MASK(a)) [sources/chalign.h:68]
MEM_ALIGN_NEXT(p, a)=MEM_ALIGN_PREV((size_t)(p) + MEM_ALIGN_MASK(a), (a)) [sources/chalign.h:79]
MEM_IS_ALIGNED(p, a)=(((size_t)(p) & MEM_ALIGN_MASK(a)) == 0U) [sources/chalign.h:90]
MEM_IS_VALID_ALIGNMENT(a)=(((size_t)(a) != 0U) && (((size_t)(a) & ((size_t)(a) - 1U)) == 0U)) [sources/chalign.h:98]
