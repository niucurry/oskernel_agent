#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include "sources/chalign.h"
int main(void){
printf("BITS %zu\n",sizeof(size_t)*8);
printf("Q1 %zu\n",(size_t)MEM_ALIGN_MASK(((size_t)UINT64_C(16))));
printf("Q2 %zu\n",(size_t)MEM_ALIGN_PREV(((size_t)UINT64_C(65)), ((size_t)UINT64_C(32))));
printf("Q3 %zu\n",(size_t)MEM_ALIGN_NEXT(((size_t)UINT64_C(64)), ((size_t)UINT64_C(32))));
printf("Q4 %zu\n",(size_t)MEM_ALIGN_NEXT(((size_t)UINT64_C(65)), ((size_t)UINT64_C(32))));
printf("Q5 %zu\n",(size_t)MEM_ALIGN_PREV(((size_t)UINT64_C(4294967321)), ((size_t)UINT64_C(16))));
printf("Q6 %zu\n",(size_t)MEM_ALIGN_NEXT(((size_t)UINT64_C(4294967321)), ((size_t)UINT64_C(16))));
printf("Q7 %zu\n",(size_t)MEM_ALIGN_NEXT(((size_t)UINT64_C(18446744073709551608)), ((size_t)UINT64_C(16))));
printf("Q8 %zu\n",(size_t)MEM_ALIGN_PREV(((size_t)UINT64_C(18446744073709551608)), ((size_t)UINT64_C(16))));
printf("Q9 %zu\n",(size_t)MEM_IS_ALIGNED(((size_t)UINT64_C(64)), ((size_t)UINT64_C(32))));
printf("Q10 %zu\n",(size_t)MEM_IS_ALIGNED(((size_t)UINT64_C(65)), ((size_t)UINT64_C(32))));
printf("Q11 %zu\n",(size_t)MEM_IS_VALID_ALIGNMENT(((size_t)UINT64_C(0))));
printf("Q12 %zu\n",(size_t)MEM_IS_VALID_ALIGNMENT(((size_t)UINT64_C(48))));
printf("X1 %zu\n",(size_t)MEM_ALIGN_PREV(7,4));
printf("X2 %zu\n",(size_t)MEM_ALIGN_NEXT(7,4));
printf("X3 %zu\n",(size_t)MEM_ALIGN_NEXT(8,4));
printf("X4 %zu\n",(size_t)MEM_IS_VALID_ALIGNMENT(8));
for(size_t a=1;a<=256;a*=2){for(size_t p=0;p<65536;p++){printf("V %zu %zu %zu %zu\n",p,a,MEM_ALIGN_PREV(p,a),MEM_ALIGN_NEXT(p,a));}} return 0;}
