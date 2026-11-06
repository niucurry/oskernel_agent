#include "config.h"

int attempt_count = 0;

static int request_attempt(void) {
    attempt_count++;
    return -1;
}

int send_request(void) {
    int result = -1;
    for (int attempt = 0; attempt <= DEFAULT_RETRIES; attempt++) {
        result = request_attempt();
        if (result == 0) {
            return 0;
        }
    }
    return result;
}
