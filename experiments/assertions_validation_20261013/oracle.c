#include <assert.h>
#include <stdio.h>
extern int attempt_count;
extern int send_request(void);
int main(void) {
    attempt_count = 0;
    int result = send_request();
    assert(result == -1);
    assert(attempt_count == 4);
    printf("{\"total_calls\":%d,\"additional_retries\":%d,\"result\":%d}\n", attempt_count, attempt_count - 1, result);
    return 0;
}
