"""Check that ALH and Bank cows in milk can be rebuilt from DairyComp events.

Bank is the cows in milk in pens 89 to 93. ALH is every other cow in milk,
leaving out pen 150. The inventory file holds today's pens, so it gives the
right answer for today. This script rebuilds the same two numbers from the
events file alone and compares them cow by cow. It also checks every MOVE
line in the history against the pen the rebuild had her in.

Nothing is written to the database.

    python scripts/check_pen_cow_counts.py "C:\\path\\to\\DCEXPORTALH"

Rebuild rules, per cow, in date order:

- She is in milk from her fresh date until DRY, SOLD, or DIED.
- An ABORT that opens lactation 1 counts as a fresh only when a later move
  shows her in pen 1, pen 6, or pens 89 to 93. The next line is often her
  leaving that pen. An early abort that is sold from a heifer pen does not
  start a milking spell.
- TOCLOSE is a close-up pen change only. It does not end milking. An abort
  heifer often has TOCLOSE on the same day she enters milk.
- A MOVE remark such as F006T092 puts her in pen 92.
- FRESH and DRY move her without a MOVE line, so her pen is not known after
  them. A pen that is not known counts as ALH.
- The same remark says she left pen 6. When her pen was not known, she is
  placed in the pen she left, back to the day it stopped being known.
- Moves on the same day are put in the order that links them up. The file
  does not keep them in order.
- She counts from her fresh date, the same as the dashboard.
- Events dated before her earliest entry date are ignored.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.cows_in_milk import (  # noqa: E402
    BNK_MILK_PENS,
    MOVE_REMARK,
    MOVED_PEN,
    MilkSegment,
    MoveFact,
    _is_milking_inventory_row,
    build_milk_segments,
    pen_number,
)
from services.duplicate_animals import ear_tag_key  # noqa: E402

ENCODING = "windows-1252"

BANK = "Bank"
ALH = "ALH"
MOVED = "Pen 150"
PLACES = (BANK, ALH, MOVED)


def place_for_pen(pen: int | None) -> str:
    if pen in BNK_MILK_PENS:
        return BANK
    if pen == MOVED_PEN:
        return MOVED
    return ALH


def parse_date(text: str | None) -> dt.date | None:
    text = (text or "").strip()
    if not text or text in {"-", "00000000"}:
        return None
    for fmt in ("%d/%m/%y", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_number(text: str | None) -> float | None:
    try:
        return float((text or "").strip())
    except ValueError:
        return None


def show_date(day: dt.date | None) -> str:
    return day.strftime("%d %b %Y") if day else "no date"


def cow_key(cow_id: str | None, etag: str | None) -> str:
    """Ear tag identifies the animal. A cow number can be reused."""
    tag = ear_tag_key(etag)
    if tag and tag != "-":
        return tag
    return f"ID {(cow_id or '').strip()}"


def read_rows(path: Path) -> Iterator[dict[str, str]]:
    with path.open(newline="", encoding=ENCODING) as handle:
        reader = csv.reader(handle)
        header = [name.strip().upper() for name in next(reader)]
        for row in reader:
            if len(row) > len(header):
                continue  # The import skips these lines too.
            yield {name: value.strip() for name, value in zip(header, row) if name}


@dataclass(frozen=True)
class InventoryCow:
    cow_id: str
    pen: int | None
    bname: str
    rpro: str
    lact: float
    dim: float | None
    fdat: dt.date | None
    counted: bool

    @property
    def place(self) -> str:
        return place_for_pen(self.pen)

    def describe(self) -> str:
        pen = f"pen {self.pen}" if self.pen is not None else "no pen"
        if self.counted:
            status = "in milk"
        elif self.lact <= 0:
            status = "not calved"
        elif self.rpro == "DRY":
            status = "dry"
        else:
            status = f"fresh, day {int(self.dim) if self.dim is not None else '?'}"
        return f"{pen}, {status}"


def load_inventory(path: Path) -> tuple[dict[str, InventoryCow], dt.date | None]:
    cows: dict[str, InventoryCow] = {}
    file_dates: Counter[dt.date] = Counter()
    for row in read_rows(path):
        key = cow_key(row.get("ID"), row.get("ETAG"))
        if key in cows:
            continue
        rpro = (row.get("RPRO") or "").upper()
        lact = parse_number(row.get("LACT")) or 0.0
        dim = parse_number(row.get("DIM"))
        fdat = parse_date(row.get("FDAT"))
        cows[key] = InventoryCow(
            cow_id=row.get("ID", ""),
            pen=pen_number(row.get("PEN")),
            bname=(row.get("BNAME") or "").upper(),
            rpro=rpro,
            lact=lact,
            dim=dim,
            fdat=fdat,
            counted=_is_milking_inventory_row(rpro, lact, dim),
        )
        # A milking cow's days in milk run up to the day the file was written.
        if lact > 0 and rpro != "DRY" and fdat is not None and dim is not None:
            file_dates[fdat + dt.timedelta(days=int(dim))] += 1
    file_date = file_dates.most_common(1)[0][0] if file_dates else None
    return cows, file_date


@dataclass(frozen=True)
class Event:
    date: dt.date
    order: int
    name: str
    remark: str
    fdat: dt.date | None
    lact: int


@dataclass
class EventFile:
    by_cow: dict[str, list[Event]] = field(default_factory=lambda: defaultdict(list))
    cow_ids: dict[str, str] = field(default_factory=dict)
    entry_dates: dict[str, dt.date] = field(default_factory=dict)
    first_date: dt.date | None = None
    last_date: dt.date | None = None
    before_entry: Counter[str] = field(default_factory=Counter)
    odd_moves: list[str] = field(default_factory=list)


def load_events(path: Path) -> EventFile:
    out = EventFile()
    for order, row in enumerate(read_rows(path)):
        date = parse_date(row.get("DATE"))
        if date is None:
            continue
        name = (row.get("EVENT") or "").upper()
        key = cow_key(row.get("ID"), row.get("ETAG"))
        edat = parse_date(row.get("EDAT"))
        if edat is not None:
            previous = out.entry_dates.get(key)
            if previous is None or edat < previous:
                out.entry_dates[key] = edat
        fdat = parse_date(row.get("FDAT"))
        lact = int(parse_number(row.get("LACT")) or 0)
        remark = (row.get("REMARK") or "").upper()
        if name == "MOVE" and not MOVE_REMARK.match(remark):
            out.odd_moves.append(f"{row.get('ID', '')} {show_date(date)} {remark}")
        out.by_cow[key].append(
            Event(date=date, order=order, name=name, remark=remark, fdat=fdat, lact=lact)
        )
        out.cow_ids[key] = row.get("ID", "")
        if out.first_date is None or date < out.first_date:
            out.first_date = date
        if out.last_date is None or date > out.last_date:
            out.last_date = date

    # Drop events before each cow's earliest entry date once every row is read.
    filtered: dict[str, list[Event]] = defaultdict(list)
    for key, rows in out.by_cow.items():
        entry = out.entry_dates.get(key)
        if entry is None:
            filtered[key] = rows
            continue
        for event in rows:
            if event.date < entry:
                out.before_entry[event.name] += 1
                continue
            filtered[key].append(event)
    out.by_cow = filtered
    return out


@dataclass
class MoveIssue:
    cow_id: str
    date: dt.date
    remark: str
    had_pen: int | None
    pen_note: str
    in_milk: bool
    since: dt.date | None = None


@dataclass
class Rebuilt:
    cow_id: str
    segments: list[MilkSegment]
    pen_note: str
    milk_note: str

    @property
    def last(self) -> MilkSegment | None:
        return self.segments[-1] if self.segments else None

    def counted_on(self, day: dt.date) -> bool:
        last = self.last
        if last is None or not last.in_milk or last.fdat is None:
            return False
        return (day - last.fdat).days >= 0

    @property
    def place(self) -> str:
        return place_for_pen(self.last.pen if self.last else None)

    def describe(self, day: dt.date) -> str:
        last = self.last
        if last is None:
            return "no events"
        if not last.in_milk:
            return f"not in milk ({self.milk_note})"
        if not self.counted_on(day):
            return f"fresh, day {(day - last.fdat).days if last.fdat else '?'}"
        return f"{self.place}, {self.pen_note}"


@dataclass
class MoveCheck:
    agree: int = 0
    differ: int = 0
    filled: int = 0
    # Lines that show her leaving a Bank pen, by year.
    bank_lines: Counter[int] = field(default_factory=Counter)
    bank_lines_known: Counter[int] = field(default_factory=Counter)
    bank_filled: list[MoveIssue] = field(default_factory=list)
    bank_other_pen: list[MoveIssue] = field(default_factory=list)
    still_in_bank: list[MoveIssue] = field(default_factory=list)
    bank_not_in_milk: list[MoveIssue] = field(default_factory=list)


def _record_move(check: MoveCheck, fact: MoveFact) -> None:
    from_bank = fact.from_pen in BNK_MILK_PENS
    issue = MoveIssue(
        fact.cow_id,
        fact.date,
        fact.remark,
        fact.pen_before,
        fact.pen_note,
        fact.in_milk,
        since=fact.filled_since if from_bank else None,
    )
    if from_bank:
        check.bank_lines[fact.date.year] += 1
        if not fact.in_milk:
            check.bank_not_in_milk.append(issue)
    if fact.pen_before is None:
        check.filled += 1
        if from_bank:
            check.bank_filled.append(issue)
        return
    if fact.pen_before == fact.from_pen:
        check.agree += 1
        if from_bank:
            check.bank_lines_known[fact.date.year] += 1
        return
    check.differ += 1
    if from_bank and fact.pen_before not in BNK_MILK_PENS:
        check.bank_other_pen.append(issue)
    elif from_bank:
        check.bank_lines_known[fact.date.year] += 1
    elif fact.pen_before in BNK_MILK_PENS:
        check.still_in_bank.append(issue)


def rebuild_cow(
    cow_id: str, events: list[Event], entry: dt.date | None, check: MoveCheck
) -> Rebuilt:
    segments, facts = build_milk_segments(cow_id, events, entry)
    for fact in facts:
        _record_move(check, fact)
    last = segments[-1] if segments else None
    return Rebuilt(
        cow_id=cow_id,
        segments=segments,
        pen_note=last.pen_note if last else "pen not known, no MOVE line",
        milk_note=last.milk_note if last else "no fresh date",
    )


def daily_counts(
    rebuilt: dict[str, Rebuilt], days: list[dt.date]
) -> dict[dt.date, dict[str, int]]:
    """Cows counted in each place at the end of each day."""
    deltas: dict[str, dict[dt.date, int]] = {place: defaultdict(int) for place in PLACES}
    for cow in rebuilt.values():
        for index, segment in enumerate(cow.segments):
            if not segment.in_milk or segment.fdat is None:
                continue
            end = cow.segments[index + 1].start if index + 1 < len(cow.segments) else None
            start = max(segment.start, segment.fdat)
            if end is not None and start >= end:
                continue
            place = place_for_pen(segment.pen)
            deltas[place][start] += 1
            if end is not None:
                deltas[place][end] -= 1

    out: dict[dt.date, dict[str, int]] = {}
    for place in PLACES:
        changes = sorted(deltas[place].items())
        running = 0
        position = 0
        for day in sorted(days):
            while position < len(changes) and changes[position][0] <= day:
                running += changes[position][1]
                position += 1
            out.setdefault(day, {})[place] = running
    return out


def find_file(folder: Path, word: str) -> Path:
    matches = sorted(
        path
        for path in folder.iterdir()
        if path.is_file() and path.suffix.lower() == ".csv" and word in path.name.upper()
    )
    if not matches:
        raise SystemExit(f"No {word} csv file in {folder}")
    return matches[0]


def default_folder() -> Path | None:
    raw = os.environ.get("LOCAL_HERD_EXPORT_DIR", "").strip()
    if not raw:
        return None
    root = Path(raw)
    for candidate in (root / "DCEXPORTALH", root):
        if candidate.is_dir() and any(candidate.glob("*INV*.csv")):
            return candidate
    return None


def _print_issues(title: str, issues: list[MoveIssue], limit: int) -> None:
    print(f"\n{title}: {len(issues)}")
    by_year = Counter(issue.date.year for issue in issues)
    if by_year:
        print("  By year: " + ", ".join(f"{year}: {by_year[year]}" for year in sorted(by_year)))
    for issue in sorted(issues, key=lambda item: item.date, reverse=True)[:limit]:
        since = f"  placed there from {show_date(issue.since)}" if issue.since else ""
        print(
            f"  {issue.cow_id:>6}  {show_date(issue.date)}  {issue.remark}  "
            f"rebuild had: {issue.pen_note}{since}"
        )
    if len(issues) > limit:
        print(f"  ... and {len(issues) - limit} more")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare ALH and Bank cows in milk rebuilt from events with today's pens."
    )
    parser.add_argument("folder", nargs="?", help="Folder holding the ALH INV and EVENTS csv files")
    parser.add_argument("--days", type=int, default=14, help="Days of rebuilt history to print")
    parser.add_argument("--list", type=int, default=40, help="Most cows to list in each section")
    args = parser.parse_args(argv)

    folder = Path(args.folder) if args.folder else default_folder()
    if folder is None or not folder.is_dir():
        raise SystemExit("Give the folder that holds the ALH INV and EVENTS csv files.")

    inventory, file_date = load_inventory(find_file(folder, "INV"))
    events = load_events(find_file(folder, "EVENT"))
    if file_date is None or events.last_date is None:
        raise SystemExit("Could not read a date from the inventory or events file.")

    check = MoveCheck()
    rebuilt = {
        key: rebuild_cow(events.cow_ids.get(key, ""), rows, events.entry_dates.get(key), check)
        for key, rows in events.by_cow.items()
    }

    print(f"Inventory file date: {show_date(file_date)}")
    print(f"Events: {show_date(events.first_date)} to {show_date(events.last_date)}")
    print(f"Events ignored because they are dated before her entry date: "
          f"{sum(events.before_entry.values())}")

    # ---- Today: inventory pens against the rebuild -------------------------
    inv_sets: dict[str, set[str]] = {place: set() for place in PLACES}
    for key, cow in inventory.items():
        if cow.counted:
            inv_sets[cow.place].add(key)
    new_sets: dict[str, set[str]] = {place: set() for place in PLACES}
    for key, cow in rebuilt.items():
        if cow.counted_on(file_date):
            new_sets[cow.place].add(key)

    print("\nToday, cows in milk")
    print(f"  {'':<10}{'Inventory':>10}{'Rebuilt':>10}{'Difference':>12}")
    for place in PLACES:
        have, got = len(inv_sets[place]), len(new_sets[place])
        print(f"  {place:<10}{have:>10}{got:>10}{got - have:>+12}")
    total_have = sum(len(group) for group in inv_sets.values())
    total_got = sum(len(group) for group in new_sets.values())
    print(f"  {'Total':<10}{total_have:>10}{total_got:>10}{total_got - total_have:>+12}")

    inv_place = {key: place for place, keys in inv_sets.items() for key in keys}
    new_place = {key: place for place, keys in new_sets.items() for key in keys}
    differing = sorted(
        key
        for key in set(inv_place) | set(new_place)
        if inv_place.get(key) != new_place.get(key)
    )
    kinds: Counter[str] = Counter()
    lines: list[tuple[str, str]] = []
    for key in differing:
        have = inv_place.get(key, "not counted")
        got = new_place.get(key, "not counted")
        kind = f"inventory {have}, rebuilt {got}"
        kinds[kind] += 1
        inv_cow = inventory.get(key)
        new_cow = rebuilt.get(key)
        cow_id = inv_cow.cow_id if inv_cow else (new_cow.cow_id if new_cow else key)
        inv_text = inv_cow.describe() if inv_cow else "not on the inventory"
        new_text = new_cow.describe(file_date) if new_cow else "no events"
        lines.append((kind, f"  {cow_id:>6}  inventory: {inv_text}  |  rebuilt: {new_text}"))

    print(f"\nCows placed differently today: {len(differing)}")
    for kind, count in kinds.most_common():
        print(f"\n  {kind}: {count}")
        shown = [line for line_kind, line in lines if line_kind == kind]
        for line in shown[: args.list]:
            print("  " + line)
        if len(shown) > args.list:
            print(f"    ... and {len(shown) - args.list} more")

    # ---- History: every MOVE line against the pen the rebuild had ----------
    checked = check.agree + check.differ
    print("\nEvery MOVE line, against the pen the rebuild had her in")
    print(f"  Left the pen the rebuild had her in:   {check.agree}")
    print(f"  Left a different pen:                  {check.differ}")
    print(f"  Pen not known, taken from this line:   {check.filled}")
    if checked:
        print(f"  Agreement where the pen was known:     {100.0 * check.agree / checked:.1f}%")
    if events.odd_moves:
        print(f"  MOVE remarks that could not be read:   {len(events.odd_moves)}")

    print("\nMOVE lines that show her leaving a Bank pen")
    print(f"  {'Year':<6}{'Lines':>8}{'Rebuild had her there':>24}{'Share':>8}")
    for year in sorted(check.bank_lines):
        lines_count = check.bank_lines[year]
        known = check.bank_lines_known[year]
        share = 100.0 * known / lines_count if lines_count else 0.0
        print(f"  {year:<6}{lines_count:>8}{known:>24}{share:>7.1f}%")

    _print_issues(
        "In a Bank pen with no line putting her there, found from a later line",
        check.bank_filled,
        args.list,
    )
    _print_issues(
        "Left a Bank pen while the rebuild had her in another pen",
        check.bank_other_pen,
        args.list,
    )
    _print_issues(
        "Moved from another pen while the rebuild still had her in a Bank pen",
        check.still_in_bank,
        args.list,
    )
    _print_issues(
        "Left a Bank pen while the rebuild had her not in milk",
        check.bank_not_in_milk,
        args.list,
    )

    # ---- Rebuilt daily numbers ---------------------------------------------
    days = [file_date - dt.timedelta(days=offset) for offset in range(args.days - 1, -1, -1)]
    counts = daily_counts(rebuilt, days)
    print(f"\nRebuilt cows in milk, last {args.days} days")
    print(f"  {'Date':<14}{BANK:>8}{ALH:>8}{MOVED:>10}{'Total':>8}")
    for day in days:
        row = counts[day]
        print(
            f"  {show_date(day):<14}{row[BANK]:>8}{row[ALH]:>8}{row[MOVED]:>10}"
            f"{sum(row.values()):>8}"
        )

    matched = not differing
    print("\nResult: " + ("rebuilt numbers match today's pens." if matched else
                          "rebuilt numbers do not match today's pens."))
    return 0 if matched else 1


if __name__ == "__main__":
    raise SystemExit(main())
