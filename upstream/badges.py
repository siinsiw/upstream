"""Security and end-of-life badges for rows.

compute() is fast and offline. It reads the release notes the scan already fetched and the end-of-life tables
that refresh_eol() keeps in the cache (endoflife.date, one small JSON per product).

  security  the offered update's release notes name a CVE or GHSA id, or say security fix / update / patch /
            release or vulnerability (a negation such as "no security fixes" does not count)
  eol       the installed release line is past end of life, or reaches it within 90 days
  outdated  the installed major version is two or more majors behind the offered update

End-of-life data exists only for the products in PRODUCTS. An app maps to one when its winget id or its
registry display name matches a pattern written for it; anything else gets no end-of-life badge. Products
that ship a new major every few weeks (browsers, Notepad++) are "rolling": the release before the current
one is formally past end of life all the time, so they only get the badge after a grace period (or for an
LTS line such as Firefox ESR) and never the outdated one."""
from __future__ import annotations

import http.client
import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import net, versioning
from .i18n import N_

log = logging.getLogger("upstream.badges")

EOL_API = "https://endoflife.date/api"      # <EOL_API>/<product>.json; tests point this at a local server
EOL_TTL = 7 * 24 * 3600                      # a product's table is fetched again after this long
MAX_REQUESTS = 10                            # per refresh_eol() call
REQUEST_TIMEOUT = 8
MAX_BYTES = 2 * 1024 * 1024
SOON_DAYS = 90
ROLLING_GRACE_DAYS = 60
MAJORS_BEHIND = 2
MAX_IDS = 3


@dataclass(frozen=True)
class Badge:
    kind: str                       # security | eol | outdated | pinned (pinned is drawn from status.pin, not here)
    text: str                       # English template shown on the row (render(text, values) when shown)
    tooltip: str = ""               # English template, filled from the same values
    values: dict = field(default_factory=dict, hash=False)     # str values for both templates


# --- products --------------------------------------------------------------------------------

@dataclass(frozen=True)
class Product:
    slug: str                       # endoflife.date product name
    label: str                      # how a tooltip names it
    names: tuple                    # patterns for the whole registry display name
    winget: tuple                   # patterns for the whole winget id
    rolling: bool = False


def _product(slug: str, label: str, names: tuple = (), winget: tuple = (), rolling: bool = False) -> Product:
    return Product(slug, label, tuple(re.compile(p, re.I) for p in names),
                   tuple(re.compile(p, re.I) for p in winget), rolling)


_BITS = r"(?: \([^)]*\))?"
_VERSION = r"\d+(?:\.\d+)*"

# Every pattern must match the whole name, so "Python Launcher" or "MongoDB Compass" never take a database
# or runtime's end-of-life date. Entries here were checked against the slugs and cycles endoflife.date serves.
PRODUCTS: tuple[Product, ...] = (
    _product("python", "Python", (rf"Python {_VERSION}(?: \((?:32|64)-bit\))?",),
             (rf"Python\.Python\.{_VERSION}",)),
    _product("nodejs", "Node.js", (r"Node\.js",), (r"OpenJS\.NodeJS(?:\.LTS)?",)),
    _product("php", "PHP", (rf"PHP \d+(?:\.\d+)+{_BITS}",), (rf"PHP\.PHP(?:\.NTS)?(?:\.{_VERSION})?",)),
    _product("dotnet", ".NET",
             (r"Microsoft \.NET (?:Core )?(?:Runtime|SDK|Host|Host FX Resolver|AppHost Pack|Targeting Pack|Toolset)\b.*",
              r"Microsoft (?:ASP\.NET Core|Windows Desktop Runtime)\b.*"),
             (rf"Microsoft\.DotNet\.(?:Runtime|SDK|DesktopRuntime|AspNetCore|HostingBundle)(?:\.{_VERSION})?",)),
    _product("powershell", "PowerShell", (r"PowerShell \d+(?:-(?:x64|x86|arm64))?",), (r"Microsoft\.PowerShell",)),
    _product("eclipse-temurin", "Eclipse Temurin", (r"Eclipse Temurin (?:JDK|JRE) with Hotspot .*",),
             (r"EclipseAdoptium\.Temurin\.\d+\.(?:JDK|JRE)",)),
    _product("microsoft-build-of-openjdk", "Microsoft Build of OpenJDK",
             (r"Microsoft Build of OpenJDK with Hotspot .*",), (r"Microsoft\.OpenJDK\.\d+",)),
    _product("amazon-corretto", "Amazon Corretto", (r"Amazon Corretto\b.*",)),
    _product("mysql", "MySQL", (r"MySQL Server \d+\.\d+",)),
    _product("mariadb", "MariaDB", (rf"MariaDB \d+\.\d+{_BITS}",), (rf"MariaDB\.Server(?:\.{_VERSION})?",)),
    _product("postgresql", "PostgreSQL", (rf"PostgreSQL {_VERSION}{_BITS}",), (r"PostgreSQL\.PostgreSQL(?:\.\d+)?",)),
    _product("mongodb", "MongoDB", (r"MongoDB \d+\.\d+.*",), (rf"MongoDB\.Server(?:\.{_VERSION})?",)),
    _product("redis", "Redis", (rf"Redis(?: {_VERSION})?",)),
    _product("nginx", "nginx", (rf"nginx(?: {_VERSION})?",)),
    _product("firefox", "Firefox", (rf"Mozilla Firefox(?: ESR)?(?: {_VERSION})?{_BITS}",),
             (r"Mozilla\.Firefox(?:\.ESR)?(?:\.[A-Za-z]{2,3}(?:-[A-Za-z]{2,4})?)?",), rolling=True),
    _product("chrome", "Chrome", (r"Google Chrome",), (r"Google\.Chrome",), rolling=True),
    _product("notepad-plus-plus", "Notepad++", (rf"Notepad\+\+{_BITS}",), (r"Notepad\+\+\.Notepad\+\+",),
             rolling=True),
    _product("libreoffice", "LibreOffice", (rf"LibreOffice {_VERSION}",), (r"TheDocumentFoundation\.LibreOffice",)),
    _product("virtualbox", "VirtualBox", (rf"Oracle VM VirtualBox {_VERSION}",), (r"Oracle\.VirtualBox",)),
    _product("wireshark", "Wireshark", (rf"Wireshark {_VERSION}(?: \S+)?",), (r"WiresharkFoundation\.Wireshark",)),
)


def product_for(status) -> Product | None:
    """The end-of-life product an app belongs to, or None."""
    winget_id = str(getattr(status, "winget_id", None) or "").strip()
    name = str(getattr(getattr(status, "app", None), "name", "") or "").strip()
    for product in PRODUCTS:
        if any(p.fullmatch(winget_id) for p in product.winget) or any(p.fullmatch(name) for p in product.names):
            return product
    return None


# --- release notes ---------------------------------------------------------------------------

_ID = re.compile(r"\b(CVE-\d{4}-\d{4,7}|GHSA(?:-[a-z0-9]{4}){3})\b", re.I)
_PHRASE = re.compile(r"\bsecurity[\s-]+(?:fix(?:es)?|updates?|patch(?:es)?|release)\b|\bvulnerabilit(?:y|ies)\b", re.I)
_NEGATIONS = {"no", "not", "none", "without", "never", "neither", "nor", "zero", "cannot"}
_TURNS = {"only", "just", "but", "except", "instead"}
_NOT_NEGATING = re.compile(r"\b(?:no longer|not only)\b")
_REPORTING = {"report", "reports", "reporting", "disclose", "disclosure", "submit", "responsibly"}
_POLICY_WORDS = {"disclosure", "reporting", "policy"}
_NOTHING_AFTER = re.compile(r"[\s:\-–—(]*(?:none|n/a|nil)\b", re.I)


def _words_before(text: str, start: int) -> list[str]:
    """The last few words before `start`, within the same sentence."""
    clause = re.split(r"[.!?;\n]", text[max(0, start - 80):start])[-1]
    clause = _NOT_NEGATING.sub(" ", clause.replace("’", "'").lower())
    return re.findall(r"[\w']+", clause)[-5:]


def _negated(words: list[str]) -> bool:
    """True when a negation reaches the phrase. "no new features, only security fixes" is not negated: the
    nearest cue before the phrase is "only"."""
    for w in reversed(words):
        if w in _TURNS:
            return False
        if w in _NEGATIONS or w.endswith("n't"):
            return True
    return False


def _normal_id(found: str) -> str:
    return found.upper() if found[:3].upper() == "CVE" else "GHSA-" + found[5:].lower()


def _scan_notes(notes: str) -> tuple[list[str], bool]:
    """(advisory ids in order of appearance, whether a security phrase stands un-negated)."""
    ids: list[str] = []
    phrase = False
    for m in _ID.finditer(notes):
        if _negated(_words_before(notes, m.start())):
            continue
        found = _normal_id(m.group(1))
        if found not in ids:
            ids.append(found)
    for m in _PHRASE.finditer(notes):
        words = _words_before(notes, m.start())
        if _negated(words) or _NOTHING_AFTER.match(notes, m.end()):
            continue
        if m.group(0).lower().startswith("vuln"):
            following = re.match(r"\s+(\w+)", notes[m.end():])
            if _REPORTING & set(words) or (following and following.group(1).lower() in _POLICY_WORDS):
                continue
        phrase = True
    return ids, phrase


def _security(status, cache, today) -> Badge | None:
    update = getattr(status, "update", None)
    notes = getattr(update, "notes", "") if update else ""
    if not notes or not isinstance(notes, str):
        return None
    ids, phrase = _scan_notes(notes)
    if ids:
        tip = (N_("The release notes of this update mention {ids} and others.") if len(ids) > MAX_IDS
               else N_("The release notes of this update mention {ids}."))
        return Badge("security", N_("Security fix"), tip, {"ids": ", ".join(ids[:MAX_IDS])})
    if phrase:
        return Badge("security", N_("Security fix"),
                     N_("The release notes of this update mention a security fix or a vulnerability."))
    return None


# --- end of life -----------------------------------------------------------------------------

_NUMBERS = re.compile(r"\d+(?:\.\d+)*")


def _numbers(text) -> tuple[int, ...]:
    m = _NUMBERS.search(str(text or ""))
    return tuple(int(p) for p in m.group().split(".")) if m else ()


def find_cycle(cycles, version: str) -> dict | None:
    """The release line `version` belongs to: the cycle whose numbers are the longest prefix of the version's
    ('3.12' for 3.12.4, '20' for 20.11.1, '1.2' not for 1.20). None when no line fits."""
    have = _numbers(version)
    best, best_len = None, 0
    for item in cycles if isinstance(cycles, list) else ():
        name = item.get("cycle") if isinstance(item, dict) else None
        if not isinstance(name, str) or not re.fullmatch(r"\d+(?:\.\d+)*", name):
            continue
        want = tuple(int(p) for p in name.split("."))
        if len(want) > best_len and have[:len(want)] == want:
            best, best_len = item, len(want)
    return best


def _eol(status, cache, today) -> Badge | None:
    product = product_for(status)
    entry = cache.entry("eol", product.slug) if product and cache is not None else None
    cycle = find_cycle(entry.get("cycles"), status.app.version) if entry else None
    if cycle is None:
        return None
    values = {"product": product.label, "cycle": cycle["cycle"]}
    end = cycle.get("eol")
    if end is True:         # past end of life, no date given
        return Badge("eol", N_("End of life"),
                     N_("Support for {product} {cycle} has ended. It no longer receives security fixes."), values)
    if not isinstance(end, str):
        return None         # False: no end announced
    try:
        end_date = date.fromisoformat(end)
    except ValueError:
        return None
    values["date"] = end_date.isoformat()
    lts = bool(cycle.get("lts"))
    if end_date < today:
        if product.rolling and not lts and (today - end_date).days < ROLLING_GRACE_DAYS:
            return None
        return Badge("eol", N_("End of life"),
                     N_("Support for {product} {cycle} ended on {date}. It no longer receives security fixes."), values)
    if end_date <= today + timedelta(days=SOON_DAYS) and (lts or not product.rolling):
        return Badge("eol", N_("Support ends soon"),
                     N_("Support for {product} {cycle} ends on {date}. Plan to move to a newer release before then."),
                     values)
    return None


# --- major versions behind -------------------------------------------------------------------

def _outdated(status, cache, today) -> Badge | None:
    update, app = getattr(status, "update", None), status.app
    product = product_for(status)
    if update is None or (product and product.rolling):
        return None
    have, offered = versioning.parse_version(app.version), versioning.parse_version(update.version)
    if not have or not offered:
        return None
    # A first number of 1000 or more is a build number or a year, not a major version.
    if have[0] < 1 or max(have[0], offered[0]) >= 1000:
        return None
    if versioning.is_prerelease(update.version) or not versioning.is_newer(update.version, app.version):
        return None
    if offered[0] - have[0] < MAJORS_BEHIND:
        return None
    return Badge("outdated", N_("Outdated"),
                 N_("The installed version ({installed}) is at least two major versions behind {latest}."),
                 {"installed": app.version, "latest": update.version})


def compute(status, cache=None, enabled: bool = True, today: date | None = None) -> list[Badge]:
    """Badges for one scanned app. Fast and offline: uses the release notes already fetched and the cached
    end-of-life tables. Never raises."""
    if not enabled:
        return []
    today = today or date.today()
    badges: list[Badge] = []
    for rule in (_security, _eol, _outdated):
        try:
            badge = rule(status, cache, today)
        except Exception:
            log.warning("%s badge failed for %s", rule.__name__.strip("_"),
                        getattr(getattr(status, "app", None), "name", "?"), exc_info=True)
            continue
        if badge:
            badges.append(badge)
    return badges


# --- end-of-life tables ----------------------------------------------------------------------

def _fetch(slug: str) -> list[dict]:
    """The release lines of one product, cut down to what compute() reads."""
    req = urllib.request.Request(f"{EOL_API}/{slug}.json",
                                 headers={"Accept": "application/json", "User-Agent": net.USER_AGENT})
    with net.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        data = json.loads(resp.read(MAX_BYTES))
    if not isinstance(data, list):
        raise ValueError("unexpected end-of-life data")
    cycles = [{"cycle": item["cycle"], "eol": item["eol"], "lts": bool(item.get("lts"))}
              for item in data if isinstance(item, dict) and isinstance(item.get("cycle"), str)
              and isinstance(item.get("eol"), (str, bool))]
    if not cycles:
        raise ValueError("no release lines in the end-of-life data")
    return cycles


def _due(cache, products, now: float) -> list[Product]:
    """The products whose table is missing or older than a week, missing ones first, then the oldest."""
    due = []
    for product in products:
        entry = cache.entry("eol", product.slug)
        fetched = entry.get("ts", 0) if entry and isinstance(entry.get("cycles"), list) else 0
        if now - fetched > EOL_TTL:
            due.append((fetched, product))
    due.sort(key=lambda pair: pair[0])
    return [product for _, product in due]


def refresh_eol(cache, statuses, now: float | None = None) -> None:
    """Fetch endoflife.date tables for the products among `statuses` that are missing or stale (scan worker
    thread, a few requests at most). Never raises."""
    try:
        products: dict[str, Product] = {}
        for status in statuses:
            product = product_for(status)
            if product:
                products.setdefault(product.slug, product)
        for product in _due(cache, products.values(), time.time() if now is None else now)[:MAX_REQUESTS]:
            try:
                cache.put("eol", product.slug, {"cycles": _fetch(product.slug)})
            except urllib.error.HTTPError as e:
                e.close()
                if e.code == 404:   # no such product: do not ask again for a week
                    cache.put("eol", product.slug, {"cycles": []})
                else:
                    log.info("endoflife.date answered %s for %s", e.code, product.slug)
            except ValueError as e:
                log.info("endoflife.date data for %s unusable: %s", product.slug, e)
            except (OSError, http.client.HTTPException) as e:
                log.info("endoflife.date unreachable (%s); skipping the rest", e)
                break
    except Exception:
        log.warning("refreshing end-of-life data failed", exc_info=True)
