#!/usr/bin/env python3
"""
Supprime lentement tes propres messages d'un salon Discord (DM ou serveur).

ATTENTION : l'automatisation avec un token utilisateur (self-bot) viole les
Conditions d'utilisation de Discord et peut entraîner la suspension du compte.
Utilise ce script à tes risques et périls. Ne partage jamais ton token.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any

import requests

API_BASE = "https://discord.com/api/v10"
DEFAULT_DELAY = 1.2  # secondes entre chaque suppression (safe pour éviter le 429)


class DiscordClient:
    def __init__(self, token: str) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": token.strip(),
                "Content-Type": "application/json",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            }
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        url = f"{API_BASE}{path}"
        while True:
            resp = self.session.request(method, url, **kwargs)

            if resp.status_code == 429:
                retry_after = float(resp.json().get("retry_after", 5))
                print(f"[!] Rate limit — pause {retry_after:.1f}s")
                time.sleep(retry_after + 0.5)
                continue

            if resp.status_code >= 500:
                print(f"[!] Erreur serveur {resp.status_code}, retry dans 5s")
                time.sleep(5)
                continue

            return resp

    def get_me(self) -> dict[str, Any]:
        resp = self._request("GET", "/users/@me")
        resp.raise_for_status()
        return resp.json()

    def fetch_messages(
        self, channel_id: str, *, before: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": min(limit, 100)}
        if before:
            params["before"] = before

        resp = self._request("GET", f"/channels/{channel_id}/messages", params=params)
        resp.raise_for_status()
        return resp.json()

    def delete_message(self, channel_id: str, message_id: str) -> bool:
        resp = self._request("DELETE", f"/channels/{channel_id}/messages/{message_id}")
        if resp.status_code == 204:
            return True
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        return True


def collect_own_messages(
    client: DiscordClient,
    channel_id: str,
    user_id: str,
    *,
    max_messages: int | None = None,
) -> list[dict[str, Any]]:
    own: list[dict[str, Any]] = []
    before: str | None = None

    while True:
        batch = client.fetch_messages(channel_id, before=before)
        if not batch:
            break

        for msg in batch:
            if msg.get("author", {}).get("id") == user_id:
                own.append(msg)

        before = batch[-1]["id"]

        if max_messages and len(own) >= max_messages:
            own = own[:max_messages]
            break

        if len(batch) < 100:
            break

        # Petite pause entre les pages pour ne pas spammer la lecture
        time.sleep(0.6)

    return own


def clear_channel(
    client: DiscordClient,
    channel_id: str,
    user_id: str,
    *,
    delay: float,
    dry_run: bool,
    max_messages: int | None,
) -> tuple[int, int]:
    print("[*] Récupération des messages…")
    messages = collect_own_messages(
        client, channel_id, user_id, max_messages=max_messages
    )

    total = len(messages)
    if total == 0:
        print("[+] Aucun message à supprimer.")
        return 0, 0

    print(f"[*] {total} message(s) trouvé(s).")

    if dry_run:
        for i, msg in enumerate(messages, 1):
            content = (msg.get("content") or "")[:60].replace("\n", " ")
            print(f"  [{i}/{total}] {msg['id']} — {content!r}")
        print("[+] Mode dry-run : rien n'a été supprimé.")
        return total, 0

    deleted = 0
    for i, msg in enumerate(messages, 1):
        msg_id = msg["id"]
        content = (msg.get("content") or "")[:40].replace("\n", " ")

        try:
            if client.delete_message(channel_id, msg_id):
                deleted += 1
                print(f"[{i}/{total}] supprimé {msg_id} — {content!r}")
            else:
                print(f"[{i}/{total}] introuvable {msg_id}")
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            print(f"[{i}/{total}] échec {msg_id} (HTTP {status})")

        if i < total:
            time.sleep(delay)

    return total, deleted


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Supprime lentement tes messages dans un salon Discord."
    )
    parser.add_argument(
        "channel_id",
        help="ID du salon (clic droit sur le salon → Copier l'identifiant du salon)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("DISCORD_TOKEN"),
        help="Token utilisateur (ou variable DISCORD_TOKEN)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY,
        help=f"Délai entre chaque suppression en secondes (défaut: {DEFAULT_DELAY})",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=None,
        help="Nombre max de messages à supprimer",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Lister les messages sans les supprimer",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not args.token:
        print(
            "Erreur : fournis un token via --token ou la variable DISCORD_TOKEN.",
            file=sys.stderr,
        )
        return 1

    if args.delay < 0.5:
        print("[!] Délai < 0.5s : risque élevé de rate limit.")

    client = DiscordClient(args.token)

    try:
        me = client.get_me()
    except requests.HTTPError:
        print("Erreur : token invalide ou expiré.", file=sys.stderr)
        return 1

    username = me.get("username", "?")
    user_id = me["id"]
    print(f"[+] Connecté en tant que {username} ({user_id})")
    print(f"[*] Salon cible : {args.channel_id}")
    print(f"[*] Délai : {args.delay}s entre chaque suppression\n")

    total, deleted = clear_channel(
        client,
        args.channel_id,
        user_id,
        delay=args.delay,
        dry_run=args.dry_run,
        max_messages=args.max,
    )

    if not args.dry_run:
        print(f"\n[+] Terminé : {deleted}/{total} message(s) supprimé(s).")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
