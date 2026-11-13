# jobpilot · 求职自动化控制台

把「筛岗位 → 打招呼 → 跟进回复 → 交换微信」这条链做成一个本地可控、可观察、可暂停的面板。

> **定位**：个人求职效率工具。所有操作都在**你自己的电脑**上、用**你自己的账号**、按**你设定的节奏**进行。
> 不提供账号、不代运营、不上传任何数据到第三方服务器。

---

## 它解决什么问题

手动海投的真实痛点不是"点得慢"，而是：

1. **节奏难控**——一口气点 30 个，平台会判定环境异常，账号被锁一天
2. **状态丢失**——谁打过、谁回了、谁要了微信，全靠脑子记
3. **打断即归零**——电脑睡眠、浏览器崩溃、忘了点开始，一天就过去了

这个项目把三件事固定下来：**节奏引擎**（按段分配、间隔随机、轮次休息）、**本地账本**（每个岗位的状态机、每个 HR 的会话）、**守护进程**（挂了自动拉起、卡死自动重启）。

## 核心设计（几个不显然的决定）

| 决定 | 为什么 |
|---|---|
| **驱动真实的桌面 Chrome**（CDP），不用无头浏览器 | 无头环境的指纹特征明显，且登录态无法复用 |
| **一个账号 = 一个服务进程** | 额度、去重账本、熔断状态必须物理隔离，否则 A 号熔断会带死 B 号 |
| **账号识别靠 Chrome profile，不靠 Cookie** | 复制 Cookie 到别的 profile 既泄露凭据又引发会话竞争 |
| **风控熔断由代码执行，不由意志执行** | 每日探测次数、连续错误次数都有硬上限，超了直接拒绝 |
| **界面的数字必须是真的** | 接口路径永不缓存；离线时显示"上次数据"并明确标注，而不是假装在线 |

## 快速开始

```bash
# 1) 依赖（Python 3.11+，Chrome 已安装）
python -m venv .venv2
.venv2\Scripts\pip install -r requirements.txt

# 2) 配置：复制模板后改三处路径
copy config.example.yaml config.yaml
#   accounts[].chrome_profile   专用 Chrome 资料目录（换账号必须换它）
#   accounts[].data_dir         上游 CLI 的工作目录
#   accounts[].chrome_exe       本机 Chrome 路径

# 3) 启动专用浏览器（必须带调试端口，且用独立 profile）
"C:\Program Files\Google\Chrome\Application\chrome.exe" ^
  --remote-debugging-address=127.0.0.1 --remote-debugging-port=9333 ^
  --user-data-dir="C:\Users\<you>\chrome-jobpilot"
# 在这个窗口里登录求职账号

# 4) 一键启动（含端口自愈、健康检查、自动起跑）
.venv2\Scripts\python -m app.tools.launch --detach --cap 25 --open
```

打开 <http://127.0.0.1:8010> 即可看到当日进度、会话、微信号与运行状态。

## 常用命令

```bash
python -m app.tools.envcheck                 # 环境体检：出口 IP / Chrome / profile / 风控锁
python -m app.tools.recover --confirmed      # 风控恢复：清锁 → 单页探测（受每日配额限制）
python -m app.tools.accounts list            # 账号清单
python -m app.tools.multi status             # 多账号汇总
python -m app.tools.autostart install        # 开机自启 + 每 5 分钟自愈
python -m pytest -q                          # 全量测试（全程离线，不产生任何平台请求）
```

## 安全与边界

这个项目**刻意保留了几道"不方便"的闸门**，它们不是没写完，是设计如此：

- **每日探测配额**（`safety.probe_quota_per_day`）：诊断性请求有上限，超了拒绝执行
- **风控熔断不自动重启**：熔断是保护，不是故障
- **锁着不许起跑**：风控锁存在时，起跑命令直接跳过
- **登录失效即停**：不冷却重试，因为过期凭据重试永远不会成功

## 免责声明

- 仅供**个人学习与求职效率**使用，请遵守目标平台的服务条款
- 自动化操作第三方平台存在账号被限制的风险，**使用者自担风险**
- 请自行控制频率与规模；作者不鼓励也不支持任何形式的滥用
- 项目内不含任何真实用户数据、账号凭据或平台接口密钥

## 目录

```
app/
  adapters/     平台适配器（boss / fake）
  core/         节奏、状态机、账号、探测配额、PWA 自检等纯逻辑
  services/     节奏引擎（pacer）
  api/          HTTP 接口
  web/          无构建的单页控制台（PWA）
  tools/        命令行工具（launch / recover / envcheck / accounts / multi / autostart ...）
docs/           设计笔记与打包说明
tests/          全量测试（离线）
```

## 许可

MIT。上游浏览器自动化能力由第三方包提供（同样是 MIT），本项目只声明依赖，不打包其源码。
