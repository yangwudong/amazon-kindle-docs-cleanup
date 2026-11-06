# amazon-kindle-docs-cleanup

清理 Send-to-Kindle 邮箱（`xxx@kindle.com`）收到的个人文档，每天定时跑一次，只保留最近 N 个。

零依赖（纯 Python 标准库），走 Amazon MYCD 私有 AJAX 接口，无需浏览器自动化。

## 接口说明

| 操作 | 端点 | 请求 |
|------|------|------|
| 拿 csrfToken | `GET /hz/mycd/digital-console/contentlist/pdocsAll/dateDsc/` | 从 HTML 里正则提取 `csrfToken = "..."`（注意：页面里 `2@` 开头的 token 是商店前端的，对 mycd ajax 无效，脚本已自动过滤） |
| 列出个人文档 | `POST /hz/mycd/digital-console/ajax` | `activity=GetContentOwnershipData`，`activityInput={"contentType":"KindlePDoc","contentCategoryReference":"pdocs",...}` |
| 删除 | `POST /hz/mycd/digital-console/ajax`（兜底 `/hz/mycd/ajax`） | `activity=DeleteContent`，`activityInput={"asinDetails":{"<ASIN>":{"category":"KindlePDoc"}}}` |

认证 = 浏览器 cookies（`ubid-main`/`at-main`/`x-main`/`session-id` 等，整串粘进 `cookies.txt`）。
cookies 有效期约一年；失效时脚本退出码为 2，方便定时任务告警。

## 使用

1. Mac 浏览器登录 amazon.com → 打开 https://www.amazon.com/hz/mycd/myx （Content & Devices）
2. F12 → Network → 随便点一个请求 → 复制 Request Headers 里整串 `Cookie: ...`
3. 在仓库目录创建 `cookies.txt`，把整串 Cookie 粘进去（纯文本一行即可，**不要**粘进
   config.json —— Cookie 串里常含未转义的双引号，会破坏 JSON 语法）。
   `cookies.txt` 已被 .gitignore 排除，不会被提交。
4. 先干跑验证：

```bash
python3 cleanup.py                  # 只列出将删除的文档，不动真格（保留数取 config.json 的 keep，默认 3）
python3 cleanup.py --keep 3         # CLI 参数覆盖配置
python3 cleanup.py --apply          # 确认列表无误后真正删除
python3 cleanup.py --single-asin <ASIN> --apply   # 只删指定文档（首次验证删除链路用）
```

退出码：`0` 成功；`1` 配置错误；`2` cookies 失效（重新登录复制新 Cookie 即可）；
`3` 有删除失败；`4` 接口响应异常（Amazon 可能改版）。

## 定时任务（macOS launchd）

macOS 上用 launchd（比 cron 更适合睡眠唤醒场景），每天 09:30 跑一次：

```bash
# 从模板生成本机 plist（sed 会自动替换用户名和路径占位符）
mkdir -p ~/Library/LaunchAgents
sed "s|YOUR_USER|$(whoami)|g; s|/Users/$(whoami)/path/to|$(pwd)|" \
  com.kindle.pdocs-cleanup.plist.template \
  > ~/Library/LaunchAgents/com.kindle.pdocs-cleanup.plist
launchctl load ~/Library/LaunchAgents/com.kindle.pdocs-cleanup.plist
# 手动触发一次验证：
launchctl start com.kindle.pdocs-cleanup
# 看日志：
tail -f /tmp/kindle-cleanup.log
```

## 风险与注意

- 非官方接口，属 ToS 灰色地带；脚本已内置随机间隔（逐个删除模式 2~5s/个），每天一次的量级风上很安全。
- 删除是**永久的**（MYCD 界面上"永久删除"语义），不是移到回收站；--apply 前务必先干跑确认列表。
- 若 Amazon 改版导致接口变化，脚本会以退出码 4 失败而不是乱删。
- TLS 指纹：目前 /hz/mycd/ 端点对标准 urllib 客户端是放行的（多个开源项目 python requests 直连成功）；如果哪天开始返回验证码页/非 JSON，需要换 `curl_cffi` 之类模拟浏览器指纹的客户端。

## 参考实现（协议细节来源）

- tldev/flyleaf — `docs/kindle-endpoints.md`（pdocs 列表接口 2026-08 实测记录）
- wkei/kindle-books-manager — `apis/DeleteContent.js`（删除 payload 与响应格式）
- moifort/kindle-api-ts — `src/library.ts`（csrfToken 提取与分页策略）
- sghctoma/bOOkp — legacy `/hz/mycd/ajax` 端点用法
