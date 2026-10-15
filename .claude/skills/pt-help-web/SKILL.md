---
name: pt-help-web
description: Run, preview and extend the PT-Helper Flask web service, a searchable reference site for configuration review, vulnerability analysis and penetration test methodology and commands. Use whenever the user types /pt-help-web, or asks to start, restart, stop, preview or change the PT-Helper website, add or edit its pages or search content, or add new methodology entries or commands, even if they don't name the skill.
---

# pt-help-web

A small Flask site that gives pentesters one searchable place for methodology steps, commands and configuration checks. The app lives at the repository root, two levels above this skill folder. The first version has a single main page with live search.

## What is here

```
app.py                  Flask app: / (page), /api/search, /healthz, search scoring
requirements.txt        Flask only
content/entries.json    All searchable content (edit this to add material)
templates/index.html    Main page shell
static/style.css        Design tokens and layout
static/app.js           Live search, phase filter, copy buttons, $TARGET substitution
.claude/skills/pt-help-web/SKILL.md   This file
```

## Running it

1. Install dependency if needed: `pip install -r requirements.txt` (add `--break-system-packages` when pip refuses).
2. Check the port is free, then start in the background so the shell stays usable:
   `cd <repo-root> && nohup python3 app.py > /tmp/pt-help-web.log 2>&1 &`
3. Confirm with `curl -s http://127.0.0.1:5055/healthz`. It returns `{"status":"ok","entries":N}`.
4. Tell the user the address. The default is `http://127.0.0.1:5055`.

Settings come from environment variables: `PT_HELP_HOST` (default `127.0.0.1`), `PT_HELP_PORT` (default `5055`), `PT_HELP_DEBUG=1` for the reloader.

Keep the host on loopback unless the user explicitly asks otherwise. The site holds attack commands and has no login, so exposing it to a network is a decision for the user to make, and worth a one-line warning when they do.

To stop it: `pkill -f "python3 app.py"`, or find the PID first if other Python apps may be running.

## Adding content

Every entry in `content/entries.json` has this shape. Edits appear on the next request with no restart, because `app.py` reloads the file when it changes.

```json
{
  "id": "unique-slug",
  "title": "Plain description of the task",
  "phase": "recon | enumeration | vuln-analysis | exploitation | post-exploitation | config-review",
  "noise": "quiet | noticeable | loud",
  "tags": ["tool", "service", "port number"],
  "summary": "One or two sentences on when and why.",
  "commands": [{"label": "What this variant does", "cmd": "tool $TARGET"}],
  "notes": "Optional. Reporting or remediation hint.",
  "caution": "Optional. Risk to the target or the engagement."
}
```

Guidance for writing entries:

- Write the title as the task the tester is trying to do ("Enumerate SMB shares and users"), because that is what they will type into search.
- Put tool names, service names and port numbers in `tags`. Tag hits rank almost as high as title hits.
- Use `$TARGET` for the host. The page swaps it for the value in the Target field. Use literal example values (`10.10.10.0/24`, `example.local`) for anything else.
- `noise` is detection likelihood, not danger: quiet means mostly local or passive, noticeable means a normal scan footprint, loud means heavy traffic that logs and IDS will catch.
- Use `caution` when a step can lock accounts, crash services or fall outside a typical scope. It gets a visible callout.
- Keep to widely documented, authorized-testing methodology. Entries should not contain real credentials, real hostnames or client data.

Phases are defined in `PHASES` in `app.py`. A new phase needs an entry there and at least one entry using its id.

## Search behavior

The query is split into words. Every word must match somewhere (exact or prefix), and results are ranked by where it matched: title 6, tags 5, phase name 3, summary 2, commands 2, notes 1. An empty query lists everything in phase order. If results feel wrong, adjust `FIELD_WEIGHTS` in `app.py` rather than special-casing queries.

## Design

Keep the look modern and professional; the search box is the one prominent element and everything else stays quiet. Colors and radii are tokens at the top of `static/style.css`.

- Petrol sidebar and code blocks, cool grey canvas, teal accent, amber search highlight.
- Noise levels use green, ochre and red, kept separate from the brand teal so they read as status.
- System font stacks only. The page makes no external requests, and the Content-Security-Policy in `app.py` allows only same-origin scripts and styles, so do not add inline `<script>`, `<style>` or `style=""` attributes.
- Results are built with `textContent` and text nodes. Keep it that way so entry content can never inject markup.

## Likely next steps

- Entry detail pages (`/entry/<id>`) with shareable links
- Per-service cheat sheets and checklists that combine several entries
- A report-snippet field on each entry for findings and remediation text
