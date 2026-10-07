# Claude Code + claude-code-router + Cline API 通道搭建教程

> 目标：让 Claude Code 通过 claude-code-router（CCR）走 Cline API（`api.cline.bot`）调用 DeepSeek 等模型，
> 使用 `cline-pass/` 前缀模型走订阅额度（cost=0，不扣 API 余额），并**保证 DeepSeek 服务端 KV/prompt cache 正常命中**。
>
> 本教程在 Linux 云服务器（Node 22，无桌面）上实测通过，所有命令可直接照抄。

## 0. 架构总览

```
Claude Code (claude CLI)
   │  Anthropic Messages 格式（流式 + 工具调用）
   ▼
CCR 网关 (127.0.0.1:3456)          ← Anthropic → OpenAI 格式转换
   │  OpenAI Chat Completions 格式
   ▼
剥壳代理 (127.0.0.1:3999)          ← 只剥响应的 {"data":{...}} 包装，不碰请求体
   │
   ▼
https://api.cline.bot/api/v1  →  cline-pass/deepseek-v4.1-flash
```

**为什么需要剥壳代理**：Cline API 的非流式响应包了一层 `{"data": {choices:[...]}}` 信封，
CCR 解析时只认标准 OpenAI 格式，会报
`OpenAI response does not contain text output, reasoning output, or tool calls.`。
剥壳代理只在**响应侧**处理；**请求体一个字节都不改**——这是缓存命中的关键。

**为什么非流式要在代理内部转流式再聚合**：Cline API 的非流式端点不稳定，
会间歇性返回 `{"error":"empty response content","success":false}`（HTTP 500），
而流式端点稳定。所以代理 v2 对非流式请求**内部改用 stream=true 调 Cline**，
把 SSE chunk 聚合成一个标准非流式响应返回，并内置 3 次重试。
注意：这只影响代理→Cline 这一段的传输方式，发给 Cline 的 messages/system 内容不变，
不影响 Claude Code 侧的流式行为，也不影响缓存前缀。

**一个相关坑**：DeepSeek v4.1 是推理模型，思考内容也消耗 `max_tokens`。
测试时 `max_tokens: 30` 会被思考吃光导致 `content: null`（`finish_reason: "length"`），
不是 bug——测试请用 `max_tokens >= 500`。

**为什么能保住缓存**：DeepSeek 的 prompt cache 是服务端前缀匹配，自动生效。
只要发出去的请求前缀（system prompt + 消息历史）字节一致即可命中。
本方案中 CCR 用纯转换、无额外 transformer，剥壳代理不碰请求，所以前缀稳定。

## 1. 安装

```bash
# 无全局权限时用用户级 npm 前缀
mkdir -p ~/.npm-global && npm config set prefix ~/.npm-global
export PATH=~/.npm-global/bin:$PATH   # 建议写进 ~/.bashrc

npm install -g @anthropic-ai/claude-code @musistudio/claude-code-router
```

版本参考：claude-code 2.1.292，CCR 3.1.1（新版 CCR 用 **SQLite** 存配置，
不再读 `~/.claude-code-router/config.json`——这是最大的坑，见第 3 步）。

## 2. 先直连验证 Cline key 和模型（跳过一切代理）

```bash
curl -s -X POST https://api.cline.bot/api/v1/chat/completions \
  -H "Authorization: Bearer $CLINE_KEY" -H "Content-Type: application/json" \
  -d '{"model":"cline-pass/deepseek-v4.1-flash","messages":[{"role":"user","content":"回复两个字：成功"}],"max_tokens":50}'
```

预期：返回 `{"data":{"choices":[{"message":{"content":"成功",...`。
注意两点：
- 响应带 `{"data":...}` 信封 → 确认需要剥壳代理。
- `provider_metadata.gateway.cost` 为 `"0"` → 走的是 cline-pass 订阅额度。

模型 ID 说明：
- `cline-pass/deepseek-v4.1-flash` —— 订阅通道（cost=0），**不在公开 /models 列表里但 API 接受**；
- `deepseek/deepseek-v4.1-flash` —— 普通 API 计费通道；
- 两者都可用，选 `cline-pass/` 省钱。

## 3. 剥壳代理（核心组件，v2）

v2 功能：剥 `{"data":}` 信封 + 非流式内部转流式聚合（绕开 Cline 非流式 500）+ 3 次重试。
保存为 `~/.claude-code-router/cline-unwrap-proxy.js`：

```js
// Unwrap proxy for Cline API: listens 127.0.0.1:3999 -> https://api.cline.bot/api/v1
// - Requests pass through UNMODIFIED (critical for prompt-cache prefix stability).
// - Non-stream responses: strips the {"data":{...}} envelope.
// - Non-stream requests are internally executed as stream and re-aggregated,
//   because Cline's non-stream path intermittently returns 500 "empty response content".
// - Stream requests: transparent SSE passthrough.
const http = require("http");
const https = require("https");

const UPSTREAM = "api.cline.bot";

function post(path, bodyObj, extraHeaders) {
  const body = JSON.stringify(bodyObj);
  return new Promise((resolve, reject) => {
    const req = https.request({
      hostname: UPSTREAM, path: "/api/v1" + path, method: "POST",
      headers: { "content-type": "application/json", "content-length": Buffer.byteLength(body),
                 host: UPSTREAM, ...(extraHeaders || {}) },
    }, (res) => {
      const chunks = [];
      res.on("data", (c) => chunks.push(c));
      res.on("end", () => resolve({ status: res.statusCode, text: Buffer.concat(chunks).toString("utf8") }));
    });
    req.on("error", reject);
    req.end(body);
  });
}

function aggregateStream(sseText) {
  // merge chat.completion.chunk SSE lines into one OpenAI non-stream response
  let id = "", model = "", role = "assistant", content = "", reasoning = "",
      finish = null, usage = null;
  const toolCalls = {}; // index -> {id,name,args}
  for (const line of sseText.split("\n")) {
    if (!line.startsWith("data: ")) continue;
    const payload = line.slice(6).trim();
    if (!payload || payload === "[DONE]") continue;
    let j; try { j = JSON.parse(payload); } catch { continue; }
    if (j.usage) usage = j.usage;
    id = j.id || id; model = j.model || model;
    const ch = j.choices && j.choices[0];
    if (!ch) continue;
    const d = ch.delta || {};
    if (d.role) role = d.role;
    if (typeof d.content === "string") content += d.content;
    if (typeof d.reasoning === "string") reasoning += d.reasoning;
    if (Array.isArray(d.tool_calls)) {
      for (const tc of d.tool_calls) {
        const idx = tc.index ?? 0;
        if (!toolCalls[idx]) toolCalls[idx] = { id: tc.id || "", type: "function",
          function: { name: "", arguments: "" } };
        if (tc.id) toolCalls[idx].id = tc.id;
        if (tc.function) {
          if (tc.function.name) toolCalls[idx].function.name += tc.function.name;
          if (tc.function.arguments) toolCalls[idx].function.arguments += tc.function.arguments;
        }
      }
    }
    if (ch.finish_reason) finish = ch.finish_reason;
  }
  const message = { role, content: content || null };
  if (reasoning) message.reasoning_content = reasoning;
  const tcs = Object.keys(toolCalls).sort((a,b)=>a-b).map(k=>toolCalls[k]);
  if (tcs.length) message.tool_calls = tcs;
  const resp = { id: id || "chatcmpl-aggregated", object: "chat.completion",
    created: Math.floor(Date.now()/1000), model: model || "unknown",
    choices: [{ index: 0, message, finish_reason: finish || "stop" }] };
  if (usage) resp.usage = usage;
  return resp;
}

function sendJSON(res, status, obj) {
  const out = JSON.stringify(obj);
  res.writeHead(status, { "content-type": "application/json",
    "content-length": Buffer.byteLength(out) });
  res.end(out);
}

http.createServer((req, res) => {
  let chunks = [];
  req.on("data", (c) => chunks.push(c));
  req.on("end", () => {
    const body = Buffer.concat(chunks);
    let parsed = null;
    try { parsed = JSON.parse(body.toString()); } catch {}
    const isStream = !!(parsed && parsed.stream === true);
    const path = req.url.replace(/^\/api\/v1/, "") || "/";
    const auth = req.headers["authorization"] || "";

    if (isStream) {
      // transparent SSE passthrough
      const ureq = https.request({
        hostname: UPSTREAM, path: "/api/v1" + path, method: req.method,
        headers: { ...req.headers, host: UPSTREAM, "content-length": body.length },
      }, (ures) => {
        const sh = {};
        for (const [k, v] of Object.entries(ures.headers)) {
          const lk = k.toLowerCase();
          if (["content-encoding","transfer-encoding","content-length","connection"].includes(lk)) continue;
          sh[k] = v;
        }
        res.writeHead(ures.statusCode, sh);
        res.flushHeaders();
        ures.setEncoding("utf8");
        ures.on("data", (c) => res.write(c));
        ures.on("end", () => res.end());
        ures.on("error", () => res.end());
      });
      ureq.on("error", (e) => sendJSON(res, 502, { error: { message: "proxy error: " + e.message } }));
      ureq.end(body);
      return;
    }

    // Non-stream: convert to internal stream call, aggregate, return plain JSON.
    (async () => {
      const inner = Object.assign({}, parsed, { stream: true });
      // retry: Cline occasionally hiccups; retry up to 3 times on failure
      for (let attempt = 1; attempt <= 3; attempt++) {
        try {
          const r = await post(path, inner, { authorization: auth });
          if (r.status === 200) {
            const agg = aggregateStream(r.text);
            sendJSON(res, 200, agg);
            return;
          }
          if (attempt === 3) {
            let errObj; try { errObj = JSON.parse(r.text); } catch { errObj = { error: { message: r.text.slice(0, 300) } }; }
            sendJSON(res, r.status, errObj);
            return;
          }
        } catch (e) {
          if (attempt === 3) { sendJSON(res, 502, { error: { message: "proxy error: " + e.message } }); return; }
        }
      }
    })();
  });
}).listen(3999, "127.0.0.1", () => console.log("cline-unwrap proxy v2 on 127.0.0.1:3999"));
```

启动（常驻）：

```bash
node ~/.claude-code-router/cline-unwrap-proxy.js   # 用 nohup/systemd/supervisor 常驻
```

**写这个代理时踩过的两个坑（别再踩）：**
1. 转发响应头时必须**剔除 hop-by-hop 头**（`transfer-encoding`、`content-encoding`、
   `content-length`、`connection`），否则 Node 报"headers already sent / 冲突"，
   表现为 CCR 侧 `upstream_connect` 502。
2. 流式分支必须 `flushHeaders()` 且逐 chunk `res.write()`，不能攒齐再发——CCR 的
   Anthropic 流式转换依赖 chunk 边界。

## 4. 配置 CCR（注意：新版是 SQLite，不是 config.json！）

**新版 CCR 3.x 把配置存在 `~/.claude-code-router/config.sqlite` 的 `app_config` 表
（key='default'），`config.json` 完全不被读取。** 直接用 Python 写库：

```python
# save as /tmp/ccr_setup.py, run: python3 /tmp/ccr_setup.py
import sqlite3, json
db = sqlite3.connect('/home/YOURUSER/.claude-code-router/config.sqlite')
cfg = json.loads(db.execute("select value_json from app_config where key='default'").fetchone()[0])
cfg["Providers"] = [{
  "name": "cline",
  "api_base_url": "http://127.0.0.1:3999",   # 指向剥壳代理！不是 api.cline.bot
  "api_key": "$CLINE_KEY",
  "models": ["cline-pass/deepseek-v4.1-flash", "deepseek/deepseek-v4.1-flash"]
  # 注意：不要配 transformer！任何改请求体的 transformer 都可能破坏缓存前缀
}]
cfg["Router"]["default"] = "cline,cline-pass/deepseek-v4.1-flash"
cfg["Router"]["fallback"] = {"mode": "off", "models": [], "retryCount": 3}
db.execute("update app_config set value_json=? where key='default'", (json.dumps(cfg),))
db.commit()
print("Providers:", [p["name"] for p in cfg["Providers"]])
```

要点：
- `api_base_url` 指向剥壳代理 `http://127.0.0.1:3999`（CCR 会自动拼 `/v1/chat/completions`，
  剥壳代理会把 `/api/v1` 前缀重写回上游，两头都兼容）。
- **`models` 白名单里必须显式写上你要用的模型 ID**（包括 `cline-pass/...`）。
  否则会报 `Model "cline-pass/..." is not configured for target provider openai. Allowed models: ...` —— 这是最易忘的一步。
- **不要配 `transformer`**（如 `openrouter`）——改请求体 = 缓存前缀漂移 = 全 miss。
- 启动：`ccr start --no-open`（网关实际监听端口看 `~/.claude-code-router/service.json` 的
  `url` 字段；本机为 3456）。

**CCR 的 API key**：CCR 网关自身要求认证，key 存在同一个 SQLite 的 `api_keys` 表：

```bash
GATEWAY_KEY=$(python3 -c "import sqlite3;print(sqlite3.connect('$HOME/.claude-code-router/config.sqlite').execute(\"select encrypted_key from api_keys where name='Local Gateway'\").fetchone()[0])")
```

## 5. 接 Claude Code

首次 `ccr start` 会自动把注入段写进 `~/.claude/settings.json`，应包含：

```json
{
  "apiKeyHelper": "~/.claude-code-router/bin/ccr-claude-code-api-key-default-claude-code",
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:3456",
    "ANTHROPIC_API_BASE_URL": "http://127.0.0.1:3456",
    "NO_PROXY": "127.0.0.1,localhost,::1",
    "no_proxy": "127.0.0.1,localhost,::1"
  }
}
```

若没有自动写入，手工按上面内容合并即可。**必须设 `NO_PROXY` 含 127.0.0.1**，
否则有系统代理时本地回环会被劫持。

运行：

```bash
claude -p "任务描述" --model "cline-pass/deepseek-v4.1-flash" --dangerously-skip-permissions
# 交互式：
claude --model "cline-pass/deepseek-v4.1-flash"
```

已知提示（无害）：`[claude-code:unrecognized_model]` —— Claude Code 不认识第三方模型名，
照常工作，忽略即可。

## 6. 验证清单（全过才算通）

按顺序执行，任何一步不过先修再继续：

| # | 项目 | 方法 | 通过标准 |
|---|---|---|---|
| 1 | Cline 直连 | 第 2 步的 curl | 返回「成功」，响应含 `{"data":` 信封 |
| 2 | 剥壳代理非流式 | 对 `127.0.0.1:3999` 发同款 curl（去掉 `/api/v1` 前缀） | 返回 `{"choices":[...`，**无** `data` 包装 |
| 3 | CCR 非流式 | `POST 127.0.0.1:3456/v1/messages`（Anthropic 格式，带网关 key） | 返回标准 Anthropic `content:[{type:"text",...}]` |
| 4 | CCR 流式 | 同上 + `"stream":true` | SSE 事件里有 `content_block_delta` 且内容非空 |
| 5 | 工具调用 | 带 2+ 个 `tools` 的请求 | 返回 `tool_use` block，工具名和参数正确 |
| 6 | **KV caching** | 相同长前缀连发 2 次，看 `usage` | 第 2 次 `cache_read_input_tokens > 0`（实测 1152/1253），且 `input_tokens` 大幅下降 |
| 7 | Claude Code 真实任务 | `claude -p "写个加法函数并运行验证"` | 文件被修改、命令被执行、结果正确 |
| 8 | 计费 | 看直连响应里 `gateway.cost` | `cline-pass/` 模型为 `"0"` |

缓存验证可直接抄（第 6 项）：

```bash
BODY='{"model":"cline-pass/deepseek-v4.1-flash","max_tokens":20,"system":"'"$(python3 -c "print('Lorem ipsum dolor sit amet. '*200)")"'","messages":[{"role":"user","content":"回复两个字：好"}]}'
# 第一次: cache_read_input_tokens: 0  （冷）
# 第二次: cache_read_input_tokens: 1152, input_tokens: 101  （命中）
curl -s -X POST http://127.0.0.1:3456/v1/messages -H "content-type: application/json" \
  -H "x-api-key: $GATEWAY_KEY" -H "anthropic-version: 2023-06-01" -d "$BODY" | python3 -m json.tool | grep -E "input_tokens|cache"
```

## 7. 常驻与运维

两个进程必须活着，缺一不可：

```bash
# 剥壳代理
node ~/.claude-code-router/cline-unwrap-proxy.js
# CCR 网关
export PATH=~/.npm-global/bin:$PATH && ccr start --no-open
```

排障速查：

| 症状 | 原因 |
|---|---|
| CCR 报 `upstream_connect` 502 | 剥壳代理没起 / 响应头冲突（见第 3 步坑 1） |
| CCR 报 `response_parse: does not contain text output` | 响应的 `{"data":}` 信封没剥掉（代理没生效或路径不对） |
| CCR 报 `model_resolution: Model ... not configured` | Provider 的 `models` 白名单没包含该模型 ID |
| Cline 上游 500 `empty response content` | Cline 非流式端点不稳——用代理 v2（内部转流式聚合） |
| 流式返回空内容 | 代理流式分支没 `flushHeaders()`/逐 chunk 写 |
| 非流式 `content: null` 且 `finish_reason: length` | 推理模型思考吃光 `max_tokens`，调大到 ≥500 |
| Claude Code 报 400 All target providers failed | 多为流式链路断——先过第 4 项 |
| 缓存全 miss | 给 provider 配了 transformer，或剥壳代理改了请求体 |
| `No available models` | 新版 CCR 读 SQLite，config.json 无效（见第 4 步） |

## 8. 安全提醒

- Cline API key 全程只写进 CCR 的 SQLite 和环境变量，**不要贴进聊天/issue/截图**。
- 泄露过的 key 立即去 app.cline.bot → Settings → API Keys 撤销重发。
- 剥壳代理只监听 127.0.0.1，不要改成 0.0.0.0。
