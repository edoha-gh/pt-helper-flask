---
name: pt-help-web
description: Run, preview and extend the PT-Helper Flask web service, a searchable reference site for configuration review, vulnerability analysis and penetration test methodology and commands. Use whenever the user types /pt-help-web, or asks to start, restart, stop, preview or change the PT-Helper website, add or edit its pages or search content, or add new methodology entries or commands, even if they don't name the skill.
---

# pt-help-web

A small Flask site that gives pentesters one searchable place for methodology steps, commands and configuration checks. The app lives at the repository root, two levels above this skill folder. The first version has a single main page with live search.

## What is here

```
app.py                  Flask app: / (page), /api/search, /healthz, search scoring
wsgi.py                 uwsgi entry point (module wsgi, callable app)
requirements.txt        wheel, uwsgi, Flask
sync.py                 SSH sync and deploy tool (see Syncing between machines)
sync.config.json        Per-machine settings (git-ignored, written by `sync.py init`)
content/entries.json    All searchable content (edit this to add material)
templates/index.html    Main page shell
static/style.css        Design tokens and layout
static/app.js           Live search, phase filter, copy buttons, $TARGET substitution
deploy/wsgi.ini         uwsgi settings, installed to /var/www/flask/wsgi.ini
deploy/flask.service    systemd unit, installed to /etc/systemd/system/flask.service
.claude/skills/pt-help-web/SKILL.md   This file
```

## Running it

1. Install dependency if needed: `pip install -r requirements.txt` (add `--break-system-packages` when pip refuses).
2. Check the port is free, then start in the background so the shell stays usable:
   `cd <repo-root> && nohup python3 app.py > /tmp/pt-help-web.log 2>&1 &`
3. Confirm with `curl -s http://127.0.0.1:5055/healthz`. It returns `{"status":"ok","entries":N}`.
4. Tell the user the address. The default is `http://127.0.0.1:5055`.

The user's virtualenvs are `d:\venv` on Windows and `/var/www/flask/venv` on the Ubuntu server (app in `/var/www/flask/app`). Use those interpreters instead of system Python, and keep venvs outside the project folder.

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

## Syncing between machines

`sync.py` (repo root) moves code and data to and from a server over SSH with key login only. Full setup, config and the conflict rules are in `README.md`. Claude's own workspace cannot reach the user's servers, so run it only where the user has the SSH key, or tell the user which command to run.

### Commands

- `python sync.py init` writes a starter `sync.config.json`.
- `python sync.py check` is the quick test of key login, the server's Python and the remote folder.
- `python sync.py test` checks the whole config: values, key format (catches a PuTTY .ppk), local paths, key login, server folders and `www-data` group, the venv (Flask, uwsgi), passwordless sudo for `post_deploy` and server files, and that the systemd unit exists. Each line is OK, WARN or FAIL, and any FAIL exits 1. Run it first when anything fails.
- `python sync.py status` previews data and code changes. Show the user the plan before `data` or `deploy`.
- `python sync.py data` syncs `content/` both ways using the saved state in `.sync-state.json`. Conflicts are never overwritten; the user picks `--prefer local` or `--prefer remote`. Deletions only travel with `--delete`. `--force local|remote` ignores the saved state and makes the other side match the winner (differing files copied, extras kept unless `--delete`, overwritten files backed up). A "deleted on the server, kept here" message means a stale `.sync-state.json`; use `--force local` rather than `--delete`.
- `python sync.py deploy` pushes `code_paths` one way, then in this order: installs `server_files`, creates the venv if `remote_venv` is set and missing (offers `sudo apt-get install python3-venv` if needed), runs `pip install -r requirements.txt` when the venv is new or requirements changed, then runs `post_deploy`. Options: `-n` preview, `--force` re-send every code file and server file, `--pip` reinstall requirements, `--delete` remove server files not present locally, `--no-hook` skip `post_deploy`.

### Settings in sync.config.json

`host`, `port`, `identity_file`, `remote_base` (`/var/www/flask`), `remote_group` (`www-data`), `remote_path` (`/var/www/flask/app`), `remote_python`, `remote_venv` (`/var/www/flask/venv`), `remote_staging` (defaults to `<remote_base>/staging`), `data_paths`, `code_paths` (must include `wsgi.py`), `post_deploy` (`sudo systemctl restart flask`) and `server_files`:

```json
"server_files": [
  {"local": "deploy/wsgi.ini", "remote": "/var/www/flask/wsgi.ini"},
  {"local": "deploy/flask.service", "remote": "/etc/systemd/system/flask.service",
   "sudo": true, "after": "sudo systemctl daemon-reload"}
]
```

A server file is uploaded to the staging folder (mode 700) and installed to `remote` only when its server copy differs. `"sudo": true` installs it root-owned, `mode` defaults to `644`, `after` runs once after the install, and the old copy is backed up under `<remote_staging>/.backup/<timestamp>/`. File names must be unique.

The config is git-ignored and holds no secrets; the key stays in the user's `~/.ssh`. Do not hand-edit `.sync-state.json`. Anything overwritten or deleted by `data` is backed up under `.sync-backup/<timestamp>/` on the side that changed. A new folder goes in `data_paths` (edited on both sides) or `code_paths` (pushed from the dev machine only), never both.

### Server layout and permissions

- `/var/www` is owned by `edoha:www-data` with setgid `2775` directories, so the login can write without sudo and nginx/uwsgi can read.
- Sudo rules are exact-argument matches (wildcards would be root-equivalent). Keep them in `/etc/sudoers.d/` and edit with `visudo -f`: `www` (the folder-setup `mkdir -p`, `chmod 2775` and `chown -R edoha:www-data`), `flask-service` (`systemctl enable|start|stop|restart|reload|status flask`) and `z_edoha` (the exact `install -m 644 -o root -g root <staging>/flask.service /etc/systemd/system/flask.service` plus `systemctl daemon-reload`). `enable --now` is not covered by `enable flask`.
- Packages for the uwsgi build: `python3-venv build-essential python3-dev libcrypt-dev`.
- First deploy order: `python sync.py test`, `deploy`, `data --force local`, then once on the server `sudo systemctl enable flask` and `sudo systemctl start flask`. Later deploys restart the service through `post_deploy`.
- Known risk: `/var/www/flask` is group-writable by `www-data`, so a compromised web app could replace a staged file before it is installed as root. Mitigate with `chmod 2755` on the base and app folders (and the matching sudoers line), or set `remote_staging` to `~/.ptsync/staging`.
- The uwsgi service uses `socket = 127.0.0.1:5055` (uwsgi protocol), so nginx needs `include uwsgi_params; uwsgi_pass 127.0.0.1:5055;`. Nginx settings are still to be added.
- The step-by-step checklist is saved in the PT-HELPER project as `claude/pre-deployment.md`.

## Likely next steps

- Entry detail pages (`/entry/<id>`) with shareable links
- Per-service cheat sheets and checklists that combine several entries
- A report-snippet field on each entry for findings and remediation text
