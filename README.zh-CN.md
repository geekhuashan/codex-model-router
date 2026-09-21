# Codex Model Router

让 Codex App 和 CLI 在同一模型菜单中选择原生 ChatGPT 模型与其他 **Responses API** 模型，保留原生登录，不修改 App 文件。

这是实验性参考实现。只做模型目录、请求路由、凭据隔离和配置回滚；**不做 Anthropic Messages 或 Chat Completions 协议适配**，也不做账号池、额度轮转。

## 新模型好不好加？

同一 Responses 上游新增模型主要是配置；新的 Responses 上游还需验证能力与请求参数。提供 `/responses` 端点并不自动证明工具调用、续聊和长对话压缩兼容。

安装、初始化、添加模型、启用与回滚命令见 [英文 README](README.md#quick-start)。`add` 同时生成模型别名和路由，避免手动修改两份文件。模板元数据需要根据模型官方文档核对，尤其上下文长度、图片输入、推理档位和工具类型。

App 和 CLI 指向同一个 `CODEX_HOME` 时共用菜单和配置。模型来源由别名明确区分，例如 `example/my-model`。共用目录不代表账号额度合并。

## 兼容性检查与来源面板

```sh
codex-model-check --config "$HOME/.config/codex-model-router/router.json" --model example/my-model
codex-model-status --config "$HOME/.config/codex-model-router/router.json" --dashboard
```

添加模型时加 `--check` 可立即检查。追加 `--only tool_roundtrip` 可只复测工具一项，避免重复消耗；报告只代表所选项目。检查最多发出 7 次真实上游请求，会消耗对应额度；测试套件本身只用本机 mock。分别检查流式完成、两轮历史、工具结果回传和压缩后续聊，不执行模型生成的代码。结果区分通过、行为失败、接口不支持和网络/额度错误；429 不会被误判为不兼容。压缩测试只回传加密压缩项，失败不能直接推断所有客户端压缩方式都不可用。

来源面板每 3 秒刷新，显示来源、模型、上游主机、HTTP、生成结果和上游报告的输入/输出/缓存 Token。HTTP 200 但没有完成事件会显示“未确认完成”。记录不含聊天正文或密钥；无法知道上游账号池内部选了哪个账号，也不是实际余额或金额账单。当前进程保留最近 100 次请求，重启前历史保存在本机轮转日志中。

## 已有类似项目

| 项目 | 主要方式 | 与本项目的区别 |
| --- | --- | --- |
| [Ollama](https://github.com/ollama/ollama/blob/main/docs/integrations/chatgpt.mdx) | 合并模型目录 + 本机路由 | 最接近；主要接入 Ollama 本地和云端模型 |
| [Better Codex Custom Provider](https://github.com/Keksuccino/Better-Codex-App-Custom-Provider-Support) | 修改 App，增加 provider 菜单 | 需要补丁，更新后需重新应用 |
| [codex-merge-gateway](https://github.com/pbswimmer3/codex-merge-gateway) | 基于上述补丁接 Merge Gateway | 依赖 App 补丁 |
| [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) | 独立上游网关 | 本身不提供本项目的原生菜单集成；可作为上游 |

我们不宣称首创。目标是把已有的模型目录与本机路由模式做成小而清晰的、可配置和回滚的通用 Responses 入口。

## 边界与隐私

- 原生凭据会经过本机路由，再送到原服务；第三方请求使用单独密钥，不携带原生账号 headers。
- 日志不保存提示词、回复正文和密钥；记录 HTTP 状态不等于完整 SSE 生成成功。
- 跨来源时保留聊天正文与工具结果，过滤已知不兼容的隐藏推理。压缩上下文原样交给上游判断，不能承诺跨来源无损接续。
- 自动审批等原生内部模型仍可能消耗原账号额度。
- 通用实现不替模型厂商改写工具协议或推理参数。不支持的能力应在元数据中关闭，或由已有上游网关处理。
- 本项目不迁移用户目录，不携带登录、缓存模型目录、会话、生产配置或账号数据。初始化产生的文件仅留在本机。
- 此处没有后台服务安装器。启用期间需保持路由进程运行；停止前先 restore，再重启 Codex。

测试只使用本机 mock 和虚构凭据，不产生模型费用。具体兼容性边界见 [README](README.md#boundaries)。

## 切换模型的使用约定

- 普通对话可尝试直接切换；保留可见正文和工具结果，不承诺传递模型内部推理。
- 默认不向上游回传非空 `reasoning.content`；无加密载荷的此类推理项会被省略。有明确支持证据的路由可设置 `accepts_reasoning_content: true`。已知跨来源加密推理仍被过滤。
- 等当前工具操作完成再切换，避免未完成的工具调用干扰后续模型。
- 历史已压缩，或出现加密状态/压缩格式不兼容时，先让原模型把目标、结论、文件路径和待办写成可见交接文档，再用新任务接续。不要删除压缩记录来强行通过。
- 切换报错后先修复或回到原模型；请求失败不代表原有历史或文件被删除。
