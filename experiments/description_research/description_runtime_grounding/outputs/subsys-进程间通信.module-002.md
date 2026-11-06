<h3>模块二：消息接收与超时等待（chMsgWait / chMsgWaitTimeout）</h3>
<p>本模块实现接收端的阻塞与超时语义。核心服务函数 <code>chMsgWaitS()</code> 先以 <code>chMsgIsPendingI()</code> 判断当前线程队列是否为空，为空则调用 <code>chSchGoSleepS(CH_STATE_WTMSG)</code> 进入等待态；被唤醒后从 <code>msgqueue</code> 头部取出发送者线程，并将其状态置为 <code>CH_STATE_SNDMSG</code>（os/rt/src/chmsg.c:116）。</p>
<p>带超时版本 <code>chMsgWaitTimeoutS()</code> 使用 <code>chSchGoSleepTimeoutS()</code>，超时返回非 <code>MSG_OK</code> 时直接返回 <code>NULL</code>，从而向调用者区分“收到消息”与“超时”两种结果（os/rt/src/chmsg.c:153）。头文件中的 <code>chMsgWait()</code> 与 <code>chMsgWaitTimeout()</code> 为加锁包装，负责 <code>chSysLock()/chSysUnlock()</code> 配对（os/rt/include/chmsg.h:94、os/rt/include/chmsg.h:126）。</p>
<ul>
<li>超时参数禁止 <code>TIME_IMMEDIATE</code>，由断言在调试期拦截非法用法（os/rt/src/chmsg.c:158）。</li>
<li>返回的发送者指针为临时引用，不增加引用计数，需在 <code>chMsgRelease()</code> 前使用（os/rt/include/chmsg.h:87）。</li>
</ul>
