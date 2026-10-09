# macOS 每日岗位检查

这是现有 v5 的系统部署适配，不增加求职业务 Skill。默认每天**北京时间09:00**检查，分析固定 local。关闭 Terminal 不影响已启用的 LaunchAgent；本仓库不会在安装依赖、运行测试或准备配置时自动安装它。

## 先准备，再批准启用

在项目根目录执行，简历和状态目录使用你自己的已有路径：

```sh
.venv/bin/python -B -m job_agent.macos_schedule prepare \
  --resume /absolute/path/to/resume.pdf \
  --state-dir .agent-platform/my-existing-jobs \
  --output-dir .agent-platform/macos-daily
```

需要改变时间可加 `--daily-at 10:00`，仍按北京时间。来源默认使用 `job_agent/sources/public-careers.json`，广州优先、不足才上海、再不足才深圳；只有启用且经过核验的来源会访问。目前配置没有可用深圳来源，不能承诺深圳覆盖。候选上限8、每次新分析上限2；需要调整时编辑私人配置中的对应数值并重新核验。真实搜索验收可以单独使用广州-only；每日运行沿用完整城市策略。

准备步骤只在 `.agent-platform/macos-daily/` 生成：

- `config.json`：真实简历路径、已有状态目录、来源与运行边界；不保存简历全文或API Key。
- `local.ai-agent-lab.job-agent.plist`：绝对解释器路径、项目目录、系统触发配置。
- `enable.sh`、`disable.sh`：需要明确输入 `ENABLE` / `DISABLE` 才执行。
- `startup.log`：解释器/配置启动错误；正式业务输出不会直接写入系统日志。

目录权限700、配置和日志600，全部被Git忽略。再次准备使用新的输出目录，不覆盖已有配置。不要把私人plist、配置或日志放入公开仓库。

**安装和启用属于长期系统变更，须先取得用户明确同意。** 获准后本人执行：

```sh
sh .agent-platform/macos-daily/enable.sh
```

脚本将已核验的plist安装到当前用户的 `~/Library/LaunchAgents/` 并调用 `launchctl bootstrap`；不需要sudo，不创建系统级Daemon。已有同名任务时拒绝覆盖，plist被改成其他命令也拒绝安装。启用后不要移动项目、删除虚拟环境或搬走简历而不重新配置。

## 运行机制与恢复边界

`launchd` 每5分钟及登录加载时启动一个短进程；进程复用 `DailyUpdates / UpdateStore` 检查持久化到期点，只有到点才搜索。任务没有 `KeepAlive`，没有常驻watch，没有付费分析路径。北京时间由现有v5的UTC/+08:00计算，不依赖系统时区；09:00所在的系统触发点会检查到期状态，但系统负载可能造成延迟。

系统触发使用每5分钟的 `StartCalendarInterval`。按照 [Apple定时任务说明](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/ScheduledJobs.html)，休眠错过的日历事件在唤醒后合并触发。部署层的持久化到期点再次保证：错过多天也只补查一次，不连续补抓多天。不会主动唤醒Mac；关机期间不能运行，未登录时用户LaunchAgent不能运行。重启登录后会检查保留的到期点。

到期点在发起检查前持久化到下一天，部署锁与原v5检查锁避免重叠；重复运行不再搜索。同一天断网、失败或被中断后**不自动重试网络请求**，保留错误和已有岗位；下一天恢复检查，也可手动执行既有 `--check-now`。中断记录在下一次进程启动时标记interrupted，不把旧进程假称仍在运行。极小的“已领取到期点但尚未开始检查”崩溃窗口也不会自动补发，需要查看部署状态并手动检查。这是避免费用不明/重复外部请求的保守策略。

第一回启用只建立下一个09:00到期点，不默认立即搜索；要当天检查，请明确运行下面的手动命令。

## 查看结果与手动检查

```sh
.venv/bin/python -B -m job_agent.macos_schedule status \
  --config .agent-platform/macos-daily/config.json
```

返回最近检查、当天新增岗位、失败原因、保存的下次运行时间、目标北京时间及部署进程状态。`installed_plist`只表示文件存在，不证明任务已加载；`next_if_enabled`是启用后的预期时间，不冒充正在运行的计划。系统加载状态可另查看：

```sh
launchctl print gui/$(id -u)/local.ai-agent-lab.job-agent
```

既有交互CLI的 `/last-check`、`/new-jobs`、`/jobs` 可继续使用同一状态目录。完整摘要保存在现有 `jobs.sqlite`；部署状态在 `dispatch.json`，只含状态/计数的事件在 `events.jsonl`，超过约1MiB保留一份轮换日志。不要公开这些文件。解释器尚未启动成功的错误见 `startup.log` 或 `launchctl print` 的退出码。

手动立即检查不会改变每日计划：

```sh
.venv/bin/python -B main.py --agent job --check-now --mode local \
  --resume /absolute/path/to/resume.pdf \
  --sources job_agent/sources/public-careers.json --allow-network \
  --state-dir .agent-platform/my-existing-jobs
```

本地配置/简历解析失败会停止本次进程，启动日志只记录错误类型，不输出原文材料。业务日志被抑制；岗位和匹配证据只保存在原有私人岗位库中。系统对文件夹的隐私访问控制仍然有效；需要额外授权时由用户确认，不能绕过。

## 停用

```sh
sh .agent-platform/macos-daily/disable.sh
```

确认后只卸载并移除属于此部署的plist，不删除简历、数据库、配置、摘要或投递进度。即使简历已搬走也能停用；遇到其他程序的同名任务会拒绝操作。修改时间/路径时，先批准停用旧任务，再准备并批准启用新部署。

## 验收范围

测试使用可控时间、人工简历、人工JD、Mock网络与Mock launchctl，覆盖生成配置、到点/未到点、休眠补查、重复跳过、新岗位、JD变化、状态/备注、摘要、失败不重试、重启恢复、安装授权和配置篡改。plist及shell语法另外检查。

离线通过不等于系统任务已实际安装。最终启用前仍需：真实公开来源成功检查及重复去重、用户批准安装、启用后核对launchctl状态、首次实际到点/休眠唤醒检查。不要把网络失败或未安装状态描述为生产验收通过。
