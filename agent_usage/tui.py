"""Read-only report pane: costs per period, breakdowns, quota peaks and an activity heatmap.

Rendering produces styled lines, so the same views print as plain text outside curses."""
import curses
import sqlite3
import time
from datetime import datetime, timedelta
from decimal import Decimal

from .summary import PERIODS, History, cache_hit, missing_prices, tokens_total

SPARK = "▁▂▃▄▅▆▇█"
SHADE = " ░▒▓█"
EIGHTHS = " ▏▎▍▌▋▊▉"
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
KEYS = "d/w/m period  ↑↓ select  tab heatmap  r refresh  q quit"


def money(value):
    v = float(value)
    if v >= 10000:
        return f"${v / 1000:.1f}k"
    if v >= 100:
        return f"${v:,.0f}"
    return f"${v:.2f}"


def count(n):
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if n >= size:
            return f"{n / size:.1f}{suffix}"
    return str(int(n))


def hours(seconds):
    return f"{seconds / 3600:.1f}h" if seconds else "–"


def percent(fraction):
    return f"{fraction * 100:.0f}%" if fraction is not None else "–"


def spark(values):
    top = max(values, default=0)
    return "".join(SPARK[min(7, int(v / top * 7.999))] if top and v else " " for v in values)


def bar(fraction, width):
    cells = max(0.0, min(1.0, fraction)) * width
    full = int(cells)
    rest = EIGHTHS[int((cells - full) * 8)] if full < width else ""
    return ("█" * full + rest.strip()).ljust(width)


def delta(current, previous):
    if not previous:
        return None
    change = (current - previous) / previous * 100
    if change > 999:
        return f"▲{current / previous:.0f}×", "up"
    return f"{'▲' if change >= 0 else '▼'}{abs(change):.0f}%", ("up" if change >= 0 else "down")


def short_project(project):
    if not project or project == "unknown":
        return "unknown"
    parts = project.rstrip("/").split("/")
    return "/".join(parts[-2:]) if len(parts) > 2 else project


def pad(text, width, right=False):
    text = text if len(text) <= width else text[:max(0, width - 1)] + "…"
    return text.rjust(width) if right else text.ljust(width)


def amount(entry):
    """An agent, model or project figure: cost when priced, tokens otherwise (Lumo)."""
    return money(entry["cost"]) if entry["priced"] else count(entry["tokens"]) + " tok"


class View:
    def __init__(self, load):
        self.load = load
        self.period, self.selected, self.heatmap = "day", 0, False
        self.history, self.error, self.loaded = None, None, 0
        self.refresh()

    def refresh(self):
        try:
            self.history = self.load()
            self.error = None
        except (sqlite3.Error, OSError) as error:
            self.history, self.error = None, f"No usage history yet ({type(error).__name__})."
        self.loaded = time.monotonic()
        self.cache = {}

    def buckets(self):
        if self.period not in self.cache:
            self.cache[self.period] = self.history.buckets(self.period)
        return self.cache[self.period]

    def key(self, ch):
        if ch in (ord("q"), 27):
            return False
        if ch in (ord("d"), ord("w"), ord("m")):
            self.period = {"d": "day", "w": "week", "m": "month"}[chr(ch)]
            self.selected = 0
        elif ch in (9, ord("h")):
            self.heatmap = not self.heatmap
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.selected += 1
        elif ch in (curses.KEY_UP, ord("k")):
            self.selected = max(0, self.selected - 1)
        elif ch in (curses.KEY_HOME, ord("g")):
            self.selected = 0
        elif ch in (curses.KEY_END, ord("G")):
            self.selected = 10 ** 6
        elif ch == ord("r"):
            self.refresh()
        return True

    def render(self, width, height):
        title = f" Agent usage · {self.period}"
        keys = KEYS if len(title) + len(KEYS) + 2 <= width else "d/w/m  tab  q"
        lines = [[(pad(title, width - len(keys) - 1) + keys + " ", "title")]]
        if self.error or not self.history or not self.history.usage:
            lines += [[], [(" " + (self.error or "No usage history yet."), "warn")],
                      [(" The collector folds usage into hourly history on each pass; "
                        "run the 'backfill' action to import older session files.", "dim")]]
            return lines
        lines += self.headline(width)
        lines.append([])
        lines += self.heat(width) if self.heatmap else self.table(width, height - len(lines))
        return lines

    def headline(self, width):
        h = self.history.headline()
        parts = []
        names = {"day": "Today", "week": h["week"]["label"].split()[0], "month": h["month"]["label"].split()[0]}
        for period in PERIODS:
            item = h[period]
            parts.append((f"{names[period]} ", "dim"))
            parts.append((money(item["cost"]), "money"))
            d = delta(item["cost"], item["previous_same_point"])
            if d:
                parts.append((f" {d[0]}", d[1]))
            if period == "month":
                parts.append((f" → ~{money(item['projected'])}", "dim"))
            parts.append(("  ·  ", "dim"))
        parts.append((f"{h['streak_days']}-day streak", "bold"))
        first = [(" ", None)] + parts
        daily = [float(v) for _, v in h["daily"]]
        avg = sum(daily) / len(daily) if daily else 0
        best_day, best = max(h["daily"], key=lambda dv: dv[1])
        second = [(" 30 days ", "dim"), (spark(daily), "bar"),
                  (f"  avg {money(avg)}/day · peak {money(best)} on {best_day:%a %b %d}", "dim")]
        lines = [first, second]
        for agent, sub in h["subscriptions"].items():
            ratio = f" (×{sub['ratio']:.1f} the {money(sub['monthly'])} plan)" if sub["ratio"] else ""
            lines.append([(f" {agent.capitalize()} this month: ", "dim"), (money(sub["value"]), "money"),
                          (" API-equivalent" + ratio, "dim")])
        lines.append([(" comparisons are against the previous period at the same point; "
                       f"tracked since {h['first_day']:%b %d}", "dim")])
        return lines

    def table(self, width, height):
        buckets = self.buckets()
        self.selected = min(self.selected, len(buckets) - 1)
        agents = {}
        for b in buckets:
            for name, entry in b["agents"].items():
                agents[name] = agents.get(name, Decimal(0)) + entry["cost"] + Decimal(entry["tokens"]) / 10 ** 12
        agents = sorted(agents, key=lambda a: -agents[a])
        top = max((b["cost"] for b in buckets), default=Decimal(0)) or Decimal(1)
        label_width = {"day": 11, "week": 18, "month": 15}[self.period]
        columns = [("Period", label_width, False), ("Cost", 9, True)]
        columns += [(a.capitalize(), 10, True) for a in agents]
        columns += [("Tokens", 8, True), ("Cache", 6, True), ("Work", 7, True), ("Need you", 9, True),
                    ("Sess", 5, True)]
        used = sum(w + 1 for _, w, _ in columns)
        bar_width = max(0, min(30, width - used - 2))
        header = " " + " ".join(pad(n, w, r) for n, w, r in columns)
        lines = [[(header, "head")]]
        rows_room = max(3, min(len(buckets), (height - 12) if height > 24 else height // 2))
        first = min(max(0, self.selected - rows_room // 2), max(0, len(buckets) - rows_room))
        for i, b in enumerate(buckets[first:first + rows_room], first):
            cells = [pad(b["label"], label_width), pad(money(b["cost"]) + ("*" if missing_prices(b) else ""), 9, True)]
            for a in agents:
                entry = b["agents"].get(a)
                cells.append(pad(amount(entry) if entry else "–", 10, True))
            status = b["status"]
            cells += [pad(count(tokens_total(b["tokens"])), 8, True), pad(percent(cache_hit(b["tokens"])), 6, True),
                      pad(hours(status.get("working", 0)), 7, True), pad(hours(status.get("blocked", 0)), 9, True),
                      pad(str(len(b["sessions"])), 5, True)]
            style = "sel" if i == self.selected else None
            row = [(" " + " ".join(cells) + " ", style)]
            if bar_width:
                row.append((bar(float(b["cost"] / top), bar_width), "sel" if style else "bar"))
            lines.append(row)
        if len(buckets) > rows_room:
            lines.append([(f" {first + 1}–{min(len(buckets), first + rows_room)} of {len(buckets)}", "dim")])
        lines.append([])
        lines += self.detail(buckets[self.selected], width)
        return lines

    def detail(self, b, width):
        status = b["status"]
        working = status.get("working", 0)
        n = len(b["sessions"])
        facts = [f"{n} session{'s' if n != 1 else ''}", f"{len(b['days'])} active days" if self.period != "day" else None,
                 f"{count(b['records'])} requests",
                 f"{money(b['cost'] / Decimal(working / 3600))} per agent-hour" if working >= 600 else None,
                 f"agents waited {hours(status.get('blocked', 0))} for you" if status.get("blocked") else None]
        text = " · ".join(f for f in facts if f)
        lines = [[(f" ── {b['label']}  ", "bold"), (text + " ", "dim"),
                  ("─" * max(0, width - len(b["label"]) - len(text) - 7), "dim")]]
        peaks = [f"{agent} {name.replace('_', ' ')} {value:.0f}%" for (agent, name), value in sorted(b["quota"].items())]
        if peaks:
            lines.append([(" quota peaks: ", "dim"), (" · ".join(peaks), "warn" if max(b["quota"].values()) >= 90 else None)])
        missing = missing_prices(b)
        if missing:
            lines.append([(f" * excludes {', '.join(missing)}: not in the price catalog "
                           "(add rates under pricing.models in config.json)", "warn")])
        columns = 3 if width >= 120 else 2 if width >= 80 else 1
        col = (width - 2) // columns - 1
        blocks = [self.ranking("Models", b["models"], col), self.ranking("Projects", b["projects"], col),
                  self.sessions(b, col if columns == 3 else min(width - 2, col * columns))]
        if columns == 2:
            blocks = [blocks[0], blocks[1]], [blocks[2]]
        elif columns == 3:
            blocks = [blocks]
        else:
            blocks = [[x] for x in blocks]
        for group in blocks:
            for i in range(max(len(x) for x in group)):
                row = [(" ", None)]
                for block in group:
                    row += (block[i] if i < len(block) else [(" " * col, None)]) + [(" ", None)]
                lines.append(row)
            lines.append([])
        return lines

    def ranking(self, title, entries, width):
        ordered = sorted(entries.items(), key=lambda kv: (-kv[1]["cost"], -kv[1]["tokens"]))[:6]
        top = max((e["cost"] for _, e in ordered), default=Decimal(0)) or Decimal(1)
        name_width = max(8, width - 22)
        rows = [[(pad(title, width), "head")]]
        for name, entry in ordered:
            label = short_project(name) if title == "Projects" else name.removeprefix("claude-")
            fraction = float(entry["cost"] / top) if entry["priced"] else 0
            rows.append([(pad(label, name_width), None), (pad(amount(entry), 10, True) + " ", "money"),
                         (bar(fraction, max(0, width - name_width - 12)), "bar")])
        return rows

    def sessions(self, b, width):
        zone = self.history.zone
        rows = [[(pad("Top sessions", width), "head")]]
        ordered = sorted(b["sessions"].items(), key=lambda kv: (-kv[1]["cost"], -kv[1]["tokens"]))[:6]
        for key, s in ordered:
            start = datetime.fromtimestamp(s["first"], zone)
            end = datetime.fromtimestamp(s["last"], zone) + timedelta(hours=1)
            if start.date() == (end - timedelta(seconds=1)).date():
                when = f"{start:%b %d %H}–{end:%H}h"
            elif start.month == end.month:
                when = f"{start:%b %d}–{end:%d}"
            else:
                when = f"{start:%b %d}–{end:%b %d}"
            name = short_project(s["project"]) + ("" if s["agent"] == "claude" else f" ({s['agent']})")
            rows.append([(pad(name, max(8, width - 24)), None), (pad(amount(s), 10, True) + " ", "money"),
                         (pad(when, 13), "dim")])
        return rows

    def heat(self, width):
        """Heatmaps of the selected period: the weekday x hour grid for a week or month,
        and for a day its hours against the average active day of the four weeks before."""
        buckets = self.buckets()
        self.selected = min(self.selected, len(buckets) - 1)
        b = buckets[self.selected]
        grid = self.history.heatmap(b["begin"], b["end"])
        lines = [[(f" ◀ {b['label']} ▶  ", "bold"), (money(b["cost"]), "money"),
                  (f"  ·  {self.selected + 1} of {len(buckets)}  ·  j older  k newer", "dim")]]
        since = self.history.status_since()
        if since is None or since > b["begin"]:
            note = f"since {datetime.fromtimestamp(since, self.history.zone):%b %d}" if since else "not yet"
            lines.append([(f" working time is recorded by the live collector only ({note}); "
                           "spend covers imported history too", "dim")])
        lines.append([])
        if self.period == "day":
            before = self.history.heatmap(b["begin"] - 28 * 86400, b["begin"])
            for title, key, fmt in (("Agent working time", "working_seconds", hours),
                                    ("Estimated spend", "cost", money)):
                day = [sum(row[h] for row in grid[key]) for h in range(24)]
                typical = max(1, before["active_days"][key])
                avg = [sum(row[h] for row in before[key]) / typical for h in range(24)]
                lines += self.grid(f"{title} on {b['label']} by local hour", [("day", day), ("avg", avg)], fmt)
            n = before["active_days"]
            lines.append([(f" avg: the mean active day of the four weeks before ({n['working_seconds']} days "
                           f"with working time, {n['cost']} with spend)", "dim")])
            work = [[sum(row[h] for row in grid["working_seconds"]) for h in range(24)]]
        else:
            for title, key, fmt in (("Agent working time", "working_seconds", hours),
                                    ("Estimated spend", "cost", money)):
                rows = list(zip(WEEKDAYS, grid[key]))
                lines += self.grid(f"{title} in {b['label']} by weekday and local hour", rows, fmt)
            work = grid["working_seconds"]
        total = sum(map(sum, work))
        if total:
            slots = [(d, h) for d in range(len(work)) for h in range(24)]
            day, hour = max(slots, key=lambda dh: work[dh[0]][dh[1]])
            night = sum(work[d][h] for d in range(len(work)) for h in (*range(22, 24), *range(0, 6))) / total
            busiest = f"{hour:02d}:00" if self.period == "day" else f"{WEEKDAYS[day]} {hour:02d}:00"
            facts = [(" busiest hour " if self.period == "day" else " busiest slot ", "dim"), (busiest, "bold"),
                     ("  ·  night (22–06) ", "dim"), (percent(night), "bold")]
            if self.period != "day":
                facts += [("  ·  weekend ", "dim"), (percent(sum(map(sum, work[5:])) / total), "bold")]
            facts += [("  ·  agent-hours ", "dim"), (hours(total), "bold")]
            lines.append(facts)
        return lines

    def grid(self, title, rows, fmt):
        """Rows of 24 hourly cells, shaded against the darkest cell of the whole grid."""
        top = max((max(values) for _, values in rows), default=0)
        lines = [[(f" {title} (darkest = {fmt(top) if top else '–'})", "head")],
                 [("      " + "".join(f"{h:<6d}" for h in range(0, 24, 3)), "dim")]]
        for name, values in rows:
            line = [(f" {name:<3}  ", "dim")]
            for value in values:
                shade = SHADE[max(1, min(4, int(value / top * 4.999)))] if top and value else "·"
                line.append((shade * 2 if shade != "·" else "· ", "bar" if shade != "·" else "dim"))
            lines.append(line)
        lines.append([])
        return lines


def plain(lines):
    return "\n".join("".join(text for text, _ in line).rstrip() for line in lines)


def run(load):
    def main(screen):
        curses.curs_set(0)
        styles = {None: curses.A_NORMAL, "title": curses.A_REVERSE | curses.A_BOLD, "dim": curses.A_DIM,
                  "bold": curses.A_BOLD, "head": curses.A_BOLD | curses.A_UNDERLINE, "sel": curses.A_REVERSE}
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            for i, (name, color) in enumerate((("money", curses.COLOR_GREEN), ("bar", curses.COLOR_CYAN),
                                               ("warn", curses.COLOR_YELLOW), ("up", curses.COLOR_YELLOW),
                                               ("down", curses.COLOR_GREEN)), 1):
                curses.init_pair(i, color, -1)
                styles[name] = curses.color_pair(i)
        screen.timeout(1000)
        view = View(load)
        while True:
            if time.monotonic() - view.loaded > 60:
                view.refresh()
            height, width = screen.getmaxyx()
            screen.erase()
            for y, line in enumerate(view.render(width, height)[:height]):
                x = 0
                for text, style in line:
                    if x >= width - 1:
                        break
                    try:
                        screen.addnstr(y, x, text, width - 1 - x, styles.get(style, curses.A_NORMAL))
                    except curses.error:
                        pass
                    x += len(text)
            screen.refresh()
            ch = screen.getch()
            if ch != -1 and not view.key(ch):
                return
    curses.wrapper(main)


def connect(path):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    return db


def loader(path, zone, config):
    def load():
        db = connect(path)
        try:
            return History(db, zone, time.time(), config)
        finally:
            db.close()
    return load
