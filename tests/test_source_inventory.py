from oskernel_agent.analysis.source_inventory import quick_symbols


def test_c_inventory_includes_multiline_pointer_functions_and_excludes_prototypes():
    source = """int declared(int x);
struct ops { int (*read)(int); };
static int
read_impl(int x)
{ return x; }
const struct ops *
get_ops(void) { return 0; }
"""
    result = quick_symbols(source, ".c")
    assert result["status"] == "parsed"
    assert result["symbols"] == [("ops", 2), ("read_impl", 4), ("get_ops", 7)]


def test_malformed_c_does_not_claim_inventory_is_complete():
    result = quick_symbols("int okay(void) { return 0; }\nint broken(", ".c")
    assert ("okay", 1) in result["symbols"]
    assert result["status"] == "partial"


def test_header_only_alignment_services_appear_in_inventory():
    # ChibiOS chalign.h has no functions, but exposes these callable macros.
    source = """#define CHALIGN_H
#define MEM_ALIGN_MASK(a) ((size_t)(a) - 1U)
#define MEM_ALIGN_PREV(p, a) \\
  ((size_t)(p) & ~MEM_ALIGN_MASK(a))
#define MEM_ALIGN_NEXT(p, a) \\
  MEM_ALIGN_PREV((size_t)(p) + MEM_ALIGN_MASK(a), (a))
"""
    result = quick_symbols(source, ".h")
    assert result["status"] == "parsed"
    assert result["symbols"] == [
        ("MEM_ALIGN_MASK", 2), ("MEM_ALIGN_PREV", 3), ("MEM_ALIGN_NEXT", 5),
    ]


def test_statement_macros_do_not_invent_control_flow_functions():
    # ThreadX uses interrupt macros without trailing semicolons at call sites.
    # The source is valid after preprocessing, but the raw C tree is partial.
    source = """typedef struct Thread { void (*cleanup)(void); } Thread;
#define LOCK do {} while (0);
#define UNLOCK do {} while (0);
void release(Thread *p) {
    LOCK
    if (p->cleanup) { p->cleanup(); }
    UNLOCK
}
"""
    result = quick_symbols(source, ".c")
    assert result["symbols"] == [("Thread", 1), ("release", 4)]
    assert result["status"] == "partial"


def test_rust_inventory_and_unsupported_assembly_remain_explicit():
    assert quick_symbols("pub fn run() {}\nstruct Task {}", ".rs")["symbols"] == [
        ("run", 1), ("Task", 2),
    ]
    assert quick_symbols("entry:", ".S") == {"symbols": [], "status": "unsupported"}
