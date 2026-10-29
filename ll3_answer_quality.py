#!/usr/bin/env python3
"""
Legis-Link LL-3: answer quality - normal case first (v3.5.0).

  1. Every tool now answers the ordinary installation first and makes the verdict conditional,
     instead of assuming an unstated worst case (the "2.5 mm2 TPS on 20 A is non-compliant" error).
     NON_COMPLIANT only when the facts given break a rule; no invented extra requirements;
     no "upsize to be safe"; "High" confidence only when grounded.
  2. Reviewed practice references for electrical work in Australia, the UK and the US
     (common circuit arrangements, what changes the answer, voltage-drop limits, RCD/GFCI notes).
     No copyrighted table values.
  3. The AI model can be changed from Railway with LEGIS_MODEL (default unchanged).

Run AFTER LL-2a, in the legis-link-mcp folder:
    python ll3_answer_quality.py
Then run ll_eval.py to check answers against known-good results.
"""
import ast
import datetime
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN = os.environ.get("LL_MAIN") or os.path.join(HERE, "legis_link_mcp_server.py")
MARK = "LL-3: answer the normal case first"

SWAPS = [('MODEL             = "claude-haiku-4-5-20251001"\n', 'MODEL             = os.environ.get("LEGIS_MODEL", "") or "claude-haiku-4-5-20251001"   # set LEGIS_MODEL in Railway to upgrade\n', 1), ('async def ask_claude(system_prompt: str, user_message: str) -> dict:\n', '# ── LL-3: answer the normal case first, with reviewed practice references ──────\n# Reviewed, common-practice facts (no copyrighted table values). Injected into the\n# system prompt for the matching trade and country so routine questions are not\n# answered with an unstated worst case.\n_ANSWER_RULES = (\n    "\\n\\nANSWER THE NORMAL CASE FIRST (these rules override any tendency to assume the worst):\\n"\n    "- Judge the question as asked. If a deciding condition is not stated (installation method, contact with "\n    "thermal insulation, grouping, ambient temperature, route length, fire rating), answer for the ordinary, most "\n    "common installation and make the verdict conditional, for example: \'Compliant for a typical installation; "\n    "not compliant if the cable is fully surrounded by thermal insulation\'.\\n"\n    "- Use NON_COMPLIANT only when the facts given break a rule. Never mark something NON_COMPLIANT because an "\n    "unstated worst case might apply. Use COMPLIANT for a conditional \'yes in the normal case\' answer and list "\n    "the conditions that would change it.\\n"\n    "- Do not add requirements the facts do not trigger (for example, ordinary cable in an ordinary wall cavity "\n    "does not need fire-rated cable or conduit).\\n"\n    "- Never recommend a bigger size or heavier product \'to be safe\' as the answer; say which condition would require it.\\n"\n    "- Confidence: \'High\' only when the verdict matches the PRACTICE REFERENCE or verified data given here; "\n    "otherwise \'Medium\' or \'Low\'.\\n"\n    "- A PRACTICE REFERENCE below has been reviewed and is authoritative for common practice: do not contradict it.\\n"\n)\n\n_PRACTICE = {\n    ("electrical", "AU"): (\n        "PRACTICE REFERENCE - Australia, electrical (reviewed):\\n"\n        "- Standards: AS/NZS 3000:2018 (Wiring Rules: installation, protection, verification) and AS/NZS 3008.1.1 "\n        "(selection of cables: current ratings, derating factors, voltage drop). They gain legal force through each "\n        "state\'s electrical safety legislation.\\n"\n        "- 2.5 mm2 copper TPS protected by a 20 A circuit breaker is the standard arrangement for socket-outlet "\n        "(power) final subcircuits. AS/NZS 3000 Appendix C lists such arrangements as deemed to comply, subject to "\n        "maximum route lengths. In an ordinary wall cavity (unenclosed, not surrounded by thermal insulation, not "\n        "bundled with other loaded circuits) its current rating is comfortably above 20 A, so it is COMPLIANT in the "\n        "typical case.\\n"\n        "- What changes the answer: cable fully surrounded by thermal insulation (heavy derating that can take "\n        "2.5 mm2 below 20 A - route it clear of the insulation or upsize); grouping with other loaded circuits; high "\n        "ambient temperature (e.g. roof spaces); long runs that fail voltage drop. Read the exact ratings and derating "\n        "factors from the AS/NZS 3008.1.1 tables for the installation method; do not state table values.\\n"\n        "- Voltage drop: total from the point of supply to any point must not exceed 5% of nominal voltage (AS/NZS 3000).\\n"\n        "- Lighting final subcircuits: 1.0 or 1.5 mm2 TPS on a 10 A or 16 A circuit breaker is common practice.\\n"\n        "- TPS is routinely run in timber and steel-framed wall cavities; that alone does not require conduit or "\n        "fire-rated cable. Holes in steel framing need grommets or bushes; penetrations of fire-rated walls or floors "\n        "must be fire-stopped.\\n"\n        "- 30 mA RCD protection is required for socket-outlet and lighting final subcircuits in domestic installations "\n        "under AS/NZS 3000:2018.\\n"\n    ),\n    ("electrical", "UK"): (\n        "PRACTICE REFERENCE - United Kingdom, electrical (reviewed):\\n"\n        "- Standard: BS 7671 (IET Wiring Regulations, current amendment). In England and Wales, Building Regulations "\n        "Part P applies to notifiable domestic electrical work.\\n"\n        "- 2.5 mm2 twin and earth (T&E) is the standard cable for a 32 A ring final circuit and for a 20 A radial "\n        "circuit (standard circuit arrangements in the IET On-Site Guide). In typical installation methods (clipped "\n        "direct, or in a stud wall not surrounded by insulation) it is COMPLIANT in the typical case.\\n"\n        "- What changes the answer: contact with or enclosure in thermal insulation (installation reference methods "\n        "with heavy derating), grouping, high ambient temperature, and voltage drop on long runs.\\n"\n        "- Voltage drop (installations supplied from the public LV network): 3% for lighting and 5% for other uses (BS 7671 Appendix 4).\\n"\n        "- Lighting circuits: 1.0 or 1.5 mm2 T&E on a 6 A or 10 A device is common practice.\\n"\n        "- 30 mA RCD protection is required for socket-outlets rated up to 32 A and for cables concealed in walls at "\n        "less than 50 mm depth that are not otherwise mechanically protected.\\n"\n    ),\n    ("electrical", "US"): (\n        "PRACTICE REFERENCE - United States, electrical (reviewed):\\n"\n        "- Code: NEC (NFPA 70); the adopted edition differs by state and local authority having jurisdiction (AHJ).\\n"\n        "- NEC 240.4(D) small-conductor rule (copper): 14 AWG max 15 A, 12 AWG max 20 A, 10 AWG max 30 A overcurrent "\n        "protection, unless a specific exception applies (e.g. motor circuits).\\n"\n        "- 12 AWG copper NM-B on a 20 A breaker is the standard 20 A branch circuit; 14 AWG on 15 A is standard for "\n        "15 A circuits; 10 AWG on 30 A for typical 30 A appliance circuits. These are COMPLIANT in the typical case. "\n        "14 AWG copper on a 20 A breaker is NON_COMPLIANT.\\n"\n        "- NM cable ampacity is limited to the 60 C column.\\n"\n        "- Voltage drop: NEC informational notes suggest about 3% for branch circuits and 5% total; this is a "\n        "recommendation, not a general requirement.\\n"\n        "- GFCI and AFCI requirements depend on the location and the adopted NEC edition (210.8, 210.12).\\n"\n    ),\n}\n\n_AU_REGIONS = {"NSW", "VIC", "QLD", "WA", "SA", "TAS", "ACT", "NT", "AU", "AUSTRALIA", "NEW SOUTH WALES", "VICTORIA",\n               "QUEENSLAND", "WESTERN AUSTRALIA", "SOUTH AUSTRALIA", "TASMANIA", "AUSTRALIAN CAPITAL TERRITORY",\n               "NORTHERN TERRITORY"}\n_UK_REGIONS = {"UK", "UNITED KINGDOM", "ENGLAND", "SCOTLAND", "WALES", "NORTHERN IRELAND", "GB", "GREAT BRITAIN", "LONDON"}\n_US_STATES = {"ALABAMA", "ALASKA", "ARIZONA", "ARKANSAS", "CALIFORNIA", "COLORADO", "CONNECTICUT", "DELAWARE", "FLORIDA",\n              "GEORGIA", "HAWAII", "IDAHO", "ILLINOIS", "INDIANA", "IOWA", "KANSAS", "KENTUCKY", "LOUISIANA", "MAINE",\n              "MARYLAND", "MASSACHUSETTS", "MICHIGAN", "MINNESOTA", "MISSISSIPPI", "MISSOURI", "MONTANA", "NEBRASKA",\n              "NEVADA", "NEW HAMPSHIRE", "NEW JERSEY", "NEW MEXICO", "NEW YORK", "NORTH CAROLINA", "NORTH DAKOTA", "OHIO",\n              "OKLAHOMA", "OREGON", "PENNSYLVANIA", "RHODE ISLAND", "SOUTH CAROLINA", "SOUTH DAKOTA", "TENNESSEE", "TEXAS",\n              "UTAH", "VERMONT", "VIRGINIA", "WASHINGTON", "WEST VIRGINIA", "WISCONSIN", "WYOMING", "DISTRICT OF COLUMBIA",\n              "DC", "US", "USA", "UNITED STATES", "WASHINGTON DC", "NYC", "NEW YORK CITY"}\n\n\ndef _region_country(region: str) -> str:\n    r = " ".join((region or "").replace(",", " ").split()).upper()\n    if r in _AU_REGIONS or r.endswith(" AUSTRALIA"):\n        return "AU"\n    if r in _UK_REGIONS:\n        return "UK"\n    if r in _US_STATES or r.endswith(" USA") or r.endswith(" US"):\n        return "US"\n    return ""\n\n\ndef _practice_for(user_message: str) -> str:\n    """Answer rules for every tool, plus the reviewed practice reference for the trade and country."""\n    m = re.match(r"\\s*Trade:\\s*([^|\\n]+?)\\s*\\|\\s*Region:\\s*([^|\\n]+)", user_message or "")\n    extra = ""\n    if m:\n        ref = _PRACTICE.get((m.group(1).strip().lower(), _region_country(m.group(2))))\n        if ref:\n            extra = "\\n\\n" + ref\n    return _ANSWER_RULES + extra\n\n\nasync def ask_claude(system_prompt: str, user_message: str) -> dict:\n', 1), ('    """Call Anthropic API. Falls back to OpenAI on billing errors."""\n', '    """Call Anthropic API. Falls back to OpenAI on billing errors."""\n    system_prompt = system_prompt + _practice_for(user_message)\n', 1), ('VERSION           = "3.4.0"', 'VERSION           = "3.5.0"', 1)]


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
        abort("LL-3 is already applied.")
    if "LL-2a: streamable HTTP at /mcp" not in src:
        abort("Please apply ll2a_streamable_http.py first.")
    nl = "\r\n" if "\r\n" in src else "\n"
    out = src.replace(nl, "\n")
    for old, new, n in SWAPS:
        if out.count(old) != n:
            abort("Text not found exactly " + str(n) + " time(s):\n  " + old[:110])
        out = out.replace(old, new)
    try:
        ast.parse(out)
    except SyntaxError as e:
        abort("Patched file failed Python syntax check: " + str(e))
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(MAIN, MAIN + ".bak_" + stamp)
    with open(MAIN, "w", encoding="utf-8", newline="") as f:
        f.write(out.replace("\n", nl))
    print("OK - LL-3 applied (v3.5.0).")
    print("  done: normal case first, conditional verdicts, no unstated worst case")
    print("  done: reviewed electrical practice references for AU, UK and US")
    print("  done: model can be set with LEGIS_MODEL in Railway")
    print("Backup saved with .bak_" + stamp)


if __name__ == "__main__":
    main()
