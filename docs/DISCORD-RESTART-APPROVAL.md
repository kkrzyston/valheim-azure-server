# Discord restart-approval setup

Setup runbook for the dashboard restart-request feature: players request a restart from the
dashboard, someone holding a trusted role approves it in Discord, and only then does
`valheim-restart-exec.py` act.

The feature is fail-closed. With `RESTART_APPROVER_ROLE_ID` unset, `restart_approval_ready()`
returns false, the bot never reads `inbox/`, and no approval message is ever posted. An
unconfigured deployment is a safe deployment.

Four settings drive it, all read by `valheim-bot.py`:

    HERMODR_GUILD_ID=409918594235760641          # <YOUR_GUILD_ID>
    RESTART_APPROVAL_CHANNEL_ID=1549173815135703080   # #restart-approvals
    RESTART_APPROVER_ROLE_ID=1549147639386345512      # Restart Approver
    RESTART_APPROVER_USER_IDS=321026360292278284,230193915444068353,272087257320652803
    # hashtagblessed, Frostblade Irelia, peon -- verified against the role's own member list.
    # Hermodr's own role id is 1549106984496931129 (needed only for channel overrides).

None of these are secrets — Discord IDs are public identifiers visible to anyone in the server.
`DISCORD_BOT_TOKEN`, in the same env file, is the secret.

---

## 1. Developer Mode

User Settings -> Advanced -> Developer Mode (on). Per-account, not per-server, so it covers the
live server and the throwaway both.

Verify: right-click any server icon. "Copy Server ID" should appear at the bottom of the menu.
If it doesn't, Ctrl+R to reload the desktop app.

## 2. The approver role

Server Settings -> Roles -> Create Role. Name it `Restart Approver`. Grant it **no permissions**
— it is a marker the bot checks, not a grant of power.

In general, positioning a role high in the list helps: Manage Roles only permits assigning roles
positioned *below* the assigner's own highest role. **On this server that buys nothing**, and it is
worth knowing why.

An audit of all 14 roles plus `@everyone` (2026-09-14) found exactly one role holding Manage Roles
or Administrator:

    Admin -- Administrator, Manage Roles, Manage Channels, Manage Server, Manage Webhooks
             members: Capt Scoliosis, Frostblade Irelia, hashtagblessed, peon

Every other role -- Arnas, verified egrill, Trumpets, Valley Kooks, MY NAME IS JOSH, grills,
Great White Northerners, Deep South, PDX Gang, Wood Tier, EU Daddy, Civvies, Hermodr, @everyone --
holds none of them. Hermodr's own role holds no dangerous permission, which is correct for a bot.

Because the only Manage Roles holders also hold Administrator, and Administrator bypasses hierarchy
entirely, those four can grant themselves any role at any position. So the effective set is:

    holders of Restart Approver
  + the four members of Admin
  + the server owner (confirmed: hashtagblessed)

and role position does not narrow it. Only two things do: removing Administrator from people who
don't need it, or the user allowlist described below.

To re-audit later: Server Settings -> Roles, check each role's Permissions tab for Manage Roles and
Administrator; then Server Settings -> Members filtered by those roles. Note that Discord renders
these toggles as `<input role="switch">` with state in `.checked` -- `aria-checked` is absent, so
anything reading the accessibility tree will report every permission as off.

Who should hold it: the people you'd already trust to SSH in and run `systemctl restart`. The
approval gate exists precisely so this set is smaller than the set of players.

## 3. The restricted approval channel

Create a text channel — `#restart-approvals` — separate from the channel Hermodr answers questions
in, so people without the role never see the buttons. Mark it private when prompted, then set the
overrides explicitly rather than trusting the default:

| Role                | View Channel | Send Messages | Read Message History |
|---------------------|--------------|---------------|----------------------|
| `@everyone`         | deny         | passthrough   | passthrough          |
| `Restart Approver`  | allow        | allow         | allow                |
| `Hermodr`           | allow        | allow         | allow                |

This is the live state of `#restart-approvals` as configured on 2026-09-14, read back from the
permission controls rather than assumed. Send Messages ends up allowed for `Restart Approver`
because Discord's private-channel "add a role" flow grants a standard bundle; it is harmless here.
Approvers being able to talk in the approval channel does not weaken anything, because the bot
never treats a channel message as an approval -- `on_interaction()` is the only path, and there is
no text fallback. Set it to passthrough if you would rather the channel stay silent.

`Admin` also appears on the channel (via Administrator) and cannot be removed, and the channel
lists hashtagblessed as Server Owner.

The bot row is the one that gets missed. A bot's role is the auto-created one named after the
application, and it does not inherit from `@everyone` overrides in a channel where `@everyone` is
denied View Channel.

Verify rather than assume: in the channel's permission editor, the member dropdown at the bottom of
the Permissions tab computes the effective result for a specific member. Select Hermodr and confirm
all three show allowed — that resolves the whole role stack, not one override.

Empirical check: have the bot post anything into the channel. Missing Send Messages produces a 403
in `HERMODR_LOG` rather than failing silently.

Channel permissions are defence in depth, not the boundary. `is_authorized_approver()` re-checks
guild, channel, role ID, and (when configured) user ID server-side on every click.

## 3a. The user allowlist

A Discord role cannot be a boundary against the people who control Discord roles. Anyone with
Manage Roles or Administrator can grant themselves `Restart Approver` in seconds, and the bot's
role check will then pass them -- correctly, because from the bot's side they genuinely hold the
role.

`RESTART_APPROVER_USER_IDS` closes that. When set, `is_authorized_approver()` requires the member
to hold the role **and** appear in the list. Being added to the list means editing
`/etc/valheim-bot.env` on the VM, which needs SSH -- a different trust boundary than "has Manage
Roles in a chat server".

    RESTART_APPROVER_USER_IDS=111111111111111111,222222222222222222,333333333333333333

Obtain each with right-click a user -> Copy User ID. Comma- or space-separated.

It is a strict narrowing, never a widening: the role is still required, so the allowlist can only
ever refuse someone the role would have allowed. Leave it blank for role-only behaviour.

If it is set but contains no parseable ID -- usernames pasted instead of IDs, say -- the feature
fails closed rather than falling back to role-only. Widening access at the exact moment the
operator believed they were narrowing it is the worst possible failure mode, so it refuses to run
and logs why.

`test_approver_gate.py`, next to the bot, covers this: role-only behaviour, the allowlist accepting
listed members, a self-granted role being refused, and the malformed case failing closed. Run it
with `python3 test_approver_gate.py` -- no Discord connection or discord.py install needed.

## 4. Collecting the IDs

- Guild: right-click the server icon -> Copy Server ID
- Channel: right-click `#restart-approvals` -> Copy Channel ID
- Role: Server Settings -> Roles -> hover the role -> three-dot menu -> Copy Role ID

All are 17-20 digit numbers. `valheim-bot.py` validates with `.isdigit()`, so a stray space or
angle bracket fails the feature closed rather than causing it to misbehave. Check for that first if
the feature seems dead.

These go in `/etc/valheim-bot.env` on the VM, alongside `DISCORD_BOT_TOKEN`.

## 5. Throwaway test environment

Do not test in the live server. If the approver role ID is wrong the button is either dead or open
to everyone, and that is worth discovering somewhere harmless.

**Throwaway Discord server:** the `+` in the left rail -> Create My Own -> name it `hermodr-test`.

**Second bot application** at <https://discord.com/developers/applications> -> New Application ->
`hermodr-test-bot`. Then:

- Bot tab -> Privileged Gateway Intents -> **Message Content Intent** on -> Save Changes
- **Public Bot** off, so nobody else can invite it
- Reset Token, and copy it. Discord displays it exactly once

On Message Content: the live bot declares it (`intents.message_content = True`) for the Q&A
feature, so enabling it keeps the test faithful. It is **not** what makes the buttons work —
Discord's intent tables govern message and presence event families, and `INTERACTION_CREATE` is
not among them. If buttons don't fire during testing, the intent is not the cause.

**Invite it** (substituting the new application's ID):

    https://discord.com/oauth2/authorize?client_id=YOUR_TEST_APP_ID&scope=bot&permissions=68608

68608 = View Channel (1 << 10) + Send Messages (1 << 11) + Read Message History (1 << 16).

**Recreate the structure:** same `Restart Approver` role with no permissions, same private
`#restart-approvals` channel with the same three overrides, and collect the three IDs.

**Run it as a separate process against a separate env file.** `is_authorized_approver()` checks
guild ID *and* channel ID *and* role ID, so you cannot test by pointing the live bot at a throwaway
channel — all three must move together.

    # ---- LIVE (real Discord server, real players) ----
    HERMODR_GUILD_ID=
    RESTART_APPROVAL_CHANNEL_ID=
    RESTART_APPROVER_ROLE_ID=
    # DISCORD_BOT_TOKEN lives in /etc/valheim-bot.env on the VM. Leave it there.

    # ---- THROWAWAY (hermodr-test, safe to break) ----
    HERMODR_GUILD_ID=
    RESTART_APPROVAL_CHANNEL_ID=
    RESTART_APPROVER_ROLE_ID=
    # DISCORD_BOT_TOKEN from the test application. Different value. Never in the live file.

Both tokens live in env files on disk, `chmod 600`, read via systemd `EnvironmentFile=`. Neither
goes in a commit, a chat message, or a screenshot. `scripts/scan-secrets.sh` is the backstop;
confirm `.gitignore` covers the test env file before writing a token into it. A leaked token is
revoked by Reset Token in the portal.

## 6. What "working" looks like

Run all of these in the throwaway.

**Without the role, Approve is refused privately.** Ephemeral reply: "You're not authorized to
approve or deny restarts." Nobody else in the channel sees the refusal, and nothing is written to
`verdicts/`. To test this you need an account that can *see* the channel but lacks the role — give
a test account the role, then remove the role while leaving channel visibility.

**With the role, Approve is accepted.** The buttons vanish from the message
(`interaction.response.edit_message(view=None)`) and a verdict file is written. If stripping the
buttons fails, you get an ephemeral "Recorded: approved." instead — degraded but correct.

**Two people clicking at once yields exactly one winner.** `write_verdict_exclusive()` creates with
`O_EXCL`, so the filesystem arbitrates rather than the bot. The loser gets "Already decided by
\<name>." or "Someone else already decided this one first." Test both the same-button case and the
Approve-vs-Deny case; the latter is the one that matters.

**Restarting the bot mid-request leaves the buttons working.** `build_approval_view()` gives the
buttons no callback; all handling happens in `on_interaction()` by parsing `custom_id`. There is no
view registration to lose and no `add_view()` call is needed. `systemctl restart valheim-bot` with
a request pending, then click.

**A wrong role ID refuses everyone.** Set `RESTART_APPROVER_ROLE_ID` to a plausible but incorrect
number and restart. Every click, including yours, should be refused. If a wrong role ID makes the
button work for *everyone*, that is the failure this whole exercise exists to catch.

**A blank role ID posts nothing at all.** Clear `RESTART_APPROVER_ROLE_ID` and restart. The bot
should never post an approval message and never read `inbox/`. This is the fail-closed path that
protects you if the env file is ever truncated.

**If you set the allowlist, test that it actually excludes.** Give a test account the role, leave
it off `RESTART_APPROVER_USER_IDS`, and confirm its click is refused. That is the whole point of
the allowlist, and it is the one behaviour that silently reverts to role-only if the variable is
misspelled in the env file.

---

## Verdict schema

`valheim-restart-exec.py` validates every verdict file and quarantines anything malformed:

    verdict not in ("approve", "deny")  -> quarantined
    schema != 1                         -> quarantined

so the bot must write:

    {"schema": 1, "id": "<uuid4>", "verdict": "approve"|"deny", "at": <float>,
     "approver": {"display": "...", "discord_id": "..."}}

An older revision of `write_verdict_exclusive()` wrote `"decision": "approved"|"denied"` with no
`schema` key and `"approver": {"id": ...}`. Every verdict from that revision is quarantined as
malformed — the approval appears to succeed in Discord and the restart never happens. If the
restart flow goes silent, check `quarantine/` before anything else.
