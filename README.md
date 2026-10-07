# KeyVault — 本地 API Key 管理与额度查询

统一管理各厂商 LLM API Key，一键查询余额。数据 AES 加密存本地，不上传任何服务器。

## 支持厂商
- **余额查询**：DeepSeek / Moonshot(Kimi) / OpenRouter / Together AI / OpenAI
- **Key 有效性验证**：小米 MiMo / 自定义 OpenAI 兼容接口
- **控制台链接**：Cline / 智谱 GLM / SiliconFlow（官方无余额接口）

## 启动
```bash
python3 server.py          # 默认 8787 端口
python3 server.py 9000     # 自定义端口
```
浏览器打开 http://127.0.0.1:8787

## 说明
- 纯 Python 标准库，无需安装依赖（Python 3.8+）
- Key 加密保存在同目录 `keys.enc`（已加入 .gitignore，不会被提交）
