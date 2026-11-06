<h3>模块一：消息发送与阻塞等待（chMsgSend）</h3>
<p>本模块实现同步消息的发送端语义。发送者调用 <code>chMsgSend()</code> 后，将消息标量写入自身线程的 <code>sentmsg</code> 字段，并把等待对象指向接收者的 <code>msgqueue</code>，随后通过 <code>__ch_msg_insert()</code> 挂入接收者的消息队列（os/rt/src/chmsg.c:80）。若接收者正处于 <code>CH_STATE_WTMSG</code> 等待态，则立即将其置为就绪并携带 <code>MSG_OK</code>（os/rt/src/chmsg.c:89）。发送者自身转入 <code>CH_STATE_SNDMSGQ</code> 睡眠，直到接收者调用释放接口后才被唤醒，并以 <code>u.rdymsg</code> 作为应答返回值（os/rt/src/chmsg.c:93）。</p>
<p>该设计的关键点是消息不复制数据，仅传递 <code>msg_t</code> 标量或指针，因此交换开销极低；同时发送者在应答前保持挂起，保证指针所指数据在接收者处理期间稳定（os/rt/src/chmsg.c:26）。</p>
<ul>
<li>发送即挂起，形成“客户端—服务器”双向数据交换模型。</li>
<li>队列插入顺序由 <code>CH_CFG_USE_MESSAGES_PRIORITY</code> 决定，可切换 FIFO 或优先级序（os/rt/include/chmsg.h:52）。</li>
<li>发送路径全程在系统锁内完成，保证队列与线程状态变更的原子性。</li>
</ul>
