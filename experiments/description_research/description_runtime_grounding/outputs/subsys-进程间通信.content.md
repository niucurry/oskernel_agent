<h3>子系统总览：进程间通信（同步消息）</h3>
<p>本子系统位于 <code>os/rt</code>，实现 ChibiOS 风格的同步线程间消息机制，由头文件 <code>os/rt/include/chmsg.h</code> 与实现文件 <code>os/rt/src/chmsg.c</code> 两个文件构成，受编译开关 <code>CH_CFG_USE_MESSAGES</code> 控制（os/rt/src/chmsg.c:47）。其核心模型是“发送者阻塞、接收者应答”：发送线程调用 <code>chMsgSend()</code> 后挂起，直到接收线程通过 <code>chMsgWait()</code>/<code>chMsgPoll()</code> 取件、<code>chMsgGet()</code> 读值、<code>chMsgRelease()</code> 回传应答，发送者才被唤醒并取得应答值。</p>
<p>消息以 <code>msg_t</code> 标量传递，不复制数据，仅传指针或标量，因此交换高效；发送者在应答前保持挂起，保证指针数据稳定（os/rt/src/chmsg.c:26）。队列插入顺序可配置为 FIFO 或优先级序（os/rt/include/chmsg.h:52）。接收侧提供阻塞（<code>chMsgWait</code>）、超时（<code>chMsgWaitTimeout</code>）与非阻塞轮询（<code>chMsgPoll</code>）三种等待方式，并以内联 <code>chMsgIsPendingI()</code> 做 O(1) 队列非空判定（os/rt/include/chmsg.h:171）。</p>
<h4>模块列表</h4>
<table>
<tr><th>槽位</th><th>模块</th><th>主要接口</th><th>文件</th></tr>
<tr><td>1</td><td>消息发送与阻塞等待</td><td>chMsgSend</td><td>os/rt/src/chmsg.c</td></tr>
<tr><td>2</td><td>消息接收与超时等待</td><td>chMsgWait / chMsgWaitTimeout</td><td>os/rt/src/chmsg.c、os/rt/include/chmsg.h</td></tr>
<tr><td>3</td><td>非阻塞轮询与消息读取</td><td>chMsgPoll / chMsgGet / chMsgIsPendingI</td><td>os/rt/src/chmsg.c、os/rt/include/chmsg.h</td></tr>
<tr><td>4</td><td>应答释放与线程唤醒</td><td>chMsgRelease / chMsgReleaseS</td><td>os/rt/src/chmsg.c、os/rt/include/chmsg.h</td></tr>
</table>
<p>模块关系：模块一负责入队与发送者挂起，模块二、三负责接收侧取件（阻塞/超时/轮询），模块四负责回传应答并唤醒发送者，四者围绕线程结构中的 <code>msgqueue</code>、<code>sentmsg</code>、<code>u.rdymsg</code> 字段协同，构成一次完整的同步消息事务。</p>
