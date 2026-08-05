# 商城抽奖模块

> 仅提供手动 GitHub Actions 入口。账号、密码、品牌令牌和 TG 凭据必须存入 GitHub Secrets，不得写入仓库。

该目录提供独立的商城 SSO 登录、剩余次数查询、顺序抽奖、JSON/XLSX 汇总和 TG 通知。商城会话只在浏览器上下文中使用 `PRO_NEW_SID`，不会写入结果文件或日志。

## 配置 GitHub Secrets

| Secret | 用途 | 格式 |
| --- | --- | --- |
| `BRAND_TOKEN` | 构造统一登录地址 | 3 个英文字母 |
| `MALL_ACCOUNTS_BATCH1` ... `MALL_ACCOUNTS_BATCH5` | 五个商城账号组 | 每行 `客编,密码` |
| `MALL_ACCOUNTS_TEST` | 商城测试组 | 每行 `客编,密码` |
| `TELEGRAM_BOT_TOKEN` | 发送 TG 汇总 | Bot Token |
| `TELEGRAM_CHAT_ID` | TG 目标会话 | Chat ID |

兼容已有的 `TG_BOT_TOKEN` 和 `TG_CHAT_ID` 名称。商城账号 Secrets 与 PC 普通抽奖组的 `ACCOUNTS_BATCH*` 完全隔离。

## 手动运行工作流

- `商城抽奖组1` ... `商城抽奖组5`：只执行指定账号组。
- `商城抽奖组`：按 1 到 5 的顺序执行全部商城账号组；前一组失败也会继续下一组。
- `商城抽奖组-测试`：使用独立测试账号 Secret。
- `PC普通抽奖组`：原有普通抽奖五组顺序入口。

每个商城组完成后会上传 `mall-campaign-summary-*` artifact，其中包含合并 JSON 和 XLSX，同时发送 TG 文本与 XLSX 文件。

## 本地验证

从仓库根目录运行：

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
$env:BRAND_TOKEN="<3-letter-token>"
$env:ACCOUNT_USERNAME="<customer-code>"
$env:ACCOUNT_PASSWORD="<password>"
python SC-cj668562/mall_lottery.py --output result.json
```

测试不会读取 HAR，也不需要真实会话：

```powershell
python -m unittest discover -s tests -v
python -m unittest discover -s SC-cj668562/tests -v
```

## 结果字段

`result.json` 包含账号组、脱敏账号、执行状态、初始次数、实际抽奖数、剩余次数和奖品列表。奖品记录包括名称、类型、活动奖品 ID、有效期及是否需要绑定。
