# Contributing

This is a public repository with real deployment tooling behind it. The rules below exist
because of one hard constraint: nothing checked in here can ever contain a real credential,
hostname, or identifier.

## Setup: enable the secret-scan hook

Do this once, right after cloning, before your first commit:

```bash
git config core.hooksPath .githooks
```

Git does not install hooks from a clone by default — `.githooks/` is tracked in this repo,
but it only runs if you point Git at it yourself. This installs a pre-commit hook
(`scripts/scan-secrets.sh`) that scans staged changes for things that look like secrets
(tokens, keys, webhook URLs, IPs, etc.) and blocks the commit if it finds one.

The hook fails closed: if it cannot first confirm it's actually working — by detecting a
planted test canary — it refuses to run at all rather than silently letting the commit
through. A broken or misconfigured scanner can never report "clean" by accident. You can also
run it by hand against the whole tree at any time:

```bash
bash scripts/scan-secrets.sh
```

Run it before opening a PR if you've touched anything under `azure/` or any `*.env*` file.

## The one hard rule: never hardcode a live value

No real password, hostname, IP address, Discord webhook URL, bot token, channel or guild ID,
Steam64 ID, Azure storage account name, or subscription ID ever goes into a commit. Not in
code, not in a comment, not in a commit message, not in a PR description.

This matters more here than in a typical project because of what "public repo" actually
means: there is no way to retract a value once it's pushed. Force-pushing over a commit does
not delete it — the orphaned commit is still fetchable by anyone who has its SHA, and by the
time you notice, forks and caches (including GitHub's own) may already have a copy. The only
real fix for a leaked credential is to rotate it — assume it is compromised the moment it
lands in a commit, even a commit you immediately "remove."

This isn't hypothetical for this project. The server password behind this exact deployment
was once committed in a private repo, in plain text, because a real value was written inline
instead of going through the config system described below. A private repo isn't even a safe
place for this — a public one gives you zero margin. Treat every value as radioactive: if
it's specific to a real deployment, it stays out of git entirely.

## Where configuration actually lives

Real values live in two root-only files on the VM, never in this repository:

- `/etc/valheim-server.env` — server identity, dashboard hostname, off-site backup account,
  etc.
- `/etc/valheim-alert.env` — Discord webhook URL and alert toggles.

Both are created with `chmod 0600`, owned by `root`, and are not readable by anything but the
services that need them.

What's tracked in this repo are *templates*, not the real files:

- `.env.example` (repo root) — the master reference for every environment variable read
  anywhere in the codebase, with every value left blank.
- `azure/valheim-server.env.example` — the template installed to `/etc/valheim-server.env`.
- `azure/dashboard/valheim-alert.env` — the template installed to `/etc/valheim-alert.env`.

Every credential-shaped value in these three files must stay empty. If you add a new
environment variable anywhere in the code, add it to the relevant template(s) too, with a
comment explaining what it's for, where to obtain it, and what happens if it's left blank —
follow the style already used in those files. Do not fill in a placeholder-looking fake value
"just so it's not empty" — an empty value is the documented way to say a feature is disabled,
and a fake-but-plausible value is exactly the kind of thing the secret scanner (and a future
contributor) can't distinguish from a real one.

`.gitignore` blanket-ignores anything with `.env` in its name and then explicitly un-ignores
the three template files above. If you rename or add a template file, keep that pattern
intact — don't just delete the ignore rule to work around it.

## Local development

The Python scripts (`azure/dashboard/*.py`) and the dashboard installer are written to run on
the VM — they read from `/etc/valheim-*.env`, systemd, and on-disk world-save state that only
exists there. There's no local server to run them against.

What you *can* work on locally is the dashboard front end, `azure/dashboard/index.html`. It's
a static page that polls a handful of JSON files (`status.json`, `history.json`,
`medals.json`, `restart-state.json`) over plain `fetch()` calls relative to its own page.
Point it at a hand-written sample JSON file with the shape the real collector produces (check
`valheim-status-collect.py` for the fields it writes) and open `index.html` directly, or serve
the folder with `python -m http.server`, to iterate on layout and behavior without touching
the VM at all.

Before opening a PR, syntax-check whatever you changed:

```bash
# Any shell script
bash -n path/to/script.sh

# Any Python script
python -m py_compile path/to/script.py
```

This catches typos and syntax errors early; it does not replace actually testing the change
on the VM if it touches server-side behavior — say so in your PR if you weren't able to.

## Pull request workflow

1. Branch off `main`.
2. Make your change, with commits that describe *why*, not just *what*.
3. Run the secret scanner and the relevant syntax checks (above) before pushing.
4. Open a PR against `main`. A good description says: what the change does, why, which
   files/units it touches, whether you tested it on a real VM or only syntax-checked it
   locally, and whether it changes anything about the templates in `.env.example` /
   `azure/valheim-server.env.example` / `azure/dashboard/valheim-alert.env`.
5. The maintainer reviews before merge — expect questions if a change touches secret
   handling, network/firewall rules, or anything that runs as root.

## Reporting a security issue

If you find a security problem — a vulnerability in the code, a gap in the hardening this
repo documents, or anything else — report it privately to the maintainer (open a private
GitHub security advisory from this repo's Security tab, or contact the maintainer directly).
Do not open a public issue for it.

If you believe an actual credential has been exposed (in this repo's history, a fork, a log,
anywhere), the first action is to rotate that credential, not to try to delete or force-push
over the commit that contains it. Rotation is the only step that actually closes the
exposure; deleting the commit does not, for the reasons explained above. Report it to the
maintainer either way so the exposure can be tracked down and any other copies dealt with.
