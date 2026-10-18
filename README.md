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

## Python environment

Each machine uses its own virtualenv kept outside the project folder, so `sync.py` never copies it (it also skips any `venv` or `.venv` folder inside the project).

| Machine | Virtualenv |
| --- | --- |
| Windows | `d:\venv` |
| Ubuntu server | `/var/www/flask/venv` |

Windows (PowerShell, from the project folder):

```
py -m venv d:\venv
d:\venv\Scripts\python.exe -m pip install -r requirements.txt
d:\venv\Scripts\python.exe app.py
```

Ubuntu: `python sync.py deploy` creates `/var/www/flask/venv` (the `remote_venv` setting) when it is missing and installs `requirements.txt` into it. It reinstalls whenever `requirements.txt` changes. Add `--pip` to force the install again (for example after fixing a failed build). If creation fails because `python3-venv` is not installed, deploy offers to run `sudo apt-get install -y python3-venv` and retries. By hand, the same steps are:

```
sudo apt install -y python3-venv
python3 -m venv /var/www/flask/venv
/var/www/flask/venv/bin/pip install -r /var/www/flask/app/requirements.txt
```

`sync.py` itself uses only the standard library, so any Python 3.8+ can run it.

## Sync between machines

`sync.py` moves code and data between two machines over SSH with key login. It needs Python 3.8+ and the OpenSSH client on the machine you run it from (Linux, macOS, or Windows 10+), and `sshd` plus `python3` on the server. Nothing else is installed on the server.

### One-time SSH key setup

On the machine you will run `sync.py` from:

```
ssh-keygen -t ed25519
```

Copy the public key to the server. On Linux or macOS:

```
ssh-copy-id user@server
```

On Windows PowerShell:

```
type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh user@server "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
```

Optional but convenient: give the server a short name in `~/.ssh/config` (on Windows, `C:\Users\you\.ssh\config`):

```
Host pt-server
    HostName 192.0.2.10
    User ubuntu
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
```

Run `ssh pt-server` once by hand to accept the server's host key. `sync.py` never asks for a password, so it fails with a clear message instead of hanging if key login is not working.

### Configure

```
python sync.py init       # writes sync.config.json
```

```json
{
  "host": "pt-server",
  "port": null,
  "identity_file": "~/.ssh/id_ed25519",
  "remote_base": "/var/www/flask",
  "remote_group": "www-data",
  "remote_path": "/var/www/flask/app",
  "remote_python": "python3",
  "remote_venv": "/var/www/flask/venv",
  "data_paths": ["content"],
  "code_paths": ["app.py", "wsgi.py", "requirements.txt", "templates", "static"],
  "post_deploy": "sudo systemctl restart flask",
  "server_files": [
    {"local": "deploy/wsgi.ini", "remote": "/var/www/flask/wsgi.ini"},
    {"local": "deploy/flask.service", "remote": "/etc/systemd/system/flask.service",
     "sudo": true, "after": "sudo systemctl daemon-reload"},
    {"local": "deploy/nginx-flask.conf", "remote": "/etc/nginx/conf.d/flask.conf",
     "sudo": true, "after": "sudo nginx -t && sudo systemctl reload nginx"},
    {"local": "deploy/flask-logrotate", "remote": "/etc/logrotate.d/flask", "sudo": true}
  ]
}
```

`server_files` are files that live outside the synced app folder, such as the uwsgi ini and the systemd unit. Keep the local copies in `deploy/`. On each `deploy`, any file whose server copy differs is uploaded to `/var/www/flask/staging/` on the server (`remote_staging`; it defaults to `<remote_base>/staging`, or `~/.ptsync/staging` when `remote_base` is empty) and installed to its `remote` path (default mode `644`; add `"mode": "600"` to change it). `"sudo": true` installs it as root, which `/etc/systemd/system` needs. `after` runs once after the install, for example `sudo systemctl daemon-reload`, and `post_deploy` then restarts the service. The previous server copy is saved under `/var/www/flask/staging/.backup/<timestamp>/`. The staging folder is created by `deploy` with mode 700, and a staged file is refused if it is a symlink or not owned by your login. File names must be unique. `python sync.py test` prints the exact sudoers line to allow the install without a password:

```
edoha ALL=(root) NOPASSWD: /usr/bin/install -m 644 -o root -g root /var/www/flask/staging/flask.service /etc/systemd/system/flask.service
edoha ALL=(root) NOPASSWD: /usr/bin/systemctl daemon-reload
edoha ALL=(root) NOPASSWD: /usr/bin/systemctl restart flask
```

`host` can be an alias from `~/.ssh/config` or `user@address`. `remote_path` is the folder on the server that holds the app. `remote_base` is optional. When it is set (for example `/var/www/flask`) and your login cannot write to the server folders, `deploy` and `data` create `remote_base` and `remote_path` with `sudo`, hand them to your login user, and give them the `remote_group` group (`www-data`, which nginx and uwsgi normally run as) so they can read the files. It shows the exact command and asks before running it, and tries passwordless sudo first, then asks for your sudo password in your terminal. It refuses system folders such as `/var/www` itself, and `remote_path` must sit inside `remote_base`. If you use an alias, `port` and `identity_file` can stay empty. `remote_venv` is only used to print the right `pip` command after a deploy changes `requirements.txt`. `post_deploy` is a command that runs on the server after a deploy, for example to reload the app server.

### Commands

| Command | What it does |
| --- | --- |
| `python sync.py check` | Tests key login, the server's Python and the remote folder |
| `python sync.py test` | Full check of `sync.config.json`: config values, key format, local paths, key login, server folders, `www-data` group, venv contents, passwordless `sudo` for `post_deploy`, and that the systemd service exists. Prints OK / WARN / FAIL for each and exits 1 on any FAIL |
| `python sync.py status` | Previews everything `data` and `deploy` would change |
| `python sync.py data` | Two-way sync of `data_paths` (the searchable content) |
| `python sync.py deploy` | Pushes `code_paths` to the server, then runs `post_deploy` |

Every command shows its plan first and asks before changing anything. Add `-n` to preview only, or `-y` to skip the question in scripts.

### How data sync decides

The tool remembers what both sides held after the last sync (in `.sync-state.json`) and compares each file against that.

- Changed on one side only: it is copied to the other side.
- Changed on both sides: it is reported as a conflict and nothing is overwritten. Choose a winner with `--prefer local` or `--prefer remote`.
- Forcing it: `python sync.py data --force local` makes the server match your PC, and `--force remote` does the reverse. Sync history is ignored, so use it when the state file is stale or you just want one side to win. Every differing file is copied from the winner, files that exist only on the other side are kept unless you add `--delete`, and overwritten files are backed up. `python sync.py deploy --force` re-sends every code file even when the server copy looks identical.
- Deleted on one side: kept by default. Add `--delete` to carry deletions across.
- Any file that gets overwritten or deleted is first copied to `.sync-backup/<timestamp>/` on the side that changed.

Because the content lives in a single `entries.json`, two people editing different entries still count as a conflict. Splitting it into one file per entry would remove most of those.

## Add content

Everything searchable lives in `content/entries.json`. Each entry has a title, phase, noise level, tags, summary and commands, plus optional notes and a caution. Edits show up on the next request with no restart. The full format is documented in `.claude/skills/pt-help-web/SKILL.md`.

## Claude skill

This repo includes a `/pt-help-web` skill for Claude Code under `.claude/skills/`. It knows how to start the site, test it and add entries.

## Layout

```
app.py                Flask app, search API and scoring
sync.py               SSH sync and deploy tool
content/entries.json  Searchable content
templates/index.html  Main page
static/               style.css and app.js
```
