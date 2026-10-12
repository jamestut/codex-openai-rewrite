# Responses API Proxy

A small Python reverse proxy for the OpenAI Responses API. Requests pass through unchanged unless an enabled plugin matches them. The included title plugin simplifies matching task-title requests.

## Requirements

Python 3.9 or later. No third-party packages are required.

## Start

Create the local config file from the example, then start the proxy with your upstream URL:

```sh
cp config.example.json config.json
./responses_proxy.py --upstream https://api.openai.com
```

## Built-in plugins

- **`codex_gui_titlegen`** streamlines automatic task-title generation from Codex GUI.

## Plugins and reload

`config.json` contains an ordered list of enabled plugins. The example enables `codex_gui_titlegen`; use an empty list to disable request replacements.

After editing the config, reload the running proxy by passing the startup PID:

```sh
./responses_proxy.py reload 12345
```

Replace `12345` with the printed PID. The command returns as soon as it sends the reload signal.

Note: HTTPS upstream certificate verification is disabled.
