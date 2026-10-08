# 描述报告修复：可复现的输入与输出

日期：2026-09-27。下面是专门构造的回归用例，不是自然错误率，也不是论文效果。

修复前快照位于 baseline/；脚本与结果 JSON 一并保留。


## no_invented_dependency


输入：
```text
构建了完整的 TCP/IP 网络协议栈。
```


修复前：
```html
构建了基于 smoltcp 的 TCP/IP 网络能力
```


修复后：
```html
构建了TCP/IP 网络能力
```


## no_keyword_based_deletion


输入：
```text
fork 失败时回收页框；mmap 失败时保留旧映射。
```


修复前：
```html

```


修复后：
```html
fork 失败时回收页框；mmap 失败时保留旧映射。
```


## no_invented_fallback_or_lost_independent_sentence


输入：
```text
覆盖 120 个系统调用。未知编号由 dispatch 返回 ENOSYS；失败时保留旧映射。
```


修复前：
```html
sys_nisyscall 对未实现编号返回 ENOSYS；函数定义正则扫描识别到 48/60 个标准名称，该数字不代表接口语义可用。
```


修复后：
```html
函数定义正则扫描识别到 48/60 个标准系统调用名称；该计数只表示接口线索，不代表语义可用或测试通过。未知编号由 dispatch 返回 ENOSYS；失败时保留旧映射
```


## long_statement_available_intact


输入：
```text
模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，但仅在单核配置下启用回收。
```


修复前：
```html

<section id="modules" data-section-id="modules" class="brief-card p-5 mb-5">
  <h2 class="text-xl font-bold mb-1">模块概览</h2>
  <p class="text-xs text-slate-500 mb-3">按仓库实际设计拆分并列模块；笼统“其他”会展开为真实子模块。每项分析不超过 300 字，重要问题不重复，低风险局部问题在所属模块内说明；实现依据覆盖各子模块的代表位置。第三方库本身不计作作品缺陷或自研亮点，只评价项目适配代码及系统可见行为。</p>
  <div><article class="core-card" data-subsystem="内存" data-analysis-chars="294"><h3 class="font-bold mb-1">内存</h3><p class="text-sm leading-relaxed"><strong>静态实现：</strong>模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数。</p><div class="text-xs text-slate-500 mt-2"><strong>实现依据：</strong><ul class="inline" data-evidence-count="1"><li class="inline mr-3"><a class="file-jump" href="#kernel/mm.c:1">kernel/mm.c:1</a></li></ul></div></article></div>
</section>

```


修复后：
```html

<section id="modules" data-section-id="modules" class="brief-card p-5 mb-5">
  <h2 class="text-xl font-bold mb-1">模块概览</h2>
  <p class="text-xs text-slate-500 mb-3">按仓库实际设计拆分并列模块；笼统“其他”会展开为真实子模块。每项概览正文不超过 300 字；超出篇幅的实现与亮点可展开查看完整说明。问题陈述完整保留，重要问题集中列示，低风险局部问题在所属模块内说明；实现依据覆盖各子模块的代表位置。第三方库本身不计作作品缺陷或自研亮点，只评价项目适配代码及系统可见行为。</p>
  <div><article class="core-card" data-subsystem="内存" data-analysis-chars="0" data-detail-chars="362" data-issue-chars="0"><h3 class="font-bold mb-1">内存</h3><details class="text-sm mt-2" data-description-overflow="true"><summary>补充实现与亮点（1 条完整说明）</summary><ul class="list-disc pl-4 mt-1 space-y-0.5"><li>模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，模块维护映射与页框引用计数，但仅在单核配置下启用回收</li></ul></details><div class="text-xs text-slate-500 mt-2"><strong>实现依据：</strong><ul class="inline" data-evidence-count="1"><li class="inline mr-3"><a class="file-jump" href="#kernel/mm.c:1">kernel/mm.c:1</a></li></ul></div></article></div>
</section>

```


## plain_comparisons_not_treated_as_html


输入：
```text
当 fd<0、fd>=NOFILE 或 ofile[fd]==0 时返回 -1。
```


修复前：
```html

<section id="modules" data-section-id="modules" class="brief-card p-5 mb-5">
  <h2 class="text-xl font-bold mb-1">模块概览</h2>
  <p class="text-xs text-slate-500 mb-3">按仓库实际设计拆分并列模块；笼统“其他”会展开为真实子模块。每项分析不超过 300 字，重要问题不重复，低风险局部问题在所属模块内说明；实现依据覆盖各子模块的代表位置。第三方库本身不计作作品缺陷或自研亮点，只评价项目适配代码及系统可见行为。</p>
  <div><article class="core-card" data-subsystem="内存" data-analysis-chars="34"><h3 class="font-bold mb-1">内存</h3><p class="text-sm leading-relaxed"><strong>静态实现：</strong>当 fd =NOFILE 或 ofile[fd]==0 时返回 -1</p><div class="text-xs text-slate-500 mt-2"><strong>实现依据：</strong><ul class="inline" data-evidence-count="1"><li class="inline mr-3"><a class="file-jump" href="#kernel/mm.c:1">kernel/mm.c:1</a></li></ul></div></article></div>
</section>

```


修复后：
```html

<section id="modules" data-section-id="modules" class="brief-card p-5 mb-5">
  <h2 class="text-xl font-bold mb-1">模块概览</h2>
  <p class="text-xs text-slate-500 mb-3">按仓库实际设计拆分并列模块；笼统“其他”会展开为真实子模块。每项概览正文不超过 300 字；超出篇幅的实现与亮点可展开查看完整说明。问题陈述完整保留，重要问题集中列示，低风险局部问题在所属模块内说明；实现依据覆盖各子模块的代表位置。第三方库本身不计作作品缺陷或自研亮点，只评价项目适配代码及系统可见行为。</p>
  <div><article class="core-card" data-subsystem="内存" data-analysis-chars="39" data-detail-chars="0" data-issue-chars="0"><h3 class="font-bold mb-1">内存</h3><div class="text-sm leading-relaxed"><strong>静态实现：</strong><ul class="list-disc pl-4 mt-1 space-y-0.5"><li>当 fd&lt;0、fd&gt;=NOFILE 或 ofile[fd]==0 时返回 -1</li></ul></div><div class="text-xs text-slate-500 mt-2"><strong>实现依据：</strong><ul class="inline" data-evidence-count="1"><li class="inline mr-3"><a class="file-jump" href="#kernel/mm.c:1">kernel/mm.c:1</a></li></ul></div></article></div>
</section>

```


## semicolon_highlight_keeps_own_citation


输入：
```text
映射成功后更新引用计数；撤销时同步回收空闲页框。
```


修复前：
```html

<section id="modules" data-section-id="modules" class="brief-card p-5 mb-5">
  <h2 class="text-xl font-bold mb-1">模块概览</h2>
  <p class="text-xs text-slate-500 mb-3">按仓库实际设计拆分并列模块；笼统“其他”会展开为真实子模块。每项分析不超过 300 字，重要问题不重复，低风险局部问题在所属模块内说明；实现依据覆盖各子模块的代表位置。第三方库本身不计作作品缺陷或自研亮点，只评价项目适配代码及系统可见行为。</p>
  <div><article class="core-card" data-subsystem="内存" data-analysis-chars="27"><h3 class="font-bold mb-1">内存</h3><p class="text-sm leading-relaxed"><strong>静态实现：</strong>管理页表</p><div class="text-sm leading-relaxed"><strong>实现亮点：</strong><ul class="list-disc pl-4 mt-1 space-y-0.5"><li>映射成功后更新引用计数</li><li>撤销时同步回收空闲页框</li></ul></div><div class="text-xs text-slate-500 mt-2"><strong>实现依据：</strong><ul class="inline" data-evidence-count="1"><li class="inline mr-3"><a class="file-jump" href="#kernel/mm.c:42">kernel/mm.c:42</a></li></ul></div></article></div>
</section>

```


修复后：
```html

<section id="modules" data-section-id="modules" class="brief-card p-5 mb-5">
  <h2 class="text-xl font-bold mb-1">模块概览</h2>
  <p class="text-xs text-slate-500 mb-3">按仓库实际设计拆分并列模块；笼统“其他”会展开为真实子模块。每项概览正文不超过 300 字；超出篇幅的实现与亮点可展开查看完整说明。问题陈述完整保留，重要问题集中列示，低风险局部问题在所属模块内说明；实现依据覆盖各子模块的代表位置。第三方库本身不计作作品缺陷或自研亮点，只评价项目适配代码及系统可见行为。</p>
  <div><article class="core-card" data-subsystem="内存" data-analysis-chars="27" data-detail-chars="0" data-issue-chars="0"><h3 class="font-bold mb-1">内存</h3><div class="text-sm leading-relaxed"><strong>静态实现：</strong><ul class="list-disc pl-4 mt-1 space-y-0.5"><li>管理页表</li></ul></div><div class="text-sm leading-relaxed"><strong>实现亮点：</strong><ul class="list-disc pl-4 mt-1 space-y-0.5"><li>映射成功后更新引用计数；撤销时同步回收空闲页框 <span class="text-xs text-slate-400">（<a class="file-jump" href="#kernel/mm.c:42">kernel/mm.c:42</a>）</span></li></ul></div><div class="text-xs text-slate-500 mt-2"><strong>实现依据：</strong><ul class="inline" data-evidence-count="1"><li class="inline mr-3"><a class="file-jump" href="#kernel/mm.c:42">kernel/mm.c:42</a></li></ul></div></article></div>
</section>

```


## long_issue_keeps_qualification


输入：
```text
资源回收路径需要人工复核，资源回收路径需要人工复核，资源回收路径需要人工复核，资源回收路径需要人工复核，资源回收路径需要人工复核，资源回收路径需要人工复核，资源回收路径需要人工复核，资源回收路径需要人工复核，资源回收路径需要人工复核，资源回收路径需要人工复核，资源回收路径需要人工复核，资源回收路径需要人工复核，资源回收路径需要人工复核，资源回收路径需要人工复核，资源回收路径需要人工复核，资源回收路径需要人工复核，此问题只影响调试配置。
```


修复前：
```html

<section id="modules" data-section-id="modules" class="brief-card p-5 mb-5">
  <h2 class="text-xl font-bold mb-1">模块概览</h2>
  <p class="text-xs text-slate-500 mb-3">按仓库实际设计拆分并列模块；笼统“其他”会展开为真实子模块。每项分析不超过 300 字，重要问题不重复，低风险局部问题在所属模块内说明；实现依据覆盖各子模块的代表位置。第三方库本身不计作作品缺陷或自研亮点，只评价项目适配代码及系统可见行为。</p>
  <div><article class="core-card" data-subsystem="内存" data-analysis-chars="146"><h3 class="font-bold mb-1">内存</h3><p class="text-sm leading-relaxed"><strong>静态实现：</strong>管理页表</p><div class="text-sm leading-relaxed"><strong>局部问题：</strong><ul class="list-disc pl-4 mt-1 space-y-0.5"><li>资源回收路径需要人工复核，资源回收路径需要人工复核，资源回收路径需要人工复核，资源回收路径需要人工复核，资源回收路径需要人工复核，资源回收路径需要人工复核，资源回收路径需要人工复核，资源回收路径需要人工复核，资源回收路径需要人工复核，资源回收路径需要人工复核，资源回收路径需要人工复核。 <span class="text-xs text-slate-400">（<a class="file-jump" href="#kernel/mm.c:50">kernel/mm.c:50</a>）</span></li></ul></div><div class="text-xs text-slate-500 mt-2"><strong>实现依据：</strong><ul class="inline" data-evidence-count="1"><li class="inline mr-3"><a class="file-jump" href="#kernel/mm.c:1">kernel/mm.c:1</a></li></ul></div></article></div>
</section>

```


修复后：
```html

<section id="modules" data-section-id="modules" class="brief-card p-5 mb-5">
  <h2 class="text-xl font-bold mb-1">模块概览</h2>
  <p class="text-xs text-slate-500 mb-3">按仓库实际设计拆分并列模块；笼统“其他”会展开为真实子模块。每项概览正文不超过 300 字；超出篇幅的实现与亮点可展开查看完整说明。问题陈述完整保留，重要问题集中列示，低风险局部问题在所属模块内说明；实现依据覆盖各子模块的代表位置。第三方库本身不计作作品缺陷或自研亮点，只评价项目适配代码及系统可见行为。</p>
  <div><article class="core-card" data-subsystem="内存" data-analysis-chars="4" data-detail-chars="0" data-issue-chars="218"><h3 class="font-bold mb-1">内存</h3><div class="text-sm leading-relaxed"><strong>静态实现：</strong><ul class="list-disc pl-4 mt-1 space-y-0.5"><li>管理页表</li></ul></div><div class="text-sm leading-relaxed"><strong>局部问题：</strong><ul class="list-disc pl-4 mt-1 space-y-0.5"><li>资源回收路径需要人工复核，资源回收路径需要人工复核，资源回收路径需要人工复核，资源回收路径需要人工复核，资源回收路径需要人工复核，资源回收路径需要人工复核，资源回收路径需要人工复核，资源回收路径需要人工复核，资源回收路径需要人工复核，资源回收路径需要人工复核，资源回收路径需要人工复核，资源回收路径需要人工复核，资源回收路径需要人工复核，资源回收路径需要人工复核，资源回收路径需要人工复核，资源回收路径需要人工复核，此问题只影响调试配置 <span class="text-xs text-slate-400">（<a class="file-jump" href="#kernel/mm.c:50">kernel/mm.c:50</a>）</span></li></ul></div><div class="text-xs text-slate-500 mt-2"><strong>实现依据：</strong><ul class="inline" data-evidence-count="1"><li class="inline mr-3"><a class="file-jump" href="#kernel/mm.c:1">kernel/mm.c:1</a></li></ul></div></article></div>
</section>

```


## prompt_samples_all_three_directories


输入：
```text
{'a': 100, 'b': 2, 'c': 2}
```


修复前：
```html
["a"]
```


修复后：
```html
["a", "b", "c"]
```
