#!/usr/bin/env python3
"""Unit tests for valheim-bot.py's is_authorized_approver().

Exercises the gate with a fake interaction object, no Discord connection and no discord.py
install -- the module only imports discord lazily inside the gate, so a stub is enough.

Run:  python3 test_approver_gate.py     (from this directory; exits non-zero on failure)
"""
import importlib.util, os, sys, tempfile, types

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.join(HERE, "valheim-bot.py")

GUILD, CHAN, ROLE = 409918594235760641, 900000000000000001, 900000000000000002
ALICE, BOB, MALLORY = 111111111111111111, 222222222222222222, 333333333333333333


class FakeMember: pass
class FakeUser: pass


def _install_discord_stub():
    stub = types.ModuleType("discord")
    stub.Member, stub.User = FakeMember, FakeUser
    sys.modules["discord"] = stub


def load(user_ids_env):
    """Import valheim-bot.py fresh with the given RESTART_APPROVER_USER_IDS."""
    _install_discord_stub()
    root = tempfile.mkdtemp(prefix="vr-test-")
    os.environ.update({
        "HERMODR_GUILD_ID": str(GUILD),
        "RESTART_APPROVAL_CHANNEL_ID": str(CHAN),
        "RESTART_APPROVER_ROLE_ID": str(ROLE),
        "RESTART_APPROVER_USER_IDS": user_ids_env,
        "VALHEIM_RESTART_ROOT": root,
        "HERMODR_LOG": os.path.join(root, "bot.log"),
    })
    spec = importlib.util.spec_from_file_location("valheim_bot_under_test", BOT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def interaction(uid, roles, guild=GUILD, chan=CHAN, is_member=True):
    itx = types.SimpleNamespace(guild_id=guild, channel_id=chan)
    u = FakeMember() if is_member else FakeUser()
    u.id = uid
    u.roles = [types.SimpleNamespace(id=r) for r in roles]
    itx.user = u
    return itx


CASES = [
    ("allowlist unset -> role-only (original behaviour)", "", [
        ("holds the role",                          (ALICE, [ROLE], {}),              True),
        ("no role",                                 (ALICE, [],     {}),              False),
        ("role but wrong guild",                    (ALICE, [ROLE], {"guild": 1}),    False),
        ("role but wrong channel",                  (ALICE, [ROLE], {"chan": 1}),     False),
        ("role but not a Member (DM)",              (ALICE, [ROLE], {"is_member": False}), False),
    ]),
    ("allowlist = Alice, Bob", f"{ALICE}, {BOB}", [
        ("Alice: role + listed",                    (ALICE,   [ROLE], {}),            True),
        ("Bob: role + listed",                      (BOB,     [ROLE], {}),            True),
        ("Mallory: self-granted role, NOT listed",  (MALLORY, [ROLE], {}),            False),
        ("Alice: listed but lost the role",         (ALICE,   [],     {}),            False),
    ]),
    ("allowlist whitespace-separated", f"{ALICE} {BOB}", [
        ("Alice accepted",                          (ALICE,   [ROLE], {}),            True),
        ("Mallory refused",                         (MALLORY, [ROLE], {}),            False),
    ]),
    ("allowlist set but malformed -> fails closed", "hashtagblessed0, asiaticclams", [
        ("Alice refused",                           (ALICE,   [ROLE], {}),            False),
        ("Mallory refused",                         (MALLORY, [ROLE], {}),            False),
    ]),
]


def main():
    failed = 0
    for title, env, cases in CASES:
        mod = load(env)
        print(f"\n{title}   RESTART_APPROVER_USER_IDS={env!r}")
        for desc, (uid, roles, kw), expected in cases:
            got = mod.is_authorized_approver(interaction(uid, roles, **kw))
            ok = got is expected
            failed += not ok
            print(f"  {'PASS' if ok else '**FAIL**'}  {desc:<44} -> {got} (want {expected})")
    print("\n" + ("ALL TESTS PASSED" if not failed else f"{failed} TEST(S) FAILED"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
