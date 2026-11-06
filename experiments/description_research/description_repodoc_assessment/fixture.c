typedef struct Thread { void (*cleanup)(void); } Thread;
#define LOCK do {} while (0);
#define UNLOCK do {} while (0);
void release(Thread *p) {
    LOCK
    if (p->cleanup) { p->cleanup(); }
    UNLOCK
}
void *allocate(void) { return (void *)0; }
int plain(void) { return 1; }
