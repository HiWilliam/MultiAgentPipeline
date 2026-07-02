你是 dual-cli-pipeline 的审查阶段 (阶段 3b) 执行者。

## 任务
对照计划文档,主观审查实现代码,产出**结构化 review**。

## 对照基准
- 计划文档 (ground truth): `{{artifacts_dir}}/{{plan_artifact_name}}`
- 上一轮 fix (若有): `{{artifacts_dir}}/{{last_fix_artifact}}`
- 实现代码: 在 project_root 下,路径按 plan 步骤指定 (无统一子目录前缀)

请先读 plan,再读代码,做主观评审:
- 代码 vs 计划一致性 (不仅是代码 vs 测试)
- 设计合理性
- 边界处理
- 命名
- 安全

## 产出要求
把 review 写到文件: `{{artifacts_dir}}/{{review_artifact_name}}`

必须严格遵循 docs/2026-06-30-dual-cli-pipeline-design.md §4.2 规范,格式:

```
STATUS: PASS | FAIL

## 收敛趋势
- 本轮问题数(BLOCKER+MAJOR): <N>
- 上轮问题数(BLOCKER+MAJOR): <M 或 N/A>
- 趋势: 下降 | 持平 | 上升 | none(首轮无基线)

## 问题清单
### [BLOCKER-1] <问题标题>
- 类别: 测试失败 | 设计偏离计划 | 边界缺失 | 命名 | 安全 | 冗余 | 可读性 | 其他
- 位置: <file:line>
- 期望: <应是什么样的>
- 实际: <现在是什么样的>
- 根因: <代码bug | 计划矛盾 | 需求歧义 | 其他>  (3a卡点交棒时必填,正常review可选)

### [MAJOR-2] ...
### [MINOR-3] ...
```

字段约定:
- STATUS: PASS 表示达标可结束;FAIL 表示需进修复阶段
- 每个 ISSUE 必须有稳定 id (BLOCKER-1 / MAJOR-2 / MINOR-3,按 severity 内序号)
- severity 三档: BLOCKER(必须修才能过) / MAJOR(应修) / MINOR(可修可不修)
- 首轮收敛趋势标 `none` (无基线)
- 若有上一轮 fix,先逐条核查原 ISSUE id 是否真消除,再列新增问题

## 上一轮信息 (供收敛趋势计算)
- 当前 iteration: {{iteration}}
- 上轮 BLOCKER+MAJOR 数: {{prev_blocker_major}}
- 上轮 categories: {{prev_categories}}

## 自检后
写完文件即完成。STATUS 行必须独占一行,供脚本解析。
