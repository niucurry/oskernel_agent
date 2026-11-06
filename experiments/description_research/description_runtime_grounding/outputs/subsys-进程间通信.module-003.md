<h3>模块三：非阻塞轮询与消息读取（chMsgPoll / chMsgGet / chMsgIsPendingI）</h3>
<p>本模块提供不进入睡眠的接收路径。<code>chMsgPollS()</code> 在系统锁内检查当前线程是否有待处理消息，若有则从 <code>msgqueue</code> 头部取出发送者并置为 <code>CH_STATE_SNDMSG</code>，否则返回 <code>NULL</code>（os/rt/src/chmsg.c:188）。头文件包装 <code>chMsgPoll()</code> 完成加解锁（os/rt/include/chmsg.h:153）。</p>
<p>消息内容通过内联函数 <code>chMsgGet()</code> 读取，它断言发送者处于 <code>CH_STATE_SNDMSG</code> 后返回其 <code>sentmsg</code> 字段（os/rt/include/chmsg.h:188）。队列非空判定由 <code>chMsgIsPendingI()</code> 完成，通过比较 <code>msgqueue.next</code> 是否指向自身实现，属于 O(1) 检查（os/rt/include/chmsg.h:171）。</p>
<ul>
<li>轮询接口使接收者可在不阻塞的前提下处理消息，适合事件循环或中断下半部场景。</li>
<li>读取与轮询分离，调用者必须先 poll/wait 再 get，状态机由断言保护。</li>
</ul>
