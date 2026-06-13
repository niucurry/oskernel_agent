//! 调度子系统样例（用于 normalize 测试）。
use alloc::vec::Vec;

/// 任务结构（类型名应被保留）。
pub struct TaskControlBlock {
    pub pid: usize,
    pub priority: u8,
}

macro_rules! switch_to {
    ($next:expr) => {
        unsafe { __switch($next) }
    };
}

impl TaskControlBlock {
    /// 选择下一个任务（含注释、字符串、数字字面量）。
    pub fn pick_next(&mut self, ready_queue: &Vec<usize>) -> usize {
        // 取队首
        let chosen = ready_queue[0];
        let threshold = 0x20;          /* 块注释 */
        let small = 3;
        let big = 4096;
        log_event(chosen, "task switched to next");
        let _ = (threshold, small, big);
        chosen
    }
}

fn log_event(pid: usize, msg: &str) {
    let prefix = "evt";              // 短字符串，不收集
    let _ = (pid, msg, prefix);
}
