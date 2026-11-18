"""Twitch credential setup and live verification. Never prints secret values."""
from __future__ import annotations

import getpass
import os
import re
import stat
from pathlib import Path

import httpx

from .config import ROOT, Config, secret

ENV_PATH = ROOT / ".env"
ID_RX = re.compile(r"^[a-z0-9]{20,40}$")


def _write_env(updates: dict[str, str], path: Path = ENV_PATH) -> None:
    """Replace or append KEY=value lines in .env, keeping everything else.
    Rewrites the same file in place, so its permissions/ACL are kept."""
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    done = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in updates:
            lines[i] = f"{key}={updates[key]}"
            done.add(key)
    lines += [f"{k}={v}" for k, v in updates.items() if k not in done]
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    if os.name != "nt":
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    for k, v in updates.items():
        os.environ[k] = v


def prompt_twitch_credentials(path: Path = ENV_PATH) -> int:
    print("Twitch app credentials (from dev.twitch.tv/console). Input is stored in", path)
    print("The secret is not shown while you type or paste it.")
    cid = input("Client ID: ").strip()
    if not ID_RX.match(cid):
        print("That does not look like a Twitch Client ID (20-40 lowercase letters/digits). Nothing saved.")
        return 1
    sec = getpass.getpass("Client Secret (hidden): ").strip()
    if not ID_RX.match(sec):
        print("That does not look like a Twitch Client Secret (20-40 lowercase letters/digits). Nothing saved.")
        return 1
    ok, msg = validate_app_credentials(cid, sec)
    if not ok:
        print(f"Twitch rejected these credentials: {msg}. Nothing saved.")
        return 1
    _write_env({"TWITCH_CLIENT_ID": cid, "TWITCH_CLIENT_SECRET": sec}, path)
    print(f"Saved to {path}. {msg}")
    return 0


def validate_app_credentials(cid: str, sec: str, http: httpx.Client | None = None) -> tuple[bool, str]:
    http = http or httpx.Client(timeout=20)
    try:
        r = http.post("https://id.twitch.tv/oauth2/token",
                      data={"client_id": cid, "client_secret": sec, "grant_type": "client_credentials"})
    except httpx.HTTPError as e:
        return False, f"network error reaching id.twitch.tv ({type(e).__name__})"
    if r.status_code == 200:
        days = int(r.json().get("expires_in", 0)) // 86400
        return True, f"credentials valid (app token issued, valid ~{days} days, renewed automatically)"
    try:
        detail = r.json().get("message", "")
    except ValueError:
        detail = ""
    return False, f"HTTP {r.status_code} {detail}".strip()


def twitch_check(cfg: Config, chat_seconds: int = 0, http: httpx.Client | None = None) -> int:
    """Live, read-only verification of the whole Twitch integration."""
    from .discovery.twitch import TwitchAPI
    bad = 0

    def line(st, name, note=""):
        print(f"[{st:4}] {name:28} {note}")

    cid, sec = secret("TWITCH_CLIENT_ID"), secret("TWITCH_CLIENT_SECRET")
    line("OK" if cid else "FAIL", "TWITCH_CLIENT_ID in .env", "set" if cid else "missing")
    line("OK" if sec else "FAIL", "TWITCH_CLIENT_SECRET in .env", "set" if sec else "missing")
    if not (cid and sec):
        print("Run: python -m clipper credentials twitch")
        return 1
    ok, msg = validate_app_credentials(cid, sec, http)
    line("OK" if ok else "FAIL", "app access token", msg)
    if not ok:
        return 1
    api = TwitchAPI(cid, sec, http=http)

    names = cfg["discovery"]["game_names"]
    ids = api.game_ids(names)
    found = [n for n in names if api._game_ids.get(n)]
    line("OK" if ids else "FAIL", "GTA VI category", f"found on Twitch: {found or 'none'}; ids {ids}")
    bad += 0 if ids else 1

    logins = [c["channel"].lower() for c in cfg["authorized_channels"] if c.get("platform") == "twitch" and c.get("authorized")]
    if not logins:
        line("WARN", "authorized channels", "none with authorized: true in config/config.yaml")
    else:
        users = api._get("/users", [("login", l) for l in logins]).get("data", [])
        known = {u["login"]: u for u in users}
        live = {s.channel: s for s in api.streams_by_login(logins)}
        for l in logins:
            if l not in known:
                line("FAIL", f"channel {l}", "no such Twitch user (check the login spelling)")
                bad += 1
            elif l in live:
                s = live[l]
                in_game = s.game.lower() in {g.lower() for g in names}
                line("OK", f"channel {l}", f"LIVE, {s.viewers} viewers, category '{s.game}'"
                     + ("" if in_game else " (not GTA VI: watchdog waits)"))
            else:
                line("OK", f"channel {l}", "exists, currently offline (watchdog starts capture when live)")

    ut = secret("TWITCH_USER_TOKEN")
    if ut:
        r = (http or httpx.Client(timeout=20)).get("https://id.twitch.tv/oauth2/validate", headers={"Authorization": f"OAuth {ut}"})
        if r.status_code == 200:
            scopes = r.json().get("scopes", [])
            ok = "clips:edit" in scopes
            line("OK" if ok else "FAIL", "TWITCH_USER_TOKEN", "valid, has clips:edit" if ok else f"valid but missing clips:edit (scopes {scopes})")
            bad += 0 if ok else 1
        else:
            line("FAIL", "TWITCH_USER_TOKEN", f"invalid/expired (HTTP {r.status_code})")
            bad += 1
    else:
        line("--", "TWITCH_USER_TOKEN", "not set (only needed for publish.mode: twitch_clip)")

    if chat_seconds and logins:
        import asyncio
        from .signals.chat import ChatWindow, TwitchChat
        w = ChatWindow([])
        c = TwitchChat(logins[0], w)

        async def go():
            stop = asyncio.Event()
            t = asyncio.create_task(c.run(stop))
            await asyncio.sleep(chat_seconds)
            stop.set()
            t.cancel()
        asyncio.run(go())
        ok = c.error is None or w.total > 0
        line("OK" if ok else "FAIL", f"chat #{logins[0]}", f"{w.total} messages in {chat_seconds}s" + (f" ({c.error})" if c.error else ""))
        bad += 0 if ok else 1
    print("== Twitch integration OK ==" if not bad else f"== {bad} problem(s) ==")
    return 1 if bad else 0
