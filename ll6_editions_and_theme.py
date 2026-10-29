#!/usr/bin/env python3
"""
Legis-Link LL-6: pinned editions, 40 C ambient rule, new brand colours (v3.8.0).

  1. Answers always cite the same editions: AS/NZS 3000:2018 (no 2024 edition exists), AS/NZS 3008.1.1:2017
     (2025 edition noted as voluntary), BS 7671:2018+A4:2026, NEC by the edition the state adopted.
  2. Australian cable ratings already assume 40 C ambient air (25 C soil): derating only above 40 C.
  3. App, landing and Connect pages re-themed to the Test & Tag brand: hi-vis orange buttons with navy text
     (6.5:1 contrast), signal-yellow highlights, site-navy background.

Run AFTER LL-5, in the legis-link-mcp folder:
    python ll6_editions_and_theme.py
"""
import ast
import datetime
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN = os.environ.get("LL_MAIN") or os.path.join(HERE, "legis_link_mcp_server.py")
MARK = "EDITIONS TO CITE (always exactly these)"
SWAPS = [('        "- Standards: AS/NZS 3000:2018 (Wiring Rules: installation, protection, verification) and AS/NZS 3008.1.1 "\n        "(selection of cables: current ratings, derating factors, voltage drop). They gain legal force through each "\n        "state\'s electrical safety legislation.\\n"\n', '        "- EDITIONS TO CITE (always exactly these): AS/NZS 3000:2018 (Wiring Rules, as amended) is the current edition - there "\n        "is no 2024 edition. AS/NZS 3008.1.1:2017 (selection of cables: current ratings, derating, voltage drop) is the edition "\n        "AS/NZS 3000:2018 references; AS/NZS 3008.1.1:2025 was published in December 2025 and may be used voluntarily until "\n        "the Wiring Rules reference it. They gain legal force through each state\'s electrical safety legislation.\\n"\n        "- AMBIENT TEMPERATURE: AS/NZS 3008.1.1 current ratings already assume 40 C ambient air (and 25 C soil for buried "\n        "cables). Ambient derating applies only ABOVE 40 C air (e.g. some roof spaces), never at 25-30 C. Do not apply IEC/BS "\n        "30 C reference conditions to Australian cables.\\n"\n', 1), ('"- Standard: BS 7671 (IET Wiring Regulations, current amendment). In England and Wales, Building Regulations "', '"- EDITION TO CITE (always exactly this): BS 7671:2018+A4:2026 (IET Wiring Regulations; Amendment 4 published "\n        "15 April 2026; the +A2:2022+A3:2024 version was withdrawn in October 2026). In England and Wales, Building Regulations "', 1), ('"- Code: NEC (NFPA 70); the adopted edition differs by state and local authority having jurisdiction (AHJ).\\n"', '"- Code: NEC (NFPA 70). Editions in use: 2020, 2023 and 2026; the adopted edition differs by state and local authority "\n        "having jurisdiction (AHJ). Unless the edition adopted in the named state is given here, write \'the NEC edition "\n        "adopted in <state>\' instead of guessing a year. Section numbers used here (240.4(D), 210.8, 210.12) are stable "\n        "across these editions.\\n"', 1), ('    "- A PRACTICE REFERENCE below has been reviewed and is authoritative for common practice: do not contradict it.\\n"\n', '    "- A PRACTICE REFERENCE below has been reviewed and is authoritative for common practice: do not contradict it.\\n"\n    "- Cite standards ONLY by the editions named in the PRACTICE REFERENCE or verified data. Never invent or change an "\n    "edition year; if no edition is given, name the standard without a year.\\n"\n', 1), ('VERSION           = "3.7.0"', 'VERSION           = "3.8.0"', 1)]

NAVY = "#0B1320"
# per page: (old, new, minimum count) - applied inside the embedded page only
THEME = {
    "app.html": [
        ("--bg:#0a0f1a;--surface:#111827;--surface2:#1a2235;", "--bg:#0B1320;--surface:#111C2E;--surface2:#18263B;", 1),
        ("--border:#1e293b;--accent:#3b82f6;--accent2:#06b6d4;", "--border:#22324A;--accent:#FF6A13;--accent2:#FFC72C;", 1),
    ],
    "landing.html": [
        ("--bg:#080e1a;--surface:#0f1928;--surface2:#162030;", "--bg:#0B1320;--surface:#111C2E;--surface2:#18263B;", 1),
        ("--border:#1c2d42;--accent:#2f80ed;--accent2:#56ccf2;", "--border:#22324A;--accent:#FF6A13;--accent2:#FFC72C;", 1),
    ],
    "connect.html": [
        ("--bg:#0a0f1a; --surface:#111827; --border:#1e293b; --accent:#3b82f6; --accent2:#06b6d4;",
         "--bg:#0B1320; --surface:#111C2E; --border:#22324A; --accent:#FF6A13; --accent2:#FFC72C;", 1),
    ],
}
COMMON = [   # text on orange must be navy; blue tints become orange/yellow tints
    ("background:var(--accent);color:white", "background:var(--accent);color:" + NAVY, 0),
    ("background:var(--accent);color:#fff", "background:var(--accent);color:" + NAVY, 0),
    ("background:var(--accent);border:none;color:white", "background:var(--accent);border:none;color:" + NAVY, 0),
    ("rgba(59,130,246,", "rgba(255,106,19,", 0),
    ("rgba(6,182,212,", "rgba(255,199,44,", 0),
    ("#2563eb", "#E85A0C", 0), ("#3b82f6", "#FF6A13", 0), ("#06b6d4", "#FFC72C", 0), ("#1a6fd4", "#E85A0C", 0),
    ("var(--accent),#E85A0C);color:white", "var(--accent),#E85A0C);color:" + NAVY, 0),
]


def abort(msg):
    print("ABORTED - no changes written.")
    print(msg)
    sys.exit(1)


def main():
    if not os.path.exists(MAIN):
        abort("Not found: " + MAIN + "\nPut this script in the legis-link-mcp folder.")
    with open(MAIN, "r", encoding="utf-8-sig", newline="") as f:
        src = f.read()
    if MARK in src:
        abort("LL-6 is already applied.")
    if "# LL-5: Test & Tag logo" not in src:
        abort("Please apply ll5_logo.py first.")
    nl = "\r\n" if "\r\n" in src else "\n"
    out = src.replace(nl, "\n")
    for old, new, n in SWAPS:
        if out.count(old) != n:
            abort("Text not found exactly " + str(n) + " time(s):\n  " + old[:110])
        out = out.replace(old, new)
    for name, rules in THEME.items():
        key = '"' + name + '":'
        i = out.find(key)
        j = out.find('\n    "', i + len(key))
        if i < 0 or j < 0 or out.count(key) != 1:
            abort("Page not found once: " + name)
        page = out[i:j]
        for old, new, n in rules + COMMON:
            if n and page.count(old) < n:
                abort(name + ": colour text not found: " + old)
            page = page.replace(old, new)
        out = out[:i] + page + out[j:]
    try:
        ast.parse(out)
    except SyntaxError as e:
        abort("Patched file failed Python syntax check: " + str(e))
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(MAIN, MAIN + ".bak_" + stamp)
    with open(MAIN, "w", encoding="utf-8", newline="") as f:
        f.write(out.replace("\n", nl))
    print("OK - LL-6 applied (v3.8.0).")
    print("  done: editions pinned (AS/NZS 3000:2018, AS/NZS 3008.1.1:2017, BS 7671:2018+A4:2026, NEC by state)")
    print("  done: 40 C ambient rule for Australian cable ratings")
    print("  done: app, landing and Connect pages in the Test & Tag colours")
    print("Backup saved with .bak_" + stamp)


if __name__ == "__main__":
    main()
