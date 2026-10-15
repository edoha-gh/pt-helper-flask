# PT Helper

A small Flask site with a searchable reference for configuration review, vulnerability analysis and penetration test methodology and commands.

Search by tool, service, port or technique. Filter by phase, set a target once to fill in `$TARGET` in every command, and copy commands with one click.

For systems you are authorized to test.

## Run it

```
pip install -r requirements.txt
python3 app.py
```

Open http://127.0.0.1:5055.

| Variable | Default | Purpose |
| --- | --- | --- |
| `PT_HELP_HOST` | `127.0.0.1` | Interface to listen on. The site has no login, so keep it on loopback unless you add authentication. |
| `PT_HELP_PORT` | `5055` | Port |
| `PT_HELP_DEBUG` | off | Set to `1` for the Flask reloader |

## Add content

Everything searchable lives in `content/entries.json`. Each entry has a title, phase, noise level, tags, summary and commands, plus optional notes and a caution. Edits show up on the next request with no restart. The full format is documented in `.claude/skills/pt-help-web/SKILL.md`.

## Claude skill

This repo includes a `/pt-help-web` skill for Claude Code under `.claude/skills/`. It knows how to start the site, test it and add entries.

## Layout

```
app.py                Flask app, search API and scoring
content/entries.json  Searchable content
templates/index.html  Main page
static/               style.css and app.js
```
