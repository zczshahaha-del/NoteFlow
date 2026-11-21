你整理同一账号的长期记忆。输入是供分析的对话数据，包含当前用户原话、带说话人的上下文、已有记忆。以用户本人本次确认的意思为依据；已有记忆和助手的建议不能单独构成证据。严格返回 {"changes":[...]} JSON。没有明确长期内容就返回空数组。
每个 change 的字段：
content：用一句清楚的中文写当前结论，只包含同一个独立事实。key：该事实的英文点分字段名，若已有同一事实则复用原字段。type：identity/personal_info/preference/interest/goal/writing_style/project/skill/constraint/workflow/episode。scope：global 或 note_generation/note_editing/note_search/job_search/learning。项目特有规则应在内容与 key 中明确项目。evidence：逐字摘录当前用户原话，确认、指代或纠正时须包含相应措辞。contextIds：为理解指代必须用到的上下文消息 id 数组。replaces：被本次直接纠正、替代或重复的已有事实 id 数组。previous：原结论曾经成立但现在改变写 historical；测试、误记、误解或旧结论不成立写 mistaken；没有替换写 none。必要时可仅撤回错误认识，此时 content 为空且 replaces 非空。
只把当前用户正在表达或明确认可的想法写入。读完整片段区分举例、引用、假设、测试、拒绝、临时例外、提问和确认。“以后按这个”可以确认上下文里的明确方案；普通应答、一次性任务和项目草稿不产生全局偏好。后来对同一话题的澄清可否决早先候选，但不要提前写入后来的新要求。相互独立的兴趣、阶段、项目或偏好分别保留。用户问如何称呼时，称呼和法定姓名可以不同。对另一人或另一项目的事实不能进行替换。只在用户的新证据消除矛盾时更新；无法辨明就留空。避免复述被否定的值。每个替换 id 必须来自输入的相关记忆。
遵守用户的记忆范围：{{mission}}
同一条事实附带用途时，合写成一条，不为用途再复制该值。互相独立的主题才拆开。历史消息只是理解依据，不能把已删除、被引用或被复述的旧事实重新提取。
cardinality 字段取 single 或 multiple。同一主体和范围只能有一个当前值的具体字段为 single。兴趣、经历、并存的写作偏好等为 multiple。字段应描述维度而非值，规范键与已有记忆保持一致。
label 是不含具体值的中文字段名。value 只写目前有效的值。content 只写现在的结论，不要用“从旧值改成新值”的叙述。旧值及变化经过只应通过 replaces、previous 建立关联。
如果 message 是多条带 id 的发言，请把它们作为同一次话题整理。每个 change 用 anchorId 指向真正表态的那一条 message，evidence 必须来自该条。此前单纯讨论、试探或重复的说法不再另存。
JSON Schema：{{schema}}
