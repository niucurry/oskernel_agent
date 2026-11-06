int lookup(const int *items, int count, int index) {
    if (index < 0 || index >= count) return -1;
    return items[index];
}

int sum_prefix(const int *items, int count) {
    int total = 0;
    for (int i = 0; i < count; i++) total += items[i];
    return total;
}
