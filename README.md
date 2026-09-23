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

## Existing tasks pinned to another provider

**A model display name is not proof of its provider.** Old tasks and child tasks can retain their original `model_provider`; choosing a native model name may still call the old upstream and bypass this router entirely. The dashboard cannot see bypass traffic.

Explicitly bridge an old custom provider through the same local router:

```sh
codex-model-legacy enable --codex-home "$HOME/.codex" \
  --state-dir "$HOME/.config/codex-model-router" --provider YOUR_OLD_PROVIDER_ID
```

The provider ID remains usable by existing tasks, but its endpoint becomes this router and its authentication uses the native login. The chosen model alias then selects the actual upstream and credentials. This changes the meaning of unprefixed native model IDs under that old provider: they now use ChatGPT/OpenAI, while `provider/model` aliases use configured custom routes. Only the explicitly named provider is changed. Native sign-in is required for this bridge.

Stop active tasks and restart **all App and CLI processes** after installing: editing config cannot update provider objects already loaded in memory, including existing child agents. Do not assume an ongoing turn switches providers midway. Confirm the first new request in the dashboard. Other custom providers, explicit endpoint overrides, and other API clients remain outside this router.

Backups/receipts stay private on the user's machine. Credential references that still read the provider being replaced must be migrated first; the command refuses to break them. To undo, run the same command with `restore` instead of `enable`, then restart. Restore legacy bridges **before** restoring the main router configuration or stopping the service. No task history or database is rewritten.

## Adding more models

- Same Responses upstream, new model: add another alias and its correct capability metadata.
- New Responses upstream: add a route and a credential reference, then validate streaming, tools, follow-up turns and context compaction.
- Chat Completions or Anthropic-only upstream: not supported by this project.

Routes reload between requests; active streams keep their original route. Changes to the listening port, local token or state database still require a router restart. Codex currently caches the model catalog in its backend: **restart App/CLI after catalog changes**. The refresh service never restarts Codex or changes a task's selected model.

### Optional catalog refresh

Configure explicit discovery sources in your private `router.json` (disabled unless `refresh.enabled` is true):

```json
"refresh": {
  "enabled": true,
  "interval_seconds": 21600,
  "exclude": [],
  "native": {
    "url": "https://chatgpt.com/backend-api/codex/models",
    "auth_file": "/absolute/path/to/.codex/auth.json",
    "client_version": "YOUR_INSTALLED_CODEX_VERSION",
    "label_prefix": "Pro · "
  },
  "providers": [
    {"route_template": "example/existing-gpt-model", "prefix": "example"}
  ]
}
```

Start/restart the router once to install this feature. It checks on startup and every six hours by default. Only selected providers are queried, using the credential reference of an existing route. Refresh uses catalog GET requests, never inference probes. For a manual check or the last local report:

```sh
codex-model-refresh --config "$HOME/.config/codex-model-router/router.json"
codex-model-refresh --config "$HOME/.config/codex-model-router/router.json" --status
```

Native discovery uses the signed-in account's official catalog, not the shared models cache. Third-party IDs are added only when they match metadata freshly returned by the native catalog; matching IDs are not proof of upstream inference compatibility. Other IDs are reported as pending for manual capability configuration. This does not add protocol adapters. Existing model entries, custom overrides, routes and credentials are preserved; refresh is additive, so provider disappearance never removes a model. Put IDs/aliases in `exclude` to prevent re-discovery of manually removed models. Failed sources retain their previous catalog; status appears in the local dashboard and `refresh-status.json`. No account ownership or pool internals are inspected.

The official account catalog endpoint is an implementation detail and can change. Authentication or format failures are reported without clearing existing models. The restart indicator records that a catalog update occurred; it does not inspect whether every App/CLI process has since restarted.

## Compatibility checks

Run a model check explicitly (up to seven short **billable upstream requests**):

```sh
codex-model-check --config "$HOME/.config/codex-model-router/router.json" \
  --model example/my-model --json-report "$HOME/model-check.json"
```

To rerun only one capability, append `--only tool_roundtrip` (or `streaming`, `visible_history`, `compaction`); the report covers only the selected checks.

Or append `--check` to `codex-model-config add` to check immediately after adding. Failed checks keep the model entry for correction; the command returns nonzero. Reports distinguish `pass`, `fail`, `unsupported`, and `error`; a 429 is a rate limit, not proof of protocol incompatibility. Each request has a timeout and ordinary responses cap output at 512 tokens.

Checks cover streamed text plus completion, visible history over two turns, a function call followed by a random tool-result challenge, and opaque compaction followed by recall. No model-generated code is executed. Compaction recall deliberately excludes plaintext replay: failure means this strict opaque-only test did not pass, not necessarily that all client compaction workflows are unusable. These are bounded capability probes, not certification of every Codex tool, long context or cross-provider continuation.

## See where requests went

```sh
codex-model-status --config "$HOME/.config/codex-model-router/router.json"
codex-model-status --config "$HOME/.config/codex-model-router/router.json" --dashboard
```

The local, read-only dashboard refreshes every three seconds. It shows current and recent requests: UTC time, source, selected alias, upstream hostname, operation, HTTP status, generation outcome and upstream-reported input/output/cached tokens. HTTP 200 without a completion event is shown as **unconfirmed**. Failed/incomplete streams and interrupted connections are distinct.

The health endpoint exposes the same metadata with `--json`. Recent records and totals are limited to this process lifetime; rotated `activity.log` files retain start/finish records joined by request ID. No prompts, response text, headers, keys or complete upstream URLs are recorded. Treat the local dashboard URL as private. An upstream gateway's internal account selection and remaining account balance cannot be inferred from this router.

## Boundaries

- Binds only to `127.0.0.1`. The local URL includes a random token. Browser-origin requests are rejected; do not expose this listener to the network.
- Native requests, including the ChatGPT credential, pass through this **local** process before going to the original upstream. The router does not log prompts, headers, API keys or response text.
- Third-party credentials are isolated. Cookies are not retained, redirects are not followed, and unknown models never silently fall back to another billing source.
- WebSocket requests get HTTP 426 so supported Codex clients fall back to HTTP/SSE. Streaming is forwarded without automatic retries or fallback models.
- By default, nonempty `reasoning.content` is omitted; reasoning items without an encrypted payload are dropped. A route with verified support can set `accepts_reasoning_content: true`. Visible messages and tool records are preserved. Different providers cannot generally decode each other's encrypted reasoning. Known foreign reasoning items are omitted while visible messages and tool results remain. Compaction payloads and `previous_response_id` are preserved for upstream validation; cross-provider continuation is **not guaranteed**, especially after compaction. This is not a lossless transfer of hidden reasoning.
- Internal account-specific models, including automatic review, still use the native route. Selecting a custom conversational model does not guarantee zero native-account usage.
- No conversion of provider-specific tools or parameters. Context compaction, multimodal input and advanced agent tools depend on both the client and upstream.
- `activity.log` records both upstream HTTP status and observed completion state; reported tokens are not an account balance or monetary bill.
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
