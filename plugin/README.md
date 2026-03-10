# open-brain Claude Code Plugin

Automatic memory capture for Claude Code sessions. Observations from tool use and session summaries are sent to an open-brain server for persistent, searchable storage.

## What It Does

- **PostToolUse**: Captures observations from Edit, Write, Bash, and Agent tool uses
- **Stop / SubagentStop**: Generates session summaries when sessions or subagents complete
- **SessionStart**: Injects recent memory context into new sessions
- **Search**: Skill for searching past observations and session summaries

## Installation

### 1. Install the plugin

```bash
# From the open-brain repo
claude --plugin-dir /path/to/open-brain/plugin

# Or via plugin install (if published)
# claude plugin install open-brain
```

### 2. Configure the server connection

```bash
python3 /path/to/open-brain/plugin/scripts/setup.py
```

This prompts for:
- **Server URL**: Your open-brain server (e.g., `https://brain.example.com`)
- **API Key**: An `ob_`-prefixed API key configured on the server

Configuration is saved to `~/.open-brain/config.json`.

### 3. Verify

Start a new Claude Code session. You should see:
- Context injection on startup (if there are previous memories)
- Observation capture as you use tools
- Session summary on exit

## Configuration

Config file: `~/.open-brain/config.json`

```json
{
  "server_url": "https://brain.example.com",
  "api_key": "ob_your_api_key_here",
  "project": "auto",
  "skip_tools": ["Read", "Glob", "Grep", "Skill", "ToolSearch", "AskUserQuestion"],
  "context_limit": 50,
  "bash_output_max_kb": 10,
  "log_level": "INFO"
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `server_url` | `""` | open-brain server URL (required) |
| `api_key` | `""` | API key for authentication |
| `project` | `"auto"` | Project name. `"auto"` detects from git remote. |
| `skip_tools` | (see above) | Tools that don't trigger observation capture |
| `context_limit` | `50` | Max observations injected on SessionStart |
| `bash_output_max_kb` | `10` | Skip Bash observations with output > N KB |
| `log_level` | `"INFO"` | Logging level |

## Server Setup

The plugin requires an open-brain server with API key authentication enabled.

### Enable API keys on the server

Add `API_KEYS` to your server environment (or `.env.tpl`):

```bash
API_KEYS=ob_your_key_here,ob_another_key
```

Keys must have the `ob_` prefix.

### Server endpoints used by the plugin

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/ingest` | POST | Receive tool observations |
| `/api/summarize` | POST | Generate session summaries |
| `/api/context` | GET | Fetch recent context for injection |
| `/health` | GET | Health check |

## Troubleshooting

### Plugin not capturing observations

1. Check config exists: `cat ~/.open-brain/config.json`
2. Check server is reachable: `curl <server_url>/health`
3. Check hook log: `tail ~/.open-brain/hook-log.jsonl`

### No context injected on startup

1. Verify there are memories: `curl -H "X-API-Key: <key>" "<server_url>/api/context?project=<name>"`
2. Check the plugin is loaded: look for hook output in Claude Code startup

### Server returns 401

- Verify your API key is in the server's `API_KEYS` environment variable
- Keys must have the `ob_` prefix
