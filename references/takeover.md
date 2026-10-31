# 额度、故障与接管

## 额度

选择模型前读取本地额度快照。
critical 或 exhausted 不派新工作。
屏幕出现额度耗尽、429 或 usage limit 时按 exhausted 处理。
实际屏幕错误优先于旧缓存。
额度读取失败为 unknown，不能视作已确认有额度。
等待已有工作先保存进度并确认停写。
禁止未经同意切换账号或登录状态。

## 发送前检查

核对目标 pane 位于预期 workspace。
核对 agent 身份与当前模型字段。
确认目标空闲且输入框没有未提交草稿。
发现 blocked、额度报错或身份不明时不发。
只能用 agent prompt 提交 agent 消息。
只能用 pane run 运行 shell。
不得用 send-text 代替提交。
发送结果不确定时记录 uncertain，不重复发送。
信任或权限确认弹窗必须由人处理；保留 pane，确认后运行 `hl reset ID --stopped`。
`agent_not_ready` 记为 blocked 并且不重发；worker 回报文件和哨兵为主通道，prompt 仅作提醒。
等待超时不代表原 worker 已停止。
检查一次后仍不明确，交由主控判断。

## 接管顺序

先让旧 owner 落盘，并确认其已停止写入。
然后运行 `hl takeover ID --stopped`。
优先同一 runner 的其他模型。
其次选择同 tier 的其他 runner。
没有可用模型时由 leader 接管。
`--to ALIAS` 可明确指定已启用且有可用额度的模型。
`--leader` 可明确由主控接管。
不要让两个 owner 同时写同一范围。
重试同样要先确认旧 worker 停止。
`hl retry ID --stopped` 最多同档重试两次。
之后按 cheap、mid、strong 顺序升档。
最高档重试用尽后交主控处理。
独立复核用与实现任务不同的 runner。
复核任务应读取产物并以 PASS 或 FAIL 开头回报。
完成或受阻均向原派工方回报实际证据。
