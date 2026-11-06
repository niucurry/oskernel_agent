本模块提供内存对齐宏。以下保留原 C 标量表达式；size_t 为原生 64 位无符号数，整数实参无副作用。a 的有效对齐值为 2 的幂；地址值是数值表示，不是实际访问验证。
代数重表达（限定无副作用整数实参、原生 size_t 64 位，a 为有效对齐数）：MEM_ALIGN_MASK(a)=a-1；MEM_ALIGN_PREV(p,a)=floor((p mod 2^64)/a)*a；MEM_ALIGN_NEXT(p,a)=floor(((p+a-1) mod 2^64)/a)*a。floor 表示数学向下取整。两个检测宏仍按原始布尔表达式理解。此处只给出标量地址表示，不证明实际内存访问、OS 能力或所有 C 实参形式。
MEM_IS_ALIGNED(p, a)=(((size_t)(p) & MEM_ALIGN_MASK(a)) == 0U) [sources/chalign.h:90]
MEM_IS_VALID_ALIGNMENT(a)=(((size_t)(a) != 0U) && (((size_t)(a) & ((size_t)(a) - 1U)) == 0U)) [sources/chalign.h:98]