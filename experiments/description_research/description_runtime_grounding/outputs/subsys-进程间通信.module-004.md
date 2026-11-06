<h3>模块四：应答释放与线程唤醒（chMsgRelease / chMsgReleaseS）</h3>
<p>本模块实现消息事务的收尾环节。接收者处理完消息后调用 <code>chMsgRelease()</code>，在系统锁内断言发送者处于 <code>CH_STATE_SNDMSG</code>，再调用 <code>chMsgReleaseS()</code> 完成唤醒（os/rt/src/chmsg.c:210）。<code>chMsgReleaseS()</code> 是内联的 <code>@sclass</code> 接口，直接以 <code>chSchWakeupS(tp, msg)</code> 将应答消息作为发送者的唤醒返回值（os/rt/include/chmsg.h:205）。</p>
<p>由此形成完整的同步握手：发送者挂起 → 接收者 wait/poll 取件 → get 读值 → release 回传应答 → 发送者从 <code>chMsgSend()</code> 返回应答值。应答消息通过 <code>chSchWakeupS()</code> 写入发送者的 <code>u.rdymsg</code>，与发送路径的读取点严格对应（os/rt/src/chmsg.c:94）。</p>
<ul>
<li>释放接口同时承担“确认接收”和“回传应答”双重职责。</li>
<li>提供 <code>S</code> 后缀版本供已持锁的调度上下文使用，避免重复加锁。</li>
</ul>
