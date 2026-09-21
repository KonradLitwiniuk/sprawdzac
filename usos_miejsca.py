#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
usos_miejsca.py — monitoruje wolne miejsca w grupach zajeciowych na USOSweb PWr.

Czyta PUBLICZNA strone katalogu przedmiotow:
    https://web.usos.pwr.edu.pl/kontroler.php?_action=katalog2/przedmioty/pokazZajecia
        &zaj_cyk_id=<ID_ZAJEC>&gr_nr=<NR_GRUPY>

Ta strona nie wymaga logowania i pokazuje wiersze:
    "Limit miejsc:"            -> ile miejsc w grupie
    "Liczba osob w grupie:"    -> ile juz zajetych
    "Prowadzacy:"              -> kto prowadzi
Nie zalezy tez od tego, ktora tura rejestracji jest akurat otwarta.

Skrypt sprawdza grupy co jakis czas i krzyczy, gdy:
  * pojawi sie wolne miejsce (ktos zrezygnowal),
  * zwiekszy sie limit miejsc (dodali miejsca),
  * pojawi sie zupelnie nowa grupa u tego prowadzacego.

Wymagania: Python 3.8+. Zadnych bibliotek do doinstalowania.

Szybki start (po kodzie przedmiotu - skrypt sam znajdzie reszte):
    python3 usos_miejsca.py --course 04IST0-25S103O03096G --lecturer Komarnicki --once
    python3 usos_miejsca.py --course 04IST0-25S103O03096G --lecturer Komarnicki

Gdyby wyszukiwanie po kodzie nie zadzialalo - wtedy recznie, przez ID_ZAJEC:
    python3 usos_miejsca.py --unit 123456 --lecturer Komarnicki --once --debug

Gdzie wziac ID_ZAJEC (zaj_cyk_id):
    USOSweb -> Katalog -> Przedmioty -> znajdz swoj przedmiot -> wybierz cykl
    (semestr) -> lista grup. Kliknij dowolna grupe i spojrz na adres w pasku
    przegladarki: jest tam ...zaj_cyk_id=123456&gr_nr=3. Liczba przy
    zaj_cyk_id to wlasnie to, czego szuka skrypt (jedna na typ zajec:
    osobno wyklad, osobno cwiczenia/laborka).

    Mozesz tez po prostu wkleic caly adres:
        python3 usos_miejsca.py --url "https://web.usos.pwr.edu.pl/kontroler.php?..."
"""

from __future__ import annotations

import argparse
import ctypes
import html
import json
import os
import platform
import random
import re
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime

USOS_WEB_URL = os.environ.get("USOS_WEB_URL", "https://web.usos.pwr.edu.pl")
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

# Etykiety wierszy na stronie grupy. Kazdy wpis to lista wariantow - bierzemy
# pierwszy, ktory sie dopasuje (USOSweb bywa roznie na roznych uczelniach).
ROW_LABELS = {
    "total": ["limit miejsc"],
    "occupied": ["liczba osob w grupie", "liczba osob"],
    "lecturer": ["prowadzacy", "nauczyciel"],
    "term": ["termin i miejsce", "terminy"],
    "course": ["przedmiot"],
    "kind": ["zajecia", "typ zajec"],
    "group": ["grupa nr", "grupa"],
}


# --------------------------------------------------------------------------
# pomocnicze
# --------------------------------------------------------------------------

def fold(text: str) -> str:
    """Male litery bez polskich ogonkow - do porownywania etykiet i nazwisk."""
    text = text.replace("ł", "l").replace("Ł", "L")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.lower().strip()


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(message: str) -> None:
    print(f"[{now_str()}] {message}", flush=True)


# --------------------------------------------------------------------------
# pobieranie
# --------------------------------------------------------------------------

def build_group_url(unit_id: str, group_number: str) -> str:
    params = urllib.parse.urlencode(
        {
            "_action": "katalog2/przedmioty/pokazZajecia",
            "zaj_cyk_id": unit_id,
            "gr_nr": group_number,
            "lang": "pl",
        }
    )
    return f"{USOS_WEB_URL}/kontroler.php?{params}"


def fetch(url: str, timeout: int = 30, retries: int = 3) -> str:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "pl,en;q=0.7",
                    # bez gzip, zeby nie trzeba bylo rozpakowywac
                    "Accept-Encoding": "identity",
                },
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
            charset = "utf-8"
            match = re.search(rb'charset=["\']?([\w-]+)', raw[:4000], re.I)
            if match:
                charset = match.group(1).decode("ascii", "ignore")
            return raw.decode(charset, errors="replace")
        except Exception as error:  # noqa: BLE001 - chcemy ponowic kazdy blad sieci
            last_error = error
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"nie udalo sie pobrac {url}: {last_error}")


# --------------------------------------------------------------------------
# parsowanie strony grupy
# --------------------------------------------------------------------------

TAG_RE = re.compile(r"<[^>]+>")
ROW_RE = re.compile(r"<tr\b.*?</tr>", re.I | re.S)
CELL_RE = re.compile(r"<t[dh]\b.*?</t[dh]>", re.I | re.S)


def strip_tags(fragment: str) -> str:
    fragment = re.sub(r"<\s*br\s*/?\s*>", "\n", fragment, flags=re.I)
    fragment = re.sub(r"</\s*(p|div|li|tr)\s*>", "\n", fragment, flags=re.I)
    text = TAG_RE.sub(" ", fragment)
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def main_content(page: str) -> str:
    """Wytnij glowna tresc strony, jak robi to planer Solvro."""
    match = re.search(r'id=["\']layout-main-content["\']', page, re.I)
    if match:
        return page[match.start():]
    return page


def parse_rows(page: str) -> list[tuple[str, str]]:
    """Zwroc pary (etykieta, wartosc) ze wszystkich wierszy tabel."""
    rows: list[tuple[str, str]] = []
    for row_html in ROW_RE.findall(main_content(page)):
        cells = CELL_RE.findall(row_html)
        if len(cells) < 2:
            continue
        label = strip_tags(cells[0]).rstrip(":").strip()
        value = strip_tags(cells[1]).strip()
        if label:
            rows.append((label, value))
    return rows


def pick(rows: list[tuple[str, str]], key: str) -> str:
    """Znajdz wartosc wiersza po etykiecie (dopasowanie bez ogonkow)."""
    wanted = ROW_LABELS[key]
    for variant in wanted:
        for label, value in rows:
            folded = fold(label)
            if folded == variant or folded.startswith(variant):
                return value
    return ""


def to_int(text: str) -> int | None:
    match = re.search(r"\d+", text.replace(" ", " "))
    return int(match.group()) if match else None


class Group:
    def __init__(self, unit_id: str, group_number: str, url: str) -> None:
        self.unit_id = unit_id
        self.group_number = group_number
        self.url = url
        self.course = ""
        self.kind = ""
        self.lecturer = ""
        self.term = ""
        self.total: int | None = None
        self.occupied: int | None = None

    @property
    def key(self) -> str:
        return f"{self.unit_id}/{self.group_number}"

    @property
    def free(self) -> int | None:
        if self.total is None or self.occupied is None:
            return None
        return max(0, self.total - self.occupied)

    @property
    def valid(self) -> bool:
        # strona istnieje i wyglada jak strona grupy
        return self.total is not None or self.occupied is not None

    def describe(self) -> str:
        spots = "?" if self.total is None else f"{self.occupied}/{self.total}"
        name = self.course or "(przedmiot ?)"
        kind = f" [{self.kind}]" if self.kind else ""
        who = self.lecturer.replace("\n", ", ") or "(prowadzacy ?)"
        return f"gr. {self.group_number} {name}{kind} - {who} - {spots} miejsc"


def parse_group(
    unit_id: str, group_number: str, page: str, url: str
) -> tuple[Group, list[tuple[str, str]]]:
    group = Group(unit_id, group_number, url)
    rows = parse_rows(page)
    group.total = to_int(pick(rows, "total"))
    group.occupied = to_int(pick(rows, "occupied"))
    group.lecturer = pick(rows, "lecturer")
    group.term = pick(rows, "term")
    group.course = pick(rows, "course")
    group.kind = pick(rows, "kind")

    # awaryjnie: nazwisko prowadzacego bywa poza tabela (naglowek strony)
    if not group.lecturer:
        match = re.search(
            r"[Pp]rowadz[^<:]{0,10}:\s*(?:</[^>]+>\s*)*([^<\n]{3,120})", page
        )
        if match:
            group.lecturer = html.unescape(match.group(1)).strip()
    return group, rows


# --------------------------------------------------------------------------
# powiadomienia
# --------------------------------------------------------------------------

def beep(times: int = 3) -> None:
    for _ in range(times):
        sys.stdout.write("\a")
        sys.stdout.flush()
        time.sleep(0.25)


def desktop_notify(title: str, message: str) -> None:
    system = platform.system()
    try:
        if system == "Linux":
            subprocess.run(
                ["notify-send", "-u", "critical", title, message],
                check=False,
                timeout=10,
            )
        elif system == "Darwin":
            safe_message = message.replace('"', "'").replace("\n", " ")
            safe_title = title.replace('"', "'")
            subprocess.run(
                [
                    "osascript",
                    "-e",
                    f'display notification "{safe_message}" with title '
                    f'"{safe_title}" sound name "Glass"',
                ],
                check=False,
                timeout=10,
            )
        elif system == "Windows":
            def box() -> None:
                try:
                    ctypes.windll.user32.MessageBoxW(  # type: ignore[attr-defined]
                        0, message, title, 0x1000 | 0x40
                    )
                except Exception:  # noqa: BLE001
                    pass

            threading.Thread(target=box, daemon=True).start()
    except Exception:  # noqa: BLE001 - powiadomienie nigdy nie moze wywalic skryptu
        pass


def ntfy_notify(topic: str, title: str, message: str, click: str = "") -> None:
    """Powiadomienie na telefon przez ntfy.sh - bez zakladania konta.
    Zainstaluj apke ntfy, zasubskrybuj wymyslony przez siebie temat i podaj
    go w --ntfy (np. --ntfy konrad-usos-7f3a)."""
    try:
        # naglowki HTTP musza byc ASCII - ogonki ida w tresci, nie w tytule
        ascii_title = (
            unicodedata.normalize("NFKD", title)
            .encode("ascii", "ignore")
            .decode("ascii")
        ) or "USOS"
        headers = {
            "Title": ascii_title,
            "Priority": "urgent",
            "Tags": "rotating_light",
        }
        if click:
            headers["Click"] = click
        request = urllib.request.Request(
            f"https://ntfy.sh/{urllib.parse.quote(topic)}",
            data=message.encode("utf-8"),
            headers=headers,
            method="POST",
        )
        urllib.request.urlopen(request, timeout=15).read()
    except Exception as error:  # noqa: BLE001
        log(f"  (ntfy nie zadzialalo: {error})")


def telegram_notify(token: str, chat_id: str, message: str) -> None:
    try:
        data = urllib.parse.urlencode(
            {"chat_id": chat_id, "text": message, "disable_web_page_preview": "false"}
        ).encode()
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data
        )
        urllib.request.urlopen(request, timeout=15).read()
    except Exception as error:  # noqa: BLE001
        log(f"  (telegram nie zadzialal: {error})")


class Notifier:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args

    def alert(self, title: str, message: str, url: str = "") -> None:
        print()
        print("=" * 68)
        print(f"  {title}")
        for line in message.splitlines():
            print(f"  {line}")
        if url:
            print(f"  {url}")
        print("=" * 68)
        print(flush=True)
        beep()
        if not self.args.no_desktop:
            desktop_notify(title, message)
        if self.args.ntfy:
            ntfy_notify(self.args.ntfy, title, message + (f"\n{url}" if url else ""), url)
        if self.args.telegram_token and self.args.telegram_chat:
            telegram_notify(
                self.args.telegram_token,
                self.args.telegram_chat,
                f"{title}\n{message}\n{url}",
            )
        if self.args.open:
            target = self.args.open_url or url
            if target:
                try:
                    webbrowser.open(target)
                except Exception:  # noqa: BLE001
                    pass


# --------------------------------------------------------------------------
# stan miedzy uruchomieniami
# --------------------------------------------------------------------------

def load_state(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def save_state(path: str, state: dict) -> None:
    try:
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=1)
    except OSError as error:
        log(f"  (nie udalo sie zapisac stanu: {error})")


# --------------------------------------------------------------------------
# logika sprawdzania
# --------------------------------------------------------------------------

def parse_group_spec(spec: str) -> list[str]:
    """'1-12' albo '1,3,5' albo '1-4,7' -> lista numerow grup."""
    numbers: list[str] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            numbers.extend(str(n) for n in range(int(start), int(end) + 1))
        else:
            numbers.append(str(int(part)))
    return numbers


def course_page_urls(course_code: str) -> list[str]:
    """Adresy publicznej strony przedmiotu - probujemy po kolei."""
    code = urllib.parse.quote(course_code)
    return [
        f"{USOS_WEB_URL}/kontroler.php?_action=katalog2/przedmioty/pokazPrzedmiot"
        f"&kod={code}&lang=pl",
        f"{USOS_WEB_URL}/kontroler.php?_action=katalog2/przedmioty/szukajPrzedmiotu"
        f"&method=default&kod={code}&lang=pl",
        f"{USOS_WEB_URL}/kontroler.php?_action=katalog2/przedmioty/szukajPrzedmiotu"
        f"&method=faculty_groups&kod={code}&lang=pl",
    ]


_UNIT_CACHE: dict[str, list[str]] = {}


def discover_units(course_code: str, debug: bool = False) -> list[str]:
    """Znajdz zaj_cyk_id wszystkich typow zajec przedmiotu po jego kodzie.

    Strona przedmiotu w katalogu linkuje do list grup, a w tych linkach siedzi
    zaj_cyk_id - wystarczy je wylowic, nie trzeba znac ukladu strony.
    """
    if course_code in _UNIT_CACHE:
        return _UNIT_CACHE[course_code]

    found: list[str] = []
    for url in course_page_urls(course_code):
        try:
            page = fetch(url)
        except RuntimeError as error:
            if debug:
                log(f"  DEBUG nie poszlo {url}: {error}")
            continue

        ids = re.findall(r"zaj_cyk_id=(\d+)", html.unescape(page))
        if debug:
            log(f"  DEBUG {url} -> znalezione zaj_cyk_id: {sorted(set(ids))}")
        for unit in ids:
            if unit not in found:
                found.append(unit)
        if found:
            break

        # czasem trzeba przejsc przez wynik wyszukiwania do strony przedmiotu
        link = re.search(
            r'href="([^"]*pokazPrzedmiot[^"]*)"', html.unescape(page)
        )
        if link:
            next_url = urllib.parse.urljoin(url, link.group(1))
            try:
                page2 = fetch(next_url)
            except RuntimeError:
                continue
            for unit in re.findall(r"zaj_cyk_id=(\d+)", html.unescape(page2)):
                if unit not in found:
                    found.append(unit)
            if found:
                break

    if found:
        _UNIT_CACHE[course_code] = found
        log(f"  Znalazlem zajecia dla {course_code}: {', '.join(found)}")
        log(f"  (mozesz je podac na stale przez --unit {' --unit '.join(found)})")
    else:
        log(
            f"  Nie znalazlem zaj_cyk_id dla kodu {course_code}. "
            "Wejdz w USOSweb w katalog przedmiotu, kliknij grupe i wklej adres "
            "przez --url."
        )
    return found


def units_from_args(args: argparse.Namespace) -> list[str]:
    units: list[str] = list(args.unit or [])
    for url in args.url or []:
        found = re.findall(r"zaj_cyk_id=(\d+)", url)
        if not found:
            log(f"UWAGA: w adresie nie ma zaj_cyk_id, pomijam: {url}")
        units.extend(found)
    for course_code in getattr(args, "course", None) or []:
        units.extend(discover_units(course_code, debug=args.debug))
    # unikalne, kolejnosc zachowana
    seen: set[str] = set()
    result = []
    for unit in units:
        if unit not in seen:
            seen.add(unit)
            result.append(unit)
    return result


def scan_unit(
    unit_id: str,
    group_numbers: list[str] | None,
    args: argparse.Namespace,
) -> list[Group]:
    """Pobierz grupy danych zajec. Bez --groups skanuje 1,2,3... do konca."""
    groups: list[Group] = []
    misses = 0
    numbers = group_numbers or [str(n) for n in range(1, args.max_groups + 1)]

    for number in numbers:
        url = build_group_url(unit_id, number)
        try:
            page = fetch(url)
        except RuntimeError as error:
            log(f"  {unit_id}/{number}: {error}")
            misses += 1
            if group_numbers is None and misses >= 3:
                break
            continue

        group, rows = parse_group(unit_id, number, page, url)

        if args.debug:
            log(f"  DEBUG {unit_id}/{number} -> {url}")
            for label, value in rows[:25]:
                print(f"        | {label!r}: {value[:90]!r}")

        if not group.valid:
            misses += 1
            if group_numbers is None and misses >= 3:
                break
            continue

        misses = 0
        groups.append(group)
        time.sleep(args.delay)

    return groups


def matches_lecturer(group: Group, needle: str) -> bool:
    if not needle:
        return True
    return fold(needle) in fold(group.lecturer)


def check_once(args: argparse.Namespace, notifier: Notifier) -> None:
    state = load_state(args.state)
    group_numbers = parse_group_spec(args.groups) if args.groups else None
    units = units_from_args(args)
    if not units:
        log("Brak zajec do sprawdzenia. Podaj --unit ID albo --url ADRES.")
        return

    seen_now: dict[str, dict] = {}
    any_match = False

    for unit_id in units:
        log(f"Sprawdzam zajecia {unit_id} ...")
        for group in scan_unit(unit_id, group_numbers, args):
            if not matches_lecturer(group, args.lecturer):
                continue
            any_match = True

            previous = state.get(group.key)
            current = {
                "occupied": group.occupied,
                "total": group.total,
                "lecturer": group.lecturer,
                "course": group.course,
                "kind": group.kind,
                "term": group.term,
            }
            seen_now[group.key] = current

            free = group.free
            log(f"  {group.describe()}  -> wolne: {free}")

            if args.alert_when_free:
                # tryb bez pamieci - do chmury/crona: krzycz za kazdym razem,
                # gdy jest wolne miejsce, bez porownywania z poprzednim stanem
                if free:
                    notifier.alert(
                        "USOS: sa wolne miejsca!",
                        f"{group.describe()}\nWolnych miejsc: {free}",
                        group.url,
                    )
                continue

            if previous is None:
                if args.lecturer and free:
                    notifier.alert(
                        "USOS: wolne miejsce!",
                        f"{group.describe()}\nWolnych miejsc: {free}",
                        group.url,
                    )
                continue

            previous_free = None
            if previous.get("total") is not None and previous.get("occupied") is not None:
                previous_free = max(0, previous["total"] - previous["occupied"])

            limit_grew = (
                group.total is not None
                and previous.get("total") is not None
                and group.total > previous["total"]
            )

            if free and not previous_free:
                if limit_grew:
                    title = "USOS: dodali miejsca!"
                    detail = f"Limit: {previous['total']} -> {group.total}"
                else:
                    title = "USOS: zwolnilo sie miejsce!"
                    detail = (
                        f"Bylo {previous.get('occupied')}/{previous.get('total')}"
                    )
                notifier.alert(
                    title,
                    f"{group.describe()}\n{detail}\nWolnych miejsc: {free}",
                    group.url,
                )
            elif limit_grew:
                notifier.alert(
                    "USOS: zwiekszyli limit miejsc!",
                    f"{group.describe()}\nLimit: {previous['total']} -> {group.total}"
                    f"\nWolnych miejsc: {free}",
                    group.url,
                )
            elif args.verbose and current != previous:
                log(f"  zmiana bez wolnych miejsc: {previous} -> {current}")

    # nowa grupa u tego prowadzacego (np. dorzucili grupe w kolejnej turze)
    known = {key for key in state if key.split("/")[0] in units}
    for key in seen_now:
        if key not in known and known:
            notifier.alert(
                "USOS: pojawila sie nowa grupa!",
                f"{key}: {seen_now[key].get('course', '')} "
                f"{seen_now[key].get('lecturer', '')}",
                build_group_url(*key.split("/")),
            )

    if not any_match:
        if args.lecturer:
            log(
                f"Nie znalazlem zadnej grupy z prowadzacym '{args.lecturer}'. "
                "Sprawdz pisownie nazwiska albo odpal z --debug (i bez --lecturer), "
                "zeby zobaczyc, co skrypt widzi na stronie."
            )
        else:
            log("Nie znalazlem zadnych grup - sprawdz zaj_cyk_id (--debug pomoze).")

    state.update(seen_now)
    save_state(args.state, state)


# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Monitor wolnych miejsc w grupach na USOSweb PWr "
        "(publiczny katalog, bez logowania).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--unit", action="append", metavar="ZAJ_CYK_ID",
        help="ID zajec z USOSweb (zaj_cyk_id). Mozna podac wiele razy.",
    )
    parser.add_argument(
        "--url", action="append", metavar="ADRES",
        help="Wklejony adres strony grupy z USOSweb - skrypt sam wyciagnie zaj_cyk_id.",
    )
    parser.add_argument(
        "--course", action="append", metavar="KOD",
        help="Kod przedmiotu z USOSa, np. 04IST0-25S103O03096G - skrypt sam "
             "znajdzie zaj_cyk_id wszystkich typow zajec.",
    )
    parser.add_argument(
        "--lecturer", default="", metavar="NAZWISKO",
        help="Filtruj po nazwisku prowadzacego (bez wielkosci liter i ogonkow).",
    )
    parser.add_argument(
        "--groups", default="", metavar="ZAKRES",
        help="Numery grup, np. '1-12' albo '2,5,7'. Domyslnie skanuje od 1 w gore.",
    )
    parser.add_argument(
        "--max-groups", type=int, default=30,
        help="Ile numerow grup maksymalnie sprobowac przy autoskanie (domyslnie 30).",
    )
    parser.add_argument(
        "--interval", type=int, default=300, metavar="SEK",
        help="Co ile sekund sprawdzac (domyslnie 300 = 5 min). Nie schodz ponizej 120.",
    )
    parser.add_argument("--once", action="store_true", help="Sprawdz raz i zakoncz (do crona).")
    parser.add_argument(
        "--alert-when-free", action="store_true",
        help="Alarmuj za kazdym razem, gdy sa wolne miejsca (bez pamietania "
             "poprzedniego stanu). Do uruchomien w chmurze, gdzie plik stanu "
             "nie przezywa miedzy sprawdzeniami.",
    )
    parser.add_argument(
        "--delay", type=float, default=1.0,
        help="Przerwa miedzy pobraniem kolejnych grup w sekundach (domyslnie 1).",
    )
    parser.add_argument(
        "--state", default="usos_miejsca_state.json",
        help="Plik z zapamietanym stanem (domyslnie usos_miejsca_state.json).",
    )
    parser.add_argument("--ntfy", default="", metavar="TEMAT",
                        help="Wyslij powiadomienie na telefon przez ntfy.sh (nazwa tematu).")
    parser.add_argument("--telegram-token", default="", help="Token bota Telegram.")
    parser.add_argument("--telegram-chat", default="", help="Chat ID na Telegramie.")
    parser.add_argument("--no-desktop", action="store_true",
                        help="Nie pokazuj powiadomien systemowych.")
    parser.add_argument("--open", action="store_true",
                        help="Otworz strone w przegladarce, gdy cos sie zwolni.")
    parser.add_argument("--open-url", default="", metavar="ADRES",
                        help="Co otworzyc przy --open zamiast strony katalogu - "
                             "wklej tu adres swojej strony zapisow w USOSweb, "
                             "zebys wchodzil od razu tam, gdzie sie klika.")
    parser.add_argument("--debug", action="store_true",
                        help="Wypisz wiersze tabeli, ktore skrypt widzi na stronie.")
    parser.add_argument("--verbose", action="store_true",
                        help="Loguj takze zmiany, ktore nie daja wolnych miejsc.")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    if args.interval < 60:
        log("Interwal ponizej 60 s jest nieuprzejmy dla USOSa - ustawiam 60.")
        args.interval = 60

    if not (args.unit or args.url or args.course):
        build_parser().print_help()
        return 2

    notifier = Notifier(args)

    if args.once:
        check_once(args, notifier)
        return 0

    log(
        f"Start. Sprawdzam co {args.interval} s. "
        f"Prowadzacy: {args.lecturer or '(wszyscy)'}. Ctrl+C konczy."
    )
    while True:
        try:
            check_once(args, notifier)
        except KeyboardInterrupt:
            log("Koniec.")
            return 0
        except Exception as error:  # noqa: BLE001 - petla ma przezyc wszystko
            log(f"Blad rundy (lecimy dalej): {error}")
        # lekki rozrzut, zeby nie walic co do sekundy
        sleep_for = args.interval + random.randint(0, max(1, args.interval // 10))
        try:
            time.sleep(sleep_for)
        except KeyboardInterrupt:
            log("Koniec.")
            return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
