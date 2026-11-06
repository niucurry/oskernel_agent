#include <assert.h>
int lookup(const int *, int, int);
int sum_prefix(const int *, int);
int main(void) {
    int values[] = {2, 7, 11};
    assert(lookup(values, 3, -1) == -1);
    assert(lookup(values, 3, 3) == -1);
    assert(lookup(values, 3, 1) == 7);
    assert(sum_prefix(values, 3) == 20);
    return 0;
}
