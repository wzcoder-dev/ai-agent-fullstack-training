"""生成精简学习手册：只展示设计、带中文注释的核心代码和手动复测。"""
from __future__ import annotations
import ast
from dataclasses import dataclass
import html
from pathlib import Path
import textwrap

P = Path(__file__).resolve().parents[1]


def esc(value):
    return html.escape(str(value))


@dataclass
class Excerpt:
    file: str
    line: int
    text: str


def extract(file, symbol=None, start=None, stop=None):
    """所有摘录取自当前实现；标记失效时直接报错，避免继续输出过时示例。"""
    lines = (P / file).read_text().splitlines()
    offset = 0
    if symbol:
        nodes = ast.parse('\n'.join(lines)).body
        node = None
        for name in symbol.split('.'):
            node = next(n for n in nodes if getattr(n, 'name', None) == name)
            nodes = getattr(node, 'body', [])
        offset = node.lineno - 1
        lines = lines[offset:node.end_lineno]
    if start:
        i = next(i for i,line in enumerate(lines) if start in line)
        offset += i
        lines = lines[i:]
    if stop:
        lines = lines[:next(i for i,line in enumerate(lines) if stop in line)]
    return Excerpt(file, offset+1, textwrap.dedent('\n'.join(lines)).strip())


def annotate(excerpt, notes):
    """只插入讲解注释，不改代码；缩进随被注释语句，便于和源码逐行对照。"""
    output = []
    seen = set()
    for line in excerpt.text.splitlines():
        for marker, comments in notes:
            if marker in line and marker not in seen:
                indent = line[:len(line)-len(line.lstrip())]
                output.extend(indent+'# '+comment for comment in comments)
                seen.add(marker)
        output.append(line)
    missing = {m for m,_ in notes} - seen
    if missing:
        raise ValueError(f'注释锚点不存在：{excerpt.file}: {missing}')
    return Excerpt(excerpt.file, excerpt.line, '\n'.join(output))


def block(excerpt, notes=()):
    item = annotate(excerpt, notes)
    # 讲解只能增加注释；AST 相同才能证明没有把核心代码悄悄改成另一套示例。
    assert ast.dump(ast.parse(excerpt.text)) == ast.dump(ast.parse(item.text))
    return f'<div class="code"><div class="code-head"><span>{esc(item.file)}:{item.line}</span><button type="button" class="copy">复制摘录</button></div><pre><code>{esc(item.text)}</code></pre></div>'


def shell(text):
    return f'<div class="command"><pre><code>{esc(textwrap.dedent(text).strip())}</code></pre><button type="button" class="copy">复制命令</button></div>'


def retest(command, expected):
    return '<div class="retest"><h3>手动复测</h3>'+shell(command)+f'<p><b>要核对：</b>{expected}</p></div>'


def design(id, number, title, goal, code, command, expected, extra=''):
    return f'<section id="{id}" class="design"><p class="number">设计 {number:02}</p><h2>{title}</h2><p class="goal">{goal}</p>{code}{extra}{retest(command,expected)}</section>'


def build():
    sections = []
    sections.append('''<header id="overview"><p class="eyebrow">第三周作业 · 给 C 开发者的 Python 阅读版</p><h1>9 个设计点，读懂 Guarded Harness</h1><p class="lead">模型提出动作，程序决定能否执行，并检查是否真正完成。</p><p>每节只读一件事：设计目的 → 带中文注释的核心代码 → 手动复测。源码标注精确到文件和起始行；摘录保留当前实现，局部变量来自所在方法，并非独立可运行程序。</p><p class="route">Agent 入口 → Gate → Sandbox → 状态 / 计划 → 存档恢复 → 完成验收</p></header>''')
    sections.append('''<section id="manual" class="setup"><h2>先跑通一次，再逐点阅读</h2><details><summary>展开启动命令和完整审批流程</summary><p>以下命令在同一个终端按顺序执行。<code>DEMO</code> 是本次练习目录，目录非空时换一个名字；这能避免覆盖旧运行。</p>'''+shell(f'''
        cd homework/week03/guarded-harness
        uv sync --locked
        MODEL=fake
        DEMO=runs/learn01
        ''')+'''<p>如果出现 <code>VIRTUAL_ENV ... does not match</code> 警告，说明旧课程环境仍被激活；先执行 <code>deactivate</code>。它通常不是命令失败。</p><p>① 创建示例并开始任务：</p>'''+shell('''
        uv run python -m guarded_harness.cli init-demo --workspace "$DEMO/repo"
        uv run python -m guarded_harness.cli run --workspace "$DEMO/repo" --run-dir "$DEMO/state" --model "$MODEL"
        ''')+'''<p>预期停在 <code>WAITING_APPROVAL</code>。Diff 是减法改加法，源码此时仍未修改。<code>fake</code> 是固定动作的测试模型，运行循环和工具执行仍是实际 smolagents。</p><p>② 先检查现场，再审批：</p>'''+shell('''
        uv run python -m guarded_harness.cli inspect --workspace "$DEMO/repo" --run-dir "$DEMO/state"
        # fake 修复示例为 call-4；真实模型请改成刚才输出中的 pending.id。
        CALL_ID=call-4
        uv run python -m guarded_harness.cli approve --workspace "$DEMO/repo" --run-dir "$DEMO/state" --call-id "$CALL_ID" --actor learner
        ''')+'''<p>审批命令只执行保存的写入请求，不自动继续后续模型步骤。</p><p>③ 用新进程继续：</p>'''+shell('''
        uv run python -m guarded_harness.cli resume --workspace "$DEMO/repo" --run-dir "$DEMO/state" --model "$MODEL"
        ''')+'''<p>预期 <code>COMPLETED / VERIFIED</code>，当前版本测试为 <code>1 passed</code>。</p><aside id="live"><h3>换成 DeepSeek 复测</h3><p>已有 <code>DEEPSEEK_API_KEY</code> 时，先执行下面三行，再重复上面的 ①–③；审批使用实际调用 ID。默认 <code>deepseek-flash</code>，会发起真实 API 请求。</p>'''+shell('''
        uv sync --locked --extra live
        MODEL=deepseek
        DEMO=runs/learn-deepseek01
        ''')+'''</aside><p>只读状态用 <code>status</code>，先检查恢复条件用 <code>inspect</code>。旧 v1 存档仅作历史保留，本版练习用新目录。</p></details></section>''')

    code = block(extract('guarded_harness/harness.py','GuardedAgent.execute_tool_call'), [
        ('def execute_tool_call', ['self 是当前对象；obj.method(x) 会自动把 obj 作为 self 传入。', '可类比 C 的 method(struct Agent *self, ...)，但对象和字典都由 Python 管理内存。', '这个方法属于继承 ToolCallingAgent 的 GuardedAgent；这里只接管工具入口，不重写框架循环。']),
        ('result =', ['self.h 是宿主 Harness，gate 是统一门禁对象；点号表示取对象属性。', '框架解析完工具名和参数后，实际执行必须先经过 dispatch。']),
        ("if tool_name ==", ['dict.get("ok") 取字段；字段不存在时返回 None。not 会把空值/False 判为真。', '特别处理 final_answer：返回一个“拒绝字典”仍可能被框架视为结束，必须抛框架异常。']),
        ('raise AgentToolExecutionError', ['raise 会沿调用栈退出，交给框架捕获；框架得到失败原因后可继续下一轮。', '不要把它当成 C 的单纯错误码返回：调用者必须有对应的异常处理路径。']),
    ])
    sections.append(design('entry',1,'保留 Agent 循环，收住工具入口',
        'smolagents 负责“请求模型—解析工具—回传结果”的循环；我们的代码负责执行许可。完成声明也必须走同一个入口。',code,
        'uv run pytest -q tests/test_harness_loop.py tests/test_completion.py -k "real_smolagents or h1"',
        '模型可以多轮调用工具；没有交付物时，即使模型声明完成，也不能进入完成状态。'))

    code = block(extract('guarded_harness/gate.py','Gate.dispatch',start='if not isinstance(args, dict)',stop='h.enforce_budget()'), [
        ('if not isinstance', ['args 是 dict，类似字符串键到值的映射；不是字段固定的 C struct。', 'isinstance 检查对象类型；or 会短路，因此 args 不是 dict 时不会继续访问字典。', 'set(args) 得到所有键的集合，比较集合可同时发现缺少字段和多余字段。']),
        ('type(args[k])', ['for k, typ in ...items() 逐一取“字段名、要求的类型”。', 'any(...) 相当于 C 的循环：只要发现一个类型不对就返回 true。', '这里严格比较 type，不把 Python 的 bool 当成 int 一类兼容值混过去。']),
        ('return self.reject', ['拒绝时立即返回，handler 不会被调用；类型注解本身不会做这种运行时校验。']),
    ])
    code += block(extract('guarded_harness/gate.py','Gate.dispatch',start='signature = digest([tool, args])',stop='resumed ='), [
        ('signature =', ['同样的工具名和参数产生同样的签名；后面用它绑定审批。']),
        ("old =", ['ledger 是已执行调用账本；call_id 是这一次调用的身份。']),
        ('if old:', ['同一个调用 ID 已有记录，就拒绝再次消费，避免重复写入。']),
    ])
    code += block(extract('guarded_harness/gate.py','Gate.dispatch',start="approval = s['approvals'].get(call_id)",stop="if tool == 'final_answer':"), [
        ('approval =', ['s 是当前业务状态字典；revision 是当前源码内容的哈希，不是模型生成的版本号。']),
        ('if not approval', ['批准必须同时匹配“这个动作”和“这版代码”；换参数或换源码都不能复用旧批准。']),
        ("s['pending']", ['保存待执行的完整请求。批准后执行这里的请求，不让模型重新生成一份替换它。']),
        ('raise Pause()', ['先存档再退出当前运行。Pause 继承 BaseException，用于暂停控制流。', '这样不会被框架的 except Exception 误当成普通工具错误而自动重试。']),
    ])
    sections.append(design('gate',2,'门禁在执行前判断，审批精确绑定动作',
        '参数、调用去重、任务权限和审批都在 handler 前检查。相同工具与参数连续第三次被拦截；同一已执行调用不能重复消费。',code,
        'uv run pytest -q tests/test_gate.py',
        '参数错误时执行次数为零；未审批文件不变；审批后只写一次；篡改参数不能借用旧审批。'))

    code = block(extract('guarded_harness/sandbox.py','Sandbox.path'), [
        ('rel = Path(name)', ['Path 是路径对象，封装了路径拼接与解析；不是直接拼字符串。']),
        ('if rel.is_absolute()', ['只接受工作区内相对路径；rel.parts 把目录逐段拆开，检查 .. 逃逸。']),
        ('if any(p in SENSITIVE', ['逐个路径段检查敏感名，.env 及其变体也禁止通过工具访问。']),
        ('candidate =', ['Path 的 / 运算符用于拼接路径，这里的 / 不是数值除法。']),
        ('for part in', ['*candidate.parents 会把各级父路径展开到列表，类似把数组元素逐个追加进去。', '检查每一级软链接；否则“工作区内文件名”可能指向工作区外。']),
        ('if not candidate.resolve()', ['resolve 解析真实落点，再确认它仍在 root 下；cwd 不是文件访问权限边界。']),
        ('if write and', ['写入只放行 Python 源文件与 artifacts 下的 Markdown；测试禁写由外层 Gate 再限制。']),
    ])
    code += block(extract('guarded_harness/sandbox.py','Sandbox.run_test',start="if command != 'pytest -q':",stop='self.last_pid ='), [
        ('if command !=', ['完整匹配固定命令，拒绝模型追加其他命令或参数。']),
        ('env =', ['只把列出的环境变量交给子进程，不继承宿主的 API Key。']),
        ('process =', ['Popen 的第一个参数是 argv 列表，类似 C 的 execve(argv)。', '未启用 shell；分号、管道不会被当作 Shell 运算符解释。']),
        ('cwd=self.root', ['cwd 只设置启动目录；start_new_session 建立独立进程组，便于超时/取消时整组终止。']),
    ])
    code += block(extract('guarded_harness/sandbox.py','Sandbox.run_test',start='            try:',stop='reader.join('), [
        ('os.killpg', ['这是 finally 清理分支的局部摘录：超时/取消后终止整个进程组。', 'finally 类似 C 的统一 cleanup 路径，抛异常时也会执行。']),
        ('except ProcessLookupError', ['进程组已经消失是可接受的清理结果，因此忽略这个特定异常。']),
        ('process.wait()', ['等待直接子进程退出并回收；不是仅停止等待而放任它后台继续跑。']),
    ])
    sections.append(design('sandbox',3,'限制文件、命令、进程生命周期',
        '读/写工具限制路径，测试工具限制命令。默认测试超时 10 秒、输出保留 12,000 字节，单次写入最多 100,000 字节。',code,
        'uv run pytest -q tests/test_sandbox.py',
        '路径逃逸、敏感路径和软链接被拒；危险命令不执行；超时或取消后进程终止。',
        '<p class="boundary"><b>能力边界：</b>当前是本机应用层 Sandbox。pytest 内的 Python 代码仍拥有当前用户权限，可直接读宿主文件或联网；它不等于容器或虚拟机，只用于可信仓库。</p>'))

    code = block(extract('guarded_harness/state_store.py','StateStore.transition'), [
        ('def transition', ['to 是目标状态，event 是触发事件。reason=None 表示省略时没有附加原因。']),
        ('state = self.data', ['这里没有复制字典：state 和 self.data 指向同一个可变对象。', '可类比 C 的两个指针指向同一个 struct；通过 state 修改会直接改变业务状态。']),
        ('if to not in', ['ALLOWED 是“旧状态 → 允许目标状态集合”的表，集中约束状态迁移。', '.get(old, set()) 表示键不存在时使用空集合；终态不能随意跳回 RUNNING。']),
        ("state['transitions'].append", ['append 往历史列表尾部加一条记录；dict(...) 创建一条映射记录。', '恢复时历史列表也会恢复，len + 1 因而接着原序号往下记。']),
        ('state.update', ['update 原地修改指定字段，不会替换其他业务字段。']),
        ("return state['transitions'][-1]", ['[-1] 取最后一个元素；在 C 中通常要写 array[length - 1]。']),
    ])
    sections.append(design('state',4,'业务状态独立于模型对话',
        '对话说明模型说过什么；业务状态记录运行到哪一步。RUNNING、WAITING_APPROVAL、UNKNOWN_RESULT 等状态通过统一入口迁移。',code,
        'uv run pytest -q tests/test_course_alignment.py -k r3',
        '停止后的运行不能强行回到 RUNNING；换进程后迁移历史的序号连续。'))

    code = block(extract('guarded_harness/plan_store.py','PlanStore.begin'), [
        ('missing =', ['depends_on 存前置步骤 ID；dependencies 返回尚未完成的依赖。']),
        ('if missing:', ['非空列表在 if 中为真，类似 C 中判断 missing_count != 0。']),
        ('step =', ['get 返回计划里原有步骤字典的引用；下面修改的是实际计划，而非临时副本。']),
        ("step['attempts'] +=", ['记录尝试次数，与工具是否成功是两回事；恢复后也保留。']),
        ("if id != 'inspect'", ['已经建立的源码阅读事实不因再次阅读而清零；验证步骤每次重跑则重新进入执行中。']),
    ])
    code += block(extract('guarded_harness/plan_store.py','PlanStore.observe'), [
        ('def observe', ['开始步骤和完成步骤分开：只有执行结束后，才能按真实结果更新状态。']),
        ("if evidence and", ['evidence 是程序产生的结果记录，id 关联到具体工具调用；模型不能自行宣称有记录。']),
        ('step.update', ['A if condition else B 是 Python 条件表达式，对应 C 的 condition ? A : B。', '完成必须由 success 决定；异常和失败也要留下状态与错误信息。']),
    ])
    sections.append(design('plan',5,'把计划变成有依赖、可验收的步骤',
        '修复计划是 inspect → change → verify → finalize。每步存目标、依赖、状态、尝试次数与结果记录 ID；改完源码后，旧验证步骤退回待验证。',code,
        'uv run pytest -q tests/test_course_alignment.py -k r4',
        '未读源码不能修改；只读测试文件不算理解源码；修改后必须重新验证；无关读取失败不抹掉已有阅读事实。'))

    # 非连续摘录只保留事务的关键顺序；每条语句均来自 Gate.dispatch。
    intent = extract('guarded_harness/gate.py','Gate.dispatch',start="s['ledger'][call_id] = dict",stop='try:')
    finish = extract('guarded_harness/gate.py','Gate.dispatch',start="s['ledger'][call_id].update(status='DONE'",stop='return result')
    intent = Excerpt(intent.file,intent.line,intent.text+'\n\n# 中间由 h.handle(...) 执行真实副作用；异常处理省略。\n\n'+finish.text)
    code = block(intent, [
        ("s['ledger'][call_id] =", ['先记录“准备执行”，再存盘。此时还不能记为 DONE。']),
        ('h.checkpoint.save()', ['第一次 save 必须早于文件写入；否则崩溃后连“曾开始过”都不知道。']),
        ("s['ledger'][call_id].update", ['只有实际返回结果后，才把调用闭合为 DONE；随后再次保存。', '若崩溃发生在两次保存之间，恢复只能认定结果未知，不能猜测没执行。']),
    ])
    code += block(extract('guarded_harness/state_store.py','atomic_json',start="with temp.open('w') as f:",stop='fd = os.open'), [
        ("with temp.open", ['with 管理资源的进入和退出，离开代码块会关闭文件，异常时也关闭。', '可类比 C 的 fopen 后统一走 fclose 清理；不用在每个 return 分支手写关闭。']),
        ('json.dump', ['把内存中的字典序列化成 JSON；它不是把对象内存直接写盘。']),
        ('f.flush()', ['先刷 Python 的用户态文件缓冲，再调用 fsync 刷持久化数据。']),
        ('os.replace', ['临时文件完整写好后原子替换正式存档；完整实现还会 fsync 父目录。']),
    ])
    code += block(extract('guarded_harness/checkpoint.py','Checkpoint.load',start='report = self.inspect()',stop="elif report['code'] == 'WORKSPACE_CHANGED':"), [
        ('report =', ['恢复的第一步是只读检查：版本、校验和、结构、工作区以及未闭合调用。']),
        ("if not report['valid']", ['检查不通过就抛异常，保留原件；不能带着损坏状态启动模型。']),
        ("if report['code']", ['EXECUTING 只说明曾经开始；文件可能已改，也可能没改。']),
        ("call['status'] =", ['标记 UNKNOWN_RESULT，清理审批请求并停下等人工核对。', '这和“正常暂停可继续”是不同的恢复结论。']),
    ])
    sections.append(design('checkpoint',6,'执行前后存档，先检查再恢复',
        '关键不是恢复聊天，而是确认副作用是否发生。正常闭合的调用不重放；有开始、无结果的调用进入 UNKNOWN_RESULT。inspect/status 只读，不改现场。',code,
        'uv run pytest -q tests/test_state_checkpoint.py tests/test_course_alignment.py -k "c2 or c3 or c4 or r6"',
        '写入后崩溃不重复写；工作区变化使旧审批失效；校验和损坏拒绝恢复；inspect/status 不改变文件或修改时间。'))

    code = block(extract('guarded_harness/completion.py','latest_test'), [
        ('return next', ['这是生成器表达式，不是立即建立新列表；每次只取一个符合条件的元素。', 'reversed 从尾部往前遍历；next(..., None) 取第一个匹配项，没有就返回 None。', '可类比 C 的逆序 for：找到同一 scope 的最近记录后 break。', '必须先找最新尝试，再判断成功；不能先筛“通过项”，否则会漏掉更晚的失败。']),
    ])
    code += block(extract('guarded_harness/completion.py','test_issue'), [
        ('if not latest:', ['没有记录先返回缺项；不能把“未跑测试”当作默认成功。']),
        ("if latest.get('revision')", ['内容哈希要和当前版本相同；测试后又改代码，旧通过立即失效。']),
        ("if latest.get('reason')", ['reason 包括超时、取消和结果未知；退出码非零也不能交付。']),
        ("if latest.get('stable')", ['即使退出码为 0，测试过程中输入变化也不能作为有效通过。']),
        ('return None', ['None 在这里约定为“没有这项缺失”，对应 C 常见的 NULL 表示无错误对象。']),
    ])
    code += block(extract('guarded_harness/completion.py','contract_for'), [
        ('return RepairContract', ['按任务类型选择验收对象，类似 C 中选择不同的验证函数表。', '修复验收还检查真实源码差异、测试未被改动与计划步骤；理解验收要求固定报告路径、非空内容和哈希匹配。']),
    ])
    sections.append(design('course',7,'由完成契约验收，最后一次测试说了算',
        '模型调用 final_answer 只是在申请结束。程序分别检查修复或理解任务的全部缺项，把具体原因反馈给模型继续补齐。',code,
        'uv run pytest -q tests/test_completion.py tests/test_course_alignment.py -k "h1 or h2 or r1 or r2"',
        '先通过、后失败/取消/未知必须拒绝；其他 Markdown 不能替代 artifacts/report.md；源码改后需重新测试。'))

    code = block(extract('guarded_harness/harness.py','Harness.enforce_budget'), [
        ('self.sync_elapsed()', ['先更新有效运行耗时；使用 monotonic 时间差，不受系统时钟校准影响。', '恢复时以已保存耗时为基数；等待下一次命令的暂停时间不计入。']),
        ("s = self.store.data", ['s 是同一个业务字典的引用，tokens_used 和 elapsed_seconds 都会写入存档。']),
        ("if s['tokens_used']", ['检查的是累计消耗，不是本轮消耗。用尽后 stop 会保存原因并抛暂停信号。']),
    ])
    code += block(extract('guarded_harness/context_budget.py','render_context',start='for keep in range(',stop='summaries ='), [
        ('for keep in', ['range(start, -1, -1) 对应 C 的 for (keep=start; keep>=0; --keep)。', '先尝试保留指定数量的最近步骤，装不下时再减少；不从一条消息中间截断。']),
        ('old =', ['列表切片 [start:end] 不包含 end；groups 是按工具交互分组的历史步骤。', '这里 keep=0 单独处理，避免 Python 的 -0 切片产生意外含义。']),
        ('recent =', ['old 生成规则摘要，recent 保留完整内容；system 和任务指令另行保留。', '之后若连必须保留的指令也超出字符预算，就以 CONTEXT_BUDGET 停止。']),
    ])
    code += block(extract('guarded_harness/context_budget.py','render_context',start='after = size(candidate)',stop="raise ValueError('CONTEXT_BUDGET"), [
        ('after =', ['candidate 已由前面的代码组成为：必须保留的指令 + 旧步骤摘要 + 近期完整步骤。', 'size 计算消息序列化后的字符数；它是上下文边界，不是精确 token 数。']),
        ('if after <=', ['装得下才交给模型；如果这个方案装不下，外层 for 会减少近期步骤数量再试。']),
        ('return candidate', ['返回二元组 (messages, stats)，调用方可用 messages, stats = ... 同时接收。', '相当于 C 中通过返回结构体带回多个结果；摘要方式是固定规则，不额外请求模型。']),
    ])
    code += block(extract('guarded_harness/models.py','MeteredModel.generate',start='usage = message.token_usage',stop="self.h.audit.record('MODEL_RESPONSE'"), [
        ('usage =', ['token 用量由模型服务的响应返回，字典/对象字段不是模型自己口述的数字。']),
        ('if usage is None', ['真实模型没有用量信息就停止；只有离线测试模型允许没有用量。']),
        ("s['tokens_used'] +=", ['getattr(obj, name, default) 安全读取属性；or 0 把 None 归为零。', '这里累计输入与输出 token。一次已发出的请求可能超额，但返回后不能继续执行工具。']),
    ])
    sections.append(design('budget',8,'给运行和上下文设预算',
        '默认 50,000 token、有效运行 300 秒、消息上下文 24,000 字符。完整运行事实仍留在本地；发给模型的旧步骤按规则压缩，不额外调用摘要模型。',code,
        'uv run pytest -q tests/test_harness_loop.py tests/test_course_alignment.py -k "h3 or r5"',
        '换进程后预算不清零；压缩结果确定且不改原始消息；系统指令装不下就停止。Token 在请求边界检查，不是费用预扣硬上限。'))

    code = block(extract('guarded_harness/audit.py','Audit.record'), [
        ('def record', ['**details 接收其余命名参数，组成一个 dict；它不是 C 可变参数的无类型内存读取。']),
        ('item = dict', ['run_id 关联整个任务；tool_call_id 关联某次动作；actor 标明谁作出决定。']),
        ('previous=self.previous', ['把上一条哈希写进本条，形成前后关联。字典里的 **details 表示合并额外键值。']),
        ("item['hash']", ['digest 对结构化内容算 SHA-256。先算当前条目，再把 hash 加进去，避免自引用。']),
        ("with self.path.open('a')", ["'a' 是追加模式，类似 C 的 fopen(path, \"a\")；不会覆盖之前的行。"]),
        ('f.write', ['每行一个 JSON 对象，行与行独立，便于按调用 ID 查询。']),
        ('self.previous =', ['保存当前哈希，下一次 record 将把它作为 previous 使用。']),
    ])
    sections.append(design('audit',9,'状态用于恢复，审计用于解释原因',
        'Checkpoint 保存当前可恢复现场；Audit 追加记录曾发生的批准、拒绝和执行结果。二者各做一件事。哈希链能发现已有行被改写，但没有外部锚点时不能阻止整条链被重写。',code,
        'uv run pytest -q tests/test_audit.py',
        '拒绝也有记录；能沿调用 ID 关联审批与执行；修改已有行后哈希链校验失败。'))

    answers = [
        ('能指出模型决策和程序执行的边界，解释为什么不用重写 Agent 循环。',
         '模型负责提出下一步动作，程序负责校验、审批、执行、记录和验收。smolagents 已有多轮循环，我们只在工具入口和完成检查处增加约束。本作业继承 ToolCallingAgent，模型返回结构化工具调用；生成并执行 Python 代码的是另一种 CodeAgent。'),
        ('能解释参数、重复调用和写入审批为什么必须在执行前检查。',
         '文件一旦写入，事后拒绝并不能撤销副作用，因此门禁必须先于 handler。参数检查保证调用符合契约；同一调用 ID 不重复消费，相同动作连续第三次被拦截；写入审批绑定参数签名和当前源码版本，防止改参数或换代码后复用旧批准。'),
        ('能区分路径/命令限制与操作系统隔离，知道当前 Sandbox 的能力边界。',
         '当前 Sandbox 校验工具路径、限制固定测试命令、清理子进程环境，并管理超时、输出和进程组。这些只约束受控执行入口：pytest 内的 Python 代码仍有当前用户权限，能绕过工具直接访问宿主文件或联网。运行不可信仓库需要另加容器/虚拟机以及文件、网络和资源隔离。'),
        ('能沿状态迁移历史说明一次任务为什么暂停或停止。',
         '按序号查看 from_state → event → to_state，再看 reason 与 stop_reason。WAITING_APPROVAL 表示等待决定，不是失败；UNKNOWN_RESULT 表示执行结果待核对；NEEDS_REVIEW 表示现场变化使旧依据失效；预算耗尽则明确 STOPPED。只有完成契约通过，才是 COMPLETED，而不是“循环结束就算成功”。'),
        ('能区分“工具返回成功”“步骤完成”“任务完成”。',
         '工具成功只说明一次调用得到预期返回；步骤完成还要求前置依赖和该步骤的真实结果满足标准；任务完成要求所有必要步骤及整体完成契约通过。例如“文件写成功”不等于“修复完成”：还要核对当前源码差异、未被篡改的测试，以及最后一次有效测试通过。理解任务则需要当前源码阅读和约定报告。'),
        ('能解释写入结果未知为什么不能自动重试，且能使用 inspect 判断现场。',
         '执行顺序是存 EXECUTING → 执行副作用 → 存 DONE；两次存档之间崩溃，文件可能已改，直接重试会重复副作用。inspect 只读检查存档结构/校验和、工作区变化和未闭合调用：READY 可恢复，WAITING_APPROVAL 先审批；UNKNOWN_RESULT 或 WORKSPACE_CHANGED 先人工核对，本实现核对后另建 run，不自动重放未知写入。'),
        ('能判断一条测试记录是否是当前版本的最后一次有效通过。',
         '先取同一测试范围的最后一次尝试，再检查它是否通过，不能先筛选成功记录。有效条件是退出码为 0、没有超时/取消/未知原因、测试前后输入稳定，且记录指纹与当前源码、测试及相关配置一致。后来的失败会挡住旧通过；源码再变更后必须重测。'),
        ('能说明累计预算如何跨进程保留，以及上下文压缩保留了什么。',
         '轮数、调用数、累计 token 和有效运行耗时写入 Checkpoint，恢复时延续计数，等待人工的暂停时间不累计。上下文保留系统/任务指令和近期完整工具交互，旧步骤用确定性摘要替代，完整事实仍留在本地；指令装不下就停止。token 在请求边界检查，已发出的单次请求可能超额，因此不是账单硬上限。'),
        ('能沿调用 ID 找到批准、拒绝和执行的关联，而不是只相信模型自述。',
         '用 run_id 找整次运行，再用 tool_call_id 串起 WAIT/DENY、批准者 actor 的 ALLOW，以及执行 START/DONE 和结果。同一次动作有批准记录，不代表已经落盘；还要核对实际文件、内容哈希和当前测试。模型说“已修好”只是声明，不能替代程序产生的执行记录和完成验收。'),
    ]
    sections.append('<section id="checklist" class="mastery"><h2>学完应该掌握的 9 个判断</h2><p class="answer-intro">参考答案按课程 3-1 的循环分工、3-3 的状态与恢复、3-5 的完成契约和 3-7 的预算治理整理，并注明本作业的实际边界。</p><ol class="answers">'
                    + ''.join('<li><h3>'+esc(question)+'</h3><p>'+esc(answer)+'</p></li>' for question,answer in answers)
                    + '</ol><p>最后独立做一次：创建示例 → 运行到审批 → 检查 Diff → 批准 → 换进程继续 → 确认当前测试通过。整套自动化回归可执行：</p>'
                    + shell('uv run pytest -q') + '</section>')
    navigation = [('overview','阅读主线'),('manual','启动与完整流程'),('entry','01 · 框架入口'),('gate','02 · 门禁与审批'),('sandbox','03 · 执行边界'),('state','04 · 业务状态'),('plan','05 · 计划步骤'),('checkpoint','06 · 存档恢复'),('course','07 · 完成验收'),('budget','08 · 运行预算'),('audit','09 · 审计关联'),('checklist','最后应掌握')]
    page = TEMPLATE.replace('__NAV__',''.join(f'<a href="#{id}">{esc(title)}</a>' for id,title in navigation)).replace('__BODY__',''.join(sections))
    target=P/'learning-guide.html'
    target.write_text(page)
    print(target)


TEMPLATE = '''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Guarded Harness · 9 个设计点</title><link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='5' fill='%231c5748'/%3E%3Ctext x='6' y='23' fill='%23fff' font-size='20'%3EH%3C/text%3E%3C/svg%3E"><style>
:root{--bg:#f3f6f1;--surface:#fff;--soft:#e7eee7;--border:#cad6ca;--text:#21372d;--text-dim:#53685b;--accent:#1c5748;--amber:#94621c;--code:#102a21;--code-text:#e2efe4;--body:'PingFang SC','Microsoft YaHei',sans-serif;--mono:'SFMono-Regular',Menlo,monospace}
@media(prefers-color-scheme:dark){:root{--bg:#101d18;--surface:#182820;--soft:#23392c;--border:#385144;--text:#e0eade;--text-dim:#a6bcad;--accent:#90c4a8;--amber:#d9b377;--code:#0b1712;--code-text:#d5e9db}}
*{box-sizing:border-box}html{font-size:17px;scroll-behavior:smooth}body{margin:0;background:var(--bg);color:var(--text);font-family:var(--body);line-height:1.8}.wrap{max-width:1320px;display:grid;grid-template-columns:205px minmax(0,1fr);gap:3rem;margin:auto;padding:2rem}.toc{position:sticky;top:2rem;align-self:start;max-height:calc(100dvh - 4rem);overflow:auto}.toc-label{font: .75rem var(--mono);letter-spacing:.08rem;color:var(--text-dim);padding-bottom:1rem;border-bottom:1px solid var(--border);margin-bottom:.6rem}.toc a{display:block;padding:.45rem .6rem;color:var(--text-dim);font-size:.85rem;text-decoration:none;border-left:2px solid transparent}.toc a.active,.toc a:hover{background:var(--soft);color:var(--accent);border-left-color:var(--accent)}main{min-width:0}header,section{padding:1.6rem 0 2.8rem;border-bottom:1px solid var(--border);scroll-margin-top:1.3rem}h1,h2,h3{line-height:1.4;text-wrap:balance}h1{font-size:2.5rem;letter-spacing:-.06rem;margin:.8rem 0 1.2rem}h2{font-size:1.7rem;margin:.6rem 0 1rem}h3{font-size:1rem;margin:.6rem 0}.eyebrow,.number{font:.76rem var(--mono);color:var(--text-dim);letter-spacing:.08rem}.lead{font-size:1.18rem;color:var(--accent)}p{max-width:70ch;overflow-wrap:anywhere}.goal{font-size:1.02rem}.route{padding:.8rem 1rem;background:var(--soft);font-size:.9rem}.code{background:var(--code);color:var(--code-text);margin:1.1rem 0 1.6rem;border:1px solid var(--border);min-width:0}.code-head{display:flex;justify-content:space-between;gap:.8rem;align-items:center;padding:.6rem .8rem;border-bottom:1px solid #42624e;font:.73rem var(--mono)}.code-head span{overflow-wrap:anywhere;min-width:0}pre{padding:1rem 1.2rem;margin:0;overflow:auto;font-family:var(--mono);font-size:.82rem;line-height:1.85;tab-size:4}code{font-family:var(--mono);font-size:.84rem}.code code,.command code{font-size:inherit}.comment{color:#a9ccb3}.numbered-code{counter-reset:line}.retest{padding:1rem 1.2rem;background:var(--soft);margin-top:1.6rem}.retest p{font-size:.92rem;margin:.7rem 0 0}.command{position:relative;background:var(--surface);border:1px solid var(--border);color:var(--text);margin:.8rem 0;min-width:0}.command pre{padding-right:6rem}.command button{position:absolute;top:.4rem;right:.4rem}.copy{font-family:var(--body);font-size:.72rem;background:transparent;border:1px solid currentColor;color:inherit;padding:.25rem .5rem;border-radius:.2rem;cursor:pointer;flex-shrink:0}.copy:hover{opacity:.8}a{color:var(--accent)}button:focus-visible,summary:focus-visible,a:focus-visible{outline:3px solid var(--amber);outline-offset:3px}details{background:var(--surface);border:1px solid var(--border);padding:1rem 1.2rem}summary{font-weight:600;cursor:pointer}.boundary{padding-left:1rem;border-left:3px solid var(--amber);font-size:.94rem}aside{border-top:1px solid var(--border);margin-top:1.2rem;padding-top:1rem}ol{padding-left:1.5rem}li{padding:.3rem 0;max-width:70ch}.setup p{font-size:.94rem}.answers li{padding:.9rem 0;border-bottom:1px solid var(--border)}.answers li:last-child{border-bottom:0}.answers h3{margin:.15rem 0 .5rem;font-size:1rem}.answers p{margin:0;font-size:.95rem;color:var(--text-dim)}.answer-intro{font-size:.9rem;color:var(--text-dim)}
@media(max-width:950px){.wrap{display:block;padding:0 1rem}.toc{display:flex;gap:.3rem;top:0;max-height:none;z-index:10;overflow-x:auto;background:var(--bg);margin:0 -1rem;padding:.6rem 1rem;border-bottom:1px solid var(--border)}.toc-label{display:none}.toc a{white-space:nowrap;flex-shrink:0;border-left:0;border-bottom:2px solid transparent;font-size:.78rem}.toc a.active{border-bottom-color:var(--accent)}section,header{scroll-margin-top:4rem}h1{font-size:2rem}.code-head{font-size:.7rem}.code pre{padding:.8rem}h2{font-size:1.45rem}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}@media print{.toc,.copy{display:none}.wrap{display:block;padding:0}.code{color:#111;background:#f4f4f4}pre{white-space:pre-wrap;overflow:visible}.design{break-before:page}.code-head{color:#111}}
</style></head><body><div class="wrap"><nav class="toc" aria-label="学习目录"><div class="toc-label">GUARDED HARNESS<br>设计 · 代码 · 复测</div>__NAV__</nav><main>__BODY__</main></div><script>
document.querySelectorAll('.copy').forEach(button=>button.addEventListener('click',async()=>{const code=button.closest('.code,.command').querySelector('code');try{await navigator.clipboard.writeText(code.textContent);button.textContent='已复制'}catch(e){const range=document.createRange();range.selectNodeContents(code);const selection=getSelection();selection.removeAllRanges();selection.addRange(range);button.textContent='已选中，请复制'}setTimeout(()=>button.textContent=button.closest('.code')?'复制摘录':'复制命令',1600)}));
const links=[...document.querySelectorAll('.toc a')];const observer=new IntersectionObserver(entries=>entries.forEach(entry=>{if(entry.isIntersecting)links.forEach(a=>a.classList.toggle('active',a.hash==='#'+entry.target.id))}),{rootMargin:'-5% 0px -75% 0px'});document.querySelectorAll('header[id],section[id]').forEach(section=>observer.observe(section));
// 注释仅按整行着色，先转义 HTML 后再插入 span；复制时仍获得原始代码文本。
document.querySelectorAll('.code pre code').forEach(node=>{node.innerHTML=node.innerHTML.split('\\n').map(line=>line.trim().startsWith('#')?'<span class="comment">'+line+'</span>':line).join('\\n')});
</script></body></html>'''

if __name__ == '__main__':
    build()
