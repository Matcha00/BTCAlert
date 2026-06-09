# BTC 风险预警机器人

每天北京时间 08:30 检查 BitMEX `.BVOL7D` 指数、CFTC/CME Bitcoin futures open interest，以及 USDT 总发行/流通量。满足任意预警条件时，通过 Telegram Bot 推送消息，并用 `state.json` 控制重复推送。

## 功能

- 数据源优先使用 BitMEX 公共 API，无需交易所 API key。
- 优先读取 `trade/bucketed` 的 1 日数据；如果无可用数据，自动 fallback 到 `instrument` 的 `lastPrice`、`markPrice`、`indicativeSettlePrice`。
- 使用 CFTC 官方年度 COT 历史压缩 CSV 监控 CME Bitcoin futures open interest。
- 使用 DefiLlama stablecoins API 监控 USDT 总发行/流通量。
- 支持 Telegram Markdown 消息。
- 所有异常写入日志，日志同时显示 UTC 与北京时间。
- 不交易、不下单。

## 安装方法

服务器建议目录：

```bash
cd /root
git clone <your-repo-url> btc-vol-alert
cd /root/btc-vol-alert
bash setup.sh
```

如果你是直接上传文件：

```bash
cd /root/btc-vol-alert
bash setup.sh
```

## Telegram Bot 配置

复制环境变量模板：

```bash
cp .env.example .env
vim .env
```

填写：

```bash
TELEGRAM_BOT_TOKEN=你的_bot_token
TELEGRAM_CHAT_ID=你的_chat_id
```

Bot token 获取方式：

1. 在 Telegram 搜索 `@BotFather`。
2. 发送 `/newbot` 创建机器人。
3. 保存 BotFather 返回的 token。

## 如何获取 chat_id

私聊场景：

1. 打开你的 bot，先发送任意消息，例如 `hi`。
2. 在终端执行：

```bash
curl "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getUpdates"
```

3. 在返回 JSON 里找到 `message.chat.id`，填入 `.env` 的 `TELEGRAM_CHAT_ID`。

群组场景：

1. 把 bot 加入群组。
2. 在群组里发送一条消息并提到 bot。
3. 执行同样的 `getUpdates` 命令。
4. 群组 `chat_id` 通常是负数。

## 如何运行

Dry-run 测试，不发送 Telegram，不更新 `state.json`：

```bash
./.venv/bin/python main.py --dry-run
```

正常运行：

```bash
./.venv/bin/python main.py
```

只跑某一个监控：

```bash
./.venv/bin/python main.py --monitor bvol --dry-run
./.venv/bin/python main.py --monitor cftc-oi --dry-run
./.venv/bin/python main.py --monitor usdt-supply --dry-run
```

忽略当天已推送状态，适合手动验证：

```bash
./.venv/bin/python main.py --ignore-state
```

## 如何查看日志

```bash
tail -f logs/btc_vol_alert.log
tail -f logs/cron.log
```

每条应用日志都会包含 UTC 与北京时间。

## 如何修改阈值

可以在 `.env` 修改：

```bash
HIGH_VOL_WARNING_THRESHOLD=13
HIGH_VOL_ALERT_THRESHOLD=15

LOW_VOL_LOW_THRESHOLD=4
LOW_VOL_MEDIUM_THRESHOLD=3
LOW_VOL_HIGH_THRESHOLD=2
```

含义：

- `HIGH_VOL_WARNING_THRESHOLD`：高波动预警，默认 `.BVOL7D >= 13`。
- `HIGH_VOL_ALERT_THRESHOLD`：高波动高级警报，默认 `.BVOL7D >= 15`。
- `LOW_VOL_LOW_THRESHOLD`：低波动低级警报，默认 `.BVOL7D < 4`。
- `LOW_VOL_MEDIUM_THRESHOLD`：低波动中级警报，默认 `.BVOL7D < 3`。
- `LOW_VOL_HIGH_THRESHOLD`：低波动高级警报，默认 `.BVOL7D < 2`。

阈值顺序必须满足：

```text
LOW_VOL_HIGH_THRESHOLD < LOW_VOL_MEDIUM_THRESHOLD < LOW_VOL_LOW_THRESHOLD < HIGH_VOL_WARNING_THRESHOLD <= HIGH_VOL_ALERT_THRESHOLD
```

## CME 持仓量监控

机器人默认会启用 CFTC/CME BTC OI 监控。数据来自 CFTC 官方 `Commitments of Traders Futures Only` 年度压缩文件，筛选：

```text
BITCOIN - CHICAGO MERCANTILE EXCHANGE
CFTC Contract Market Code: 133741
```

默认规则：

- 读取最新一周 CME Bitcoin futures open interest。
- 根据 CFTC 的 `Contract Units` 自动把张数换算成 BTC 名义持仓。
- 使用 BitMEX `.BXBT` 指数价格把 BTC 名义持仓换算成 USD。
- 当前 BTC 名义持仓大于 `52周均值 x 1.0` 时触发。
- 也可以设置固定 BTC 高位/低位阈值，或固定 USD 阈值。
- 同一个 COT 报告日期只推送一次。

可以在 `.env` 修改：

```bash
ENABLE_CFTC_BTC_OI=true
CFTC_BTC_OI_LOOKBACK_WEEKS=52
CFTC_BTC_OI_MIN_HISTORY_WEEKS=8
ENABLE_CFTC_BTC_OI_MEAN_ALERT=true
CFTC_BTC_OI_MEAN_MULTIPLIER=1.0
CFTC_BTC_OI_CONTRACT_THRESHOLD=
CFTC_BTC_OI_BTC_THRESHOLD=
CFTC_BTC_OI_BTC_LOW_THRESHOLD=
CFTC_BTC_OI_USD_THRESHOLD=
```

如果你希望“高于均值 10% 才报警”，改成：

```bash
CFTC_BTC_OI_MEAN_MULTIPLIER=1.1
```

如果你希望“突破一个固定 BTC 名义持仓才报警”，设置 BTC 阈值：

```bash
CFTC_BTC_OI_BTC_THRESHOLD=150000
```

如果你希望“跌破一个固定 BTC 名义持仓才报警”，设置低位 BTC 阈值：

```bash
CFTC_BTC_OI_BTC_LOW_THRESHOLD=100000
```

如果你只想使用固定高低阈值，不想使用 52 周均值报警：

```bash
ENABLE_CFTC_BTC_OI_MEAN_ALERT=false
```

如果你希望“突破一个固定美元名义持仓才报警”，设置 USD 阈值：

```bash
CFTC_BTC_OI_USD_THRESHOLD=10000000000
```

## USDT 发行总量监控

机器人默认启用 USDT 总发行/流通量监控。数据来自 DefiLlama stablecoins API，筛选 Tether/USDT：

```text
stablecoin id: 1
symbol: USDT
```

默认规则：

- 读取 USDT 当前总量和前一日总量。
- 计算 24h 总量变化百分比。
- 当总量跌幅 `>= 0.5%` 时触发。
- 同一个北京时间日期只推送一次。

可以在 `.env` 修改：

```bash
ENABLE_USDT_SUPPLY=true
USDT_SUPPLY_STABLECOIN_ID=1
USDT_SUPPLY_DROP_THRESHOLD_PERCENT=0.5
```

如果你想临时测试 USDT 推送，可以把阈值调得很小：

```bash
USDT_SUPPLY_DROP_THRESHOLD_PERCENT=0.001 ./.venv/bin/python main.py --monitor usdt-supply --dry-run
```

## 如何设置 cron

确认服务器时区：

```bash
timedatectl
```

如果需要设置为 Asia/Shanghai：

```bash
sudo timedatectl set-timezone Asia/Shanghai
```

编辑 crontab：

```bash
crontab -e
```

如果服务器是 Asia/Shanghai，添加：

```cron
30 8 * * * cd /root/btc-vol-alert && /root/btc-vol-alert/.venv/bin/python main.py >> /root/btc-vol-alert/logs/cron.log 2>&1
```

## 如何使用 VSCode Remote SSH

1. 本地 VSCode 安装 `Remote - SSH` 插件。
2. 打开命令面板，选择 `Remote-SSH: Connect to Host...`。
3. 连接你的腾讯云服务器。
4. 打开目录 `/root/btc-vol-alert`。
5. 在 VSCode 终端执行：

```bash
bash setup.sh
cp .env.example .env
vim .env
./.venv/bin/python main.py --dry-run
```

## 如何部署

本地执行：

```bash
REMOTE_HOST=你的服务器IP ./deploy.sh
```

可选参数：

```bash
REMOTE_USER=root REMOTE_HOST=你的服务器IP REMOTE_DIR=/root/btc-vol-alert SSH_PORT=22 ./deploy.sh
```

`deploy.sh` 会：

- 使用 `rsync` 同步项目到 VPS。
- 排除 `.venv`、`.git`、`logs`、`__pycache__`、`.env`。
- SSH 到 VPS。
- 自动安装 `requirements.txt`。
- 自动执行一次 `main.py --dry-run` 测试。

部署后，在服务器上创建 `.env`：

```bash
cd /root/btc-vol-alert
cp .env.example .env
vim .env
```

也可以从本地安全复制 `.env` 到服务器，不经过 git：

```bash
scp .env root@你的服务器IP:/root/btc-vol-alert/.env
```

服务器测试：

```bash
ssh root@你的服务器IP
cd /root/btc-vol-alert
./.venv/bin/python main.py --dry-run
```

如果要在服务器上真实测试 Telegram 推送，可以临时放宽低波动阈值，不修改 `.env`：

```bash
LOW_VOL_LOW_THRESHOLD=100 HIGH_VOL_WARNING_THRESHOLD=101 HIGH_VOL_ALERT_THRESHOLD=102 ./.venv/bin/python main.py --ignore-state
```

如果只想测试 CME OI 推送，可以临时降低均值倍率：

```bash
CFTC_BTC_OI_MEAN_MULTIPLIER=0.1 ./.venv/bin/python main.py --monitor cftc-oi --ignore-state
```

如果只想测试 USDT 总量推送，可以临时降低跌幅阈值：

```bash
USDT_SUPPLY_DROP_THRESHOLD_PERCENT=0.001 ./.venv/bin/python main.py --monitor usdt-supply --ignore-state
```

测试完成后正常运行：

```bash
./.venv/bin/python main.py
```

## 预警条件

### BitMEX .BVOL7D

满足任意条件即触发，并且只推送当前最严重等级：

- 🔴 高波动高级警报：`.BVOL7D >= 15`
- 🟠 高波动预警：`.BVOL7D >= 13`
- 🟢 低波动低级警报：`.BVOL7D < 4`
- 🟡 低波动中级警报：`.BVOL7D < 3`
- 🔴 低波动高级警报：`.BVOL7D < 2`

消息示例：

```text
🟢 BTC 低波动低级警报

指标：BitMEX .BVOL7D
当前值：3.8
昨日值：4.1
日变化：-0.3
30日分位：8%

触发原因：
- 🟢 低波动低级警报：.BVOL7D < 4

状态判断：
低波动低级警报，波动率偏低，开始进入观察区。

风险提示：
BTC 短期波动率进入异常区间，请检查现货、杠杆、止损与对冲风险。
```

### CFTC/CME BTC OI

消息示例：

```text
🔴 CME BTC 持仓量预警

指标：CFTC COT / CME Bitcoin Futures Open Interest
报告日期：2026-05-19
当前 OI：23,000 张
合约单位：(5 Bitcoins)
折合 BTC：115,000 BTC
折合美元：$8.69B
BTCUSD：$75,591（.BXBT.lastPrice）

52周均值：26,152 张 / 130,760 BTC / $9.88B
均值触发线：26,152 张 / 130,760 BTC（均值 x 1.00）
固定张数阈值：未设置
固定 BTC 高位阈值：150,000
固定 BTC 低位阈值：100,000
固定 USD 阈值：未设置
偏离均值：-3,152 张（-12.1%）
周变化：-535 张 / -2,675 BTC / -$202.20M

触发原因：
- 当前 BTC 名义持仓 > 52周均值 x 1.00

风险提示：
OI 升高本身不判断方向，但常意味着后续波动、挤仓或趋势延续风险上升，请结合价格、资金费率和波动率一起看。
```

### USDT Supply

消息示例：

```text
🔴 USDT 发行总量下跌预警

指标：USDT 总发行/流通量
数据源：DefiLlama Stablecoins
当前总量：186.832B USDT
昨日总量：186.837B USDT
24h变化：-5.079M USDT（-0.003%）
跌幅阈值：-0.500%

触发原因：
- USDT 总量 24h 跌幅 >= 0.500%

风险提示：
稳定币供应下滑本身不判断价格方向，但会影响市场可用美元流动性，请结合 BTC 波动率、CME OI、交易所余额和价格结构一起看。
```
