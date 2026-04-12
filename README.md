# 🌉 Context Bridge

**Transfer AI session context between coding IDEs — never re-explain your project again.**

Context Bridge is a beautiful, interactive CLI tool that extracts conversation context and
project state from one AI coding IDE and imports it into another. Switch between Codex CLI,
Cursor, Antigravity, and Claude Code without losing your conversation history, file context,
or project understanding.

## Features

- **🔍 Auto-Detection** — Scans your machine for installed AI coding IDEs
- **📦 Universal Export** — Extracts sessions into a portable `bridge-session.json` format
- **🔄 Cross-IDE Import** — Generates target-specific artifacts for any supported IDE
- **🛡️ Safety-First** — Read-only access, automatic redaction of secrets, SHA-256 provenance
- **🎨 Beautiful CLI** — Rich panels, tables, spinners, and progress bars
- **🖥️ Full TUI Mode** — Interactive terminal UI with Textual

## Supported IDEs

| IDE | Extract | Import | Status |
|-----|---------|--------|--------|
| OpenAI Codex CLI | ✅ | ✅ | Full support |
| Cursor | ✅ | ✅ | Full support |
| Antigravity | ⚠️ | ✅ | Discovery mode (Extract is limited due to encrypted `.pb` storage) |
| Claude Code | ✅ | ⚠️ | Partial |

> **Note on Antigravity:** While you can extract conversation metadata (titles, timestamps) from Antigravity, extracting raw message text is currently restricted because Antigravity securely encrypts its proprietary `.pb` (Protobuf) session blobs locally. Context Bridge is best used to push Context **into** Antigravity.

## Quick Start

```bash
# Install
pip install -e .

# Detect installed IDEs
cb detect

# Scan sessions from an IDE
cb scan --ide codex

# One-shot bridge between IDEs
cb bridge --from codex --to cursor --session <SESSION_ID>
```

## Commands

| Command | Description |
|---------|-------------|
| `cb detect` | Scan machine for installed AI coding IDEs |
| `cb scan` | List sessions found for a specific IDE |
| `cb inspect` | Show full detail of one session |
| `cb export` | Export a session to bridge-session.json |
| `cb import` | Import a bridge session into target IDE format |
| `cb bridge` | One-shot extract → normalize → import pipeline |
| `cb forensics` | Deep diagnostic dump of IDE storage |
| `cb tui` | Launch interactive terminal UI |

## Safety

- All IDE databases opened in **read-only + immutable** mode
- API keys, tokens, and secrets are **auto-redacted** on export
- SHA-256 hashes verify source artifact integrity
- Output always goes to user-specified directories — never writes to IDE storage

## Requirements

- Python 3.11+
- Windows (primary), macOS/Linux (secondary)

## License

MIT
