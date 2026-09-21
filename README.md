# Codex Model Router

Use one model menu in Codex App and Codex CLI for your existing ChatGPT account and additional **Responses API** endpoints. No App bundle patch, account pooling, or protocol conversion.

**Experimental reference implementation.** The underlying catalog + loopback approach was exercised with Codex CLI 0.155.1 and a desktop backend 0.155.0-alpha.9.2. This portable package is tested with local mock upstreams; it does not certify every model or Codex version.

[中文说明](README.zh-CN.md)

## How it works

```text
Codex App / CLI — shared model_catalog_json
                         |
               loopback Responses router
                         |
            +------------+--------------+
            |                           |
       native models               provider/model
            |                           |
   ChatGPT / OpenAI upstream       configured Responses API
```

The built-in `openai` provider remains selected. `openai_base_url` points to a local router; a combined catalog adds unique model aliases such as `example/my-model`. Native requests retain their existing authentication. Custom requests receive only their own API key and a small header allowlist.

This is a **model router**, not a universal LLM gateway. Endpoints must already support Responses, including the tool and streaming behavior your selected model needs. Anthropic Messages and Chat Completions adapters are out of scope. An existing Responses-compatible gateway may be used as an upstream.

## Quick start

Python 3.11+ is required. Start Codex and sign in normally first, so its native model cache exists.

```sh
git clone https://github.com/geekhuashan/codex-model-router.git
cd codex-model-router
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'

codex-model-config init \
  --codex-home "$HOME/.codex" \
  --state-dir "$HOME/.config/codex-model-router"
```

Add a model from an existing Responses endpoint. Replace the placeholders; use a native model ID from your cached catalog as a metadata template:

```sh
codex-model-config add \
  --state-dir "$HOME/.config/codex-model-router" \
  --name example \
  --base-url https://api.example.com/v1 \
  --model my-model \
  --label 'Example · My model' \
  --api-key-env EXAMPLE_API_KEY \
  --template YOUR_NATIVE_MODEL_ID
```

Cloning a template is only a starting point. Check context length, modalities, reasoning levels and tool capabilities in the generated `models.json` against the upstream's own documentation. Adding a name to the menu does **not** prove agent compatibility.

Provide the key to the router process through its environment or configure a private key-file reference. No key is written into the catalog. Do not paste real keys into shell history or commit runtime state.

Run the router in a separate terminal with that environment:

```sh
codex-model-router --config "$HOME/.config/codex-model-router/router.json"
```

Then enable the shared configuration:

```sh
codex-model-config enable \
  --codex-home "$HOME/.codex" \
  --state-dir "$HOME/.config/codex-model-router"
```

Restart Codex App or CLI. Select the alias in the model menu, use `/model` in CLI, or launch `codex -m example/my-model`.

To undo the configuration changes:

```sh
codex-model-config restore \
  --codex-home "$HOME/.codex" \
  --state-dir "$HOME/.config/codex-model-router"
```

Restore before stopping the router, then restart Codex. No login file, task database or session is replaced. Enable/restore only manage the routing/catalog/provider root settings, and refuse to overwrite conflicting later edits. The example does not install a background service; keep the router running while Codex uses it.

## Adding more models

- Same Responses upstream, new model: add another alias and its correct capability metadata.
- New Responses upstream: add a route and a credential reference, then validate streaming, tools, follow-up turns and context compaction.
- Chat Completions or Anthropic-only upstream: not supported by this project.

Routing and catalog files are loaded when their respective processes start. Restart the router and Codex after configuration changes. Native models are an initialization-time cache snapshot; new native model releases are not automatically imported.

## Boundaries

- Binds only to `127.0.0.1`. The local URL includes a random token. Browser-origin requests are rejected; do not expose this listener to the network.
- Native requests, including the ChatGPT credential, pass through this **local** process before going to the original upstream. The router does not log prompts, headers, API keys or response text.
- Third-party credentials are isolated. Cookies are not retained, redirects are not followed, and unknown models never silently fall back to another billing source.
- WebSocket requests get HTTP 426 so supported Codex clients fall back to HTTP/SSE. Streaming is forwarded without automatic retries or fallback models.
- Different providers cannot generally decode each other's encrypted reasoning. Known foreign reasoning items are omitted while visible messages and tool results remain. Compaction payloads and `previous_response_id` are preserved for upstream validation; cross-provider continuation is **not guaranteed**, especially after compaction. This is not a lossless transfer of hidden reasoning.
- Internal account-specific models, including automatic review, still use the native route. Selecting a custom conversational model does not guarantee zero native-account usage.
- No conversion of provider-specific tools or parameters. Context compaction, multimodal input and advanced agent tools depend on both the client and upstream.
- `activity.log` records upstream HTTP status, not a guarantee that an SSE response completed successfully.
- This uses evolving client configuration, not an official extension API. It is not affiliated with or endorsed by OpenAI or Ollama.

## Tests

```sh
pytest -q
```

Tests use ephemeral local upstreams and synthetic credentials. They cover request routing, header isolation, zstd, HTTP fallback, streaming, visible-history preservation, opaque-state forwarding, configuration creation and rollback. They do not spend model credits.

## Related work

- [Ollama's Codex/Desktop integration](https://github.com/ollama/ollama/blob/main/docs/integrations/chatgpt.mdx): inspiration for the combined catalog and loopback routing design; supports Ollama local/cloud models alongside native models.
- [Better Codex App Custom Provider Support](https://github.com/Keksuccino/Better-Codex-App-Custom-Provider-Support): modifies the App to expose provider selection.
- [codex-merge-gateway](https://github.com/pbswimmer3/codex-merge-gateway): Merge Gateway integration using the provider patch.
- [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI): a separate gateway that can serve as a Responses upstream.

The catalog/router concept is existing work. This repository packages a small generic Responses-only implementation with explicit configuration and rollback. It does not copy Ollama's implementation or ship model catalogs, account data or personal migrations.
