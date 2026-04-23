# Windsurf Auto Register

这个项目整理成了一个可执行工具：

1. 用 `YYDS Mail` 自动创建邮箱并收验证码
2. 自动完成 Windsurf 注册
3. 自动换取 `devin-session-token` 与 `ott$...`
4. 自动把 `ott$...` 上传到你手动指定的 `WindsurfPoolAPI`
5. 可选继续完成 Turnstile 求解并生成 Pro Trial 的 Stripe Checkout URL

默认上传入口是：

- `POST /auth/login`

兼容的 dashboard 上传入口是：

- `POST /dashboard/api/accounts`

## 文件结构

- `windsurf_auth_replay.py`
  主入口。负责串联注册、取验证码、换 token、上传到 Pool，以及可选的 Trial 链接生成。
- `providers/yyds_mail.py`
  `YYDS Mail` provider 实现。
- `proto_handler.py`
  兼容旧入口，内部已转发到 `windsurf_auth_replay.py --mode trial`。
- `solver_server.py`
  可选的本地 Turnstile solver HTTP 服务。主脚本默认不依赖它，也可以直接内置浏览器求解。
- `windsurf_trial_browser.py`
  浏览器自动化版 Trial 流程。会自己打开浏览器、填账号密码、点击页面元素并抓取 Stripe Checkout URL。
## 依赖

基础依赖：

- Python 3.10+
- `requests`

如果你只用注册/上传 OTT，这就够了。

如果你还要生成 Pro Trial 链接，额外需要：

- `patchright`

推荐先创建虚拟环境，再安装依赖：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

如果你只需要基础注册/上传能力，也可以只安装：

```bash
pip install requests
```

## 配置方式

优先支持环境变量，也可以全部走命令行参数。

可以先复制示例文件：

```bash
cp .env.example .env
```

再编辑 `.env` 填入自己的 `YYDS_MAIL_API_KEY` 等变量。主脚本会自动读取当前目录的 `.env`，填好后可以直接运行。

### Windsurf

- `WINDSURF_BASE_URL`
  默认 `https://windsurf.com`
- `WINDSURF_EMAIL`
  `trial` 模式下已有账号的邮箱
- `WINDSURF_SESSION_TOKEN`
  `trial` 模式可直接提供现成 `devin-session-token`
- `WINDSURF_GENERATE_TRIAL_LINK`
  `true` 时，完整注册模式会在上传 OTT 后继续生成 Trial 链接

### WindsurfPoolAPI

- `WINDSURF_POOL_URL`
  无默认值，必须手动填写
- `WINDSURF_POOL_UPLOAD_MODE`
  `auth` 或 `dashboard`，默认 `auth`
- `WINDSURF_POOL_DASHBOARD_PASSWORD`
  无默认值。仅 dashboard 模式需要，必须手动填写
- `WINDSURF_POOL_SSH_KEY_PATH`
  默认 `~/.ssh/id_ed25519`
- `WINDSURF_POOL_SSH_USER`
  默认 `root`

### YYDS Mail

- `YYDS_MAIL_BASE_URL`
  默认 `https://maliapi.215.im/v1`
- `YYDS_MAIL_API_KEY`
  无默认值，必须手动填写
- `YYDS_MAIL_DOMAIN`
  可选
- `YYDS_MAIL_SUBDOMAIN`
  可选
- `YYDS_MAIL_LOCAL_PART`
  可选

### 注册信息

- `WINDSURF_NAME`
  可选。为空时自动生成随机昵称。
- `WINDSURF_PASSWORD`
  `full` 模式下为空时运行时提示输入；直接回车会自动生成密码。`trial` 模式下如果不提供 `session token`，这里应填写已有账号密码。
- `WINDSURF_ACCOUNT_COUNT`
  可选。为空时脚本启动后询问本轮注册数量。

### Pro Trial / Turnstile

- `WINDSURF_TURNSTILE_TOKEN`
  已知 token。提供后直接跳过浏览器求解。
- `WINDSURF_TURNSTILE_SITE_URL`
  默认 `https://windsurf.com/billing/individual?plan=9`
- `WINDSURF_TURNSTILE_SITEKEY`
  可选，调试用。
- `WINDSURF_TRIAL_SUCCESS_URL`
  默认 `/subscription/pending?expect_tier=trial`
- `WINDSURF_TRIAL_CANCEL_URL`
  默认 `/plan?plan_cancelled=true&plan_tier=trial`
- `WINDSURF_TRIAL_PLAN_ID`
  目标需要显式 plan_id 时再填写。
- `TURNSTILE_SOLVER_URL`
  可选，兼容独立 solver 服务，例如 `http://127.0.0.1:3000/solve`
- `TURNSTILE_BROWSER_PATH`
  可选，本地浏览器路径；为空时自动尝试常见 Chrome/Chromium 路径。
- `TURNSTILE_TIMEOUT`
  默认 `90`
- `TURNSTILE_HEADLESS`
  默认 `true`

### 通用

- `REQUEST_TIMEOUT`
  默认 `20`
- `POLL_TIMEOUT`
  默认 `60`
- `POLL_INTERVAL`
  默认 `5`
- `MAX_ATTEMPTS`
  默认 `5`，遇到邮箱注册后没有可用组织时自动换邮箱重试
- `VERIFY_SSL`
  默认 `true`
- `DEBUG`
  默认 `false`

## 用法

### 1. 完整自动流程

这是默认模式，会：

1. 创建 YYDS 邮箱
2. 发验证码
3. 自动读邮件拿验证码
4. 完成 Windsurf 注册
5. 生成 `ott`
6. 上传到 `WindsurfPoolAPI`

启动后会先询问本轮需要注册多少个账号。直接回车默认注册 `1` 个。

```bash
export YYDS_MAIL_API_KEY='你的YYDS_API_KEY'
python windsurf_auth_replay.py
```

也可以全命令行传参：

```bash
python windsurf_auth_replay.py \
  --name demo-user \
  --password 'DemoPassword123' \
  --yyds-api-key '你的YYDS_API_KEY'
```

### 2. 完整流程后继续生成 Pro Trial 链接

```bash
python windsurf_auth_replay.py \
  --generate-trial-link
```

这条路径现在会在注册并上传 OTT 后，直接进入浏览器自动化 Trial 流程，不再先尝试 Trial API。

如果你已经拿到 Turnstile token，并且是走仅 `session-token` 的 API 路径，也可以直接塞进去跳过浏览器求解：

```bash
python windsurf_auth_replay.py \
  --generate-trial-link \
  --turnstile-token '0.xxxxx'
```

### 3. 只为已有账号生成 Pro Trial 链接

```bash
python windsurf_auth_replay.py \
  --mode trial \
  --email 'user@example.com' \
  --password 'ExistingPassword123'
```

如果你已经有 `devin-session-token$...`：

```bash
python windsurf_auth_replay.py \
  --mode trial \
  --session-token 'devin-session-token$...'
```

旧入口仍可用：

```bash
python proto_handler.py --session-token 'devin-session-token$...'
```

### 4. 用浏览器自动点击页面生成 Trial 链接

```bash
python windsurf_trial_browser.py \
  --email 'user@example.com' \
  --password 'ExistingPassword123'
```

这个脚本会：

1. 打开登录页
2. 自动输入邮箱和密码
3. 自动进入 `billing/individual?plan=9`
4. 自动点击 Turnstile / Trial 按钮
5. 从前端网络响应中抓出 Stripe Checkout URL

### 5. 只上传现成 OTT

如果你已经拿到了 `ott$...`，不需要再注册，可以直接上传：

```bash
python windsurf_auth_replay.py \
  --mode upload \
  --ott 'ott$your-token'
```

### 6. 使用 dashboard 模式上传

如果你想严格复现原始 dashboard 导入链路：

```bash
python windsurf_auth_replay.py \
  --mode upload \
  --ott 'ott$your-token' \
  --pool-upload-mode dashboard
```

此时需要手动提供 `--pool-dashboard-password`，或者预先设置 `WINDSURF_POOL_DASHBOARD_PASSWORD`。

### 7. 单独启动本地 solver 服务

如果你仍然想把 Turnstile 求解拆成一个 HTTP 服务：

```bash
python solver_server.py
```

然后主流程里设置：

```bash
export TURNSTILE_SOLVER_URL='http://127.0.0.1:3000/solve'
```

## 典型示例

### CTF 环境下关闭证书校验

```bash
python windsurf_auth_replay.py \
  --insecure \
  --name ctf-user \
  --yyds-api-key '你的YYDS_API_KEY'
```

### 保存结果到 JSON

默认写出的 JSON 会脱敏：

```bash
python windsurf_auth_replay.py \
  --mode upload \
  --ott 'ott$your-token' \
  --output-json result.json
```

如果你明确需要把完整 token 落盘：

```bash
python windsurf_auth_replay.py \
  --mode upload \
  --ott 'ott$your-token' \
  --output-json result.json \
  --include-secrets-in-output
```

## 输出说明

脚本默认只显示脱敏后的敏感字段，例如：

- `auth1_xxx...`
- `devin-session-token$xxx...`
- `ott$xxx...`

如果明确要在终端摘要中看完整密码和 token：

```bash
python windsurf_auth_replay.py --show-secrets ...
```

## 说明

`YYDS Mail` 的对接基于它公开文档暴露的接口面：

- `POST /v1/accounts`
- `GET /v1/messages?address=...`
- `GET /v1/messages/{id}?address=...`

`WindsurfPoolAPI` 的上传路径则是直接按远端服务实际实现接入：

- `POST /auth/login`
- `POST /dashboard/api/accounts`

Windsurf 后端的 protobuf 接口已按当前前端实现发送二进制请求体：

- `WindsurfPostAuthRequest.auth1_token`
- `WindsurfPostAuthRequest.org_id`
- `GetOneTimeAuthTokenRequest.auth_token`

如果某个随机邮箱注册后返回 `no eligible organizations found`，脚本会自动换邮箱重试，默认最多 `MAX_ATTEMPTS=5` 次。

## 当前验证情况

本地已确认：

1. `python -m py_compile windsurf_auth_replay.py proto_handler.py solver_server.py providers/yyds_mail.py`
2. `python windsurf_auth_replay.py --help`
3. `python proto_handler.py --help`
4. 主 CLI 已集成 `full / upload / trial` 三种模式
5. `proto_handler.py` 已转成兼容包装，不再维护一套独立的 Trial 逻辑
6. `solver_server.py` 已去掉 Flask 依赖，改成内建 HTTP server
7. `solver_server.py` 本地启动后，`GET /health` 返回 `{"ok": true}`
8. `patchright` 安装后可被导入，`trial-browser` 路径可用

真实远端链路是否能一次跑通，仍取决于：

1. 你的 `YYDS_MAIL_API_KEY` 是否有效
2. 本机是否已安装 `patchright`，或你是否提供了 `TURNSTILE_SOLVER_URL / WINDSURF_TURNSTILE_TOKEN`
