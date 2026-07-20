# BTCAlert 生产版

BTCAlert 每天北京时间 08:30 检查 BitMEX `.BVOL7D`、CFTC/CME BTC 期货持仓、USDT 发行/流通量，并在触发阈值或连续故障时通过 Telegram 推送。程序只读公开数据，不使用交易所 API key，不交易，不下单。

## 架构

- 代码目录：`/opt/btc-vol-alert`
- 运行用户：`btcalert`，无登录 shell
- 敏感配置：`/etc/btc-vol-alert.env`，权限 `0640 root:btcalert`
- 运行状态：`/var/lib/btc-vol-alert/state.json`
- 定时执行：`btc-vol-alert.timer` + `btc-vol-alert.service`
- 状态页：`btc-vol-dashboard.service`，Streamlit 只监听 `127.0.0.1:8503`
- 入口：Nginx `127.0.0.1:8502` + Cloudflare Tunnel `btc.matcha00.xyz`

## 安装方法

本地开发：

```bash
bash setup.sh
cp .env.example .env
```

服务器生产部署：

```bash
REMOTE_HOST=43.130.48.131 ./deploy.sh
```

首次生产安装会从旧目录 `/root/btc-vol-alert/.env` 安全迁移到 `/etc/btc-vol-alert.env`，并把旧 `state.json` 迁移到 `/var/lib/btc-vol-alert/state.json`。

## Telegram Bot 配置

编辑生产环境文件：

```bash
sudo vim /etc/btc-vol-alert.env
```

填写：

```bash
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

不要把 `.env` 或 `/etc/btc-vol-alert.env` 提交到 Git。

## 如何获取 Chat ID

1. 在 Telegram 给你的 bot 发送一条消息。
2. 临时执行：

```bash
curl "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getUpdates"
```

3. 在 JSON 中找 `message.chat.id`。群组的 `chat_id` 通常是负数。

## 如何运行

本地 dry-run：

```bash
./.venv/bin/python main.py --dry-run
```

生产 dry-run：

```bash
sudo runuser -u btcalert -- bash -lc 'set -a; source /etc/btc-vol-alert.env; set +a; cd /opt/btc-vol-alert; ./.venv/bin/python main.py --dry-run'
```

只跑单项：

```bash
./.venv/bin/python main.py --monitor bvol --dry-run
./.venv/bin/python main.py --monitor cftc-oi --dry-run
./.venv/bin/python main.py --monitor usdt-supply --dry-run
```

忽略重复报警状态：

```bash
./.venv/bin/python main.py --ignore-state
```

## 查看日志

生产版主要使用 journald：

```bash
journalctl -u btc-vol-alert.service -n 100 --no-pager
journalctl -u btc-vol-dashboard.service -n 100 --no-pager
```

旧 cron 日志保留在 `/root/btc-vol-alert/logs/`，生产迁移后不再继续增长。

## 修改阈值

在 `/etc/btc-vol-alert.env` 修改：

```bash
HIGH_VOL_WARNING_THRESHOLD=13
HIGH_VOL_ALERT_THRESHOLD=15
LOW_VOL_LOW_THRESHOLD=4
LOW_VOL_MEDIUM_THRESHOLD=3
LOW_VOL_HIGH_THRESHOLD=2

CFTC_BTC_OI_BTC_THRESHOLD=150000
CFTC_BTC_OI_BTC_LOW_THRESHOLD=100000
USDT_SUPPLY_DROP_THRESHOLD_PERCENT=0.5
FAILURE_ALERT_THRESHOLD=3
```

阈值顺序必须满足：

```text
LOW_VOL_HIGH_THRESHOLD < LOW_VOL_MEDIUM_THRESHOLD < LOW_VOL_LOW_THRESHOLD < HIGH_VOL_WARNING_THRESHOLD <= HIGH_VOL_ALERT_THRESHOLD
```

## Cron 与 Systemd

旧 cron 示例：

```cron
30 8 * * * cd /root/btc-vol-alert && /root/btc-vol-alert/.venv/bin/python main.py >> /root/btc-vol-alert/logs/cron.log 2>&1
```

生产版改为：

```bash
systemctl status btc-vol-alert.timer
systemctl list-timers btc-vol-alert.timer
```

确认 timer 稳定后，删除 root crontab 中 BTCAlert 旧条目，但保留腾讯云 `stargate` cron。

## 状态页

状态页只读，不允许修改阈值、发送 Telegram 或执行服务器命令。

```bash
systemctl status btc-vol-dashboard.service
curl -I -H 'Host: btc.matcha00.xyz' http://127.0.0.1:8502/
```

最终访问：

```text
https://btc.matcha00.xyz
```

如果 `cloudflared.service` 使用 `--token-file` 运行，public hostname 通常由 Cloudflare Zero Trust 远程管理。本仓库提供了本地参考规则 `deploy/cloudflared/btc-ingress.example.yml`，但真正生效的配置需要在 Cloudflare 控制台把 `btc.matcha00.xyz` 指向现有 tunnel，origin 设置为：

```text
http://127.0.0.1:8502
```

## VSCode Remote SSH

1. VSCode 安装 Remote SSH。
2. 连接 `root@43.130.48.131`。
3. 打开 `/opt/btc-vol-alert`。
4. 不要在 VSCode 中打开或提交 `/etc/btc-vol-alert.env`。

## 部署与升级

本地提交后部署：

```bash
git status
git add .
git commit -m "Production harden BTCAlert"
git push
REMOTE_HOST=43.130.48.131 ./deploy.sh
```

服务器手动刷新：

```bash
cd /opt/btc-vol-alert
APP_DIR=/opt/btc-vol-alert bash deploy/install_production.sh
systemctl restart btc-vol-dashboard.service
systemctl start btc-vol-alert.service
```

## 备份与恢复

备份重点：

```bash
tar -czf /root/btc-vol-alert-backup.tgz \
  /opt/btc-vol-alert \
  /var/lib/btc-vol-alert/state.json \
  /etc/btc-vol-alert.env \
  /etc/systemd/system/btc-vol-alert.service \
  /etc/systemd/system/btc-vol-alert.timer \
  /etc/systemd/system/btc-vol-dashboard.service \
  /etc/nginx/conf.d/btc.matcha00.xyz.conf
```

恢复后执行：

```bash
systemctl daemon-reload
systemctl enable --now btc-vol-alert.timer btc-vol-dashboard.service
nginx -t && systemctl reload nginx
```

## 故障排查

```bash
systemctl status btc-vol-alert.service btc-vol-alert.timer btc-vol-dashboard.service
journalctl -u btc-vol-alert.service -n 200 --no-pager
ss -ltnp | grep -E '8501|8502|8503'
python -m pytest -q
```

常见问题：

- Telegram 无消息：检查 `/etc/btc-vol-alert.env` 中 token/chat_id，日志不会输出敏感值。
- 数据源失败：达到 `FAILURE_ALERT_THRESHOLD` 后会发送一次故障通知，恢复后发送一次恢复通知。
- 状态文件损坏：程序会备份为 `state.json.corrupt.<timestamp>`，并用安全默认状态继续运行。
