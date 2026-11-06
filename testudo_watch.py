"""
Testudo watcher: alerts you (via ntfy push notification) when
  - a new section is added to a watched course
  - a section's waitlist gets smaller
  - a section goes from 0 open seats to some open seats
  - a brand-new course appears under the search prefix

Config is via environment variables (set in the GitHub workflow):
  TERM_ID      e.g. 202701  (Spring 2027)
  SEARCH       course prefix to search, e.g. BMGT44  (covers BMGT440-449)
  COURSES      optional comma list to narrow alerts, e.g. BMGT444,BMGT446
               (leave blank to watch everything the search returns)
  NTFY_TOPIC   your private ntfy.sh topic name
"""
import json
import os
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

TERM = os.environ.get("TERM_ID", "202701")
SEARCH = os.environ.get("SEARCH", "BMGT44")
COURSES = {c.strip().upper() for c in os.environ.get("COURSES", "").split(",") if c.strip()}
TOPIC = os.environ.get("NTFY_TOPIC", "")
STATE_FILE = Path("state.json")
BASE = "https://app.testudo.umd.edu/soc"
HEADERS = {"User-Agent": "Mozilla/5.0 (personal seat watcher)"}
COURSE_RE = re.compile(r"^[A-Z]{4}\d{3}[A-Z]?$")


def get(url, params=None):
    r = requests.get(url, params=params, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def find_courses():
    html = get(f"{BASE}/search", {
        "courseId": SEARCH, "sectionId": "", "termId": TERM,
        "_openSectionsOnly": "on", "creditCompare": ">=", "credits": "0.0",
        "courseLevelFilter": "ALL", "instructor": "", "teachingCenter": "ALL",
        "_facetoface": "on", "_blended": "on", "_online": "on",
    })
    soup = BeautifulSoup(html, "html.parser")
    ids = set()
    for div in soup.select("div.course[id]"):
        if COURSE_RE.match(div["id"]):
            ids.add(div["id"])
    for el in soup.select(".course-id"):
        t = el.get_text(strip=True)
        if COURSE_RE.match(t):
            ids.add(t)
    return sorted(ids)


def num(el):
    if el is None:
        return None
    m = re.search(r"\d+", el.get_text())
    return int(m.group()) if m else None


def parse_sections(html):
    soup = BeautifulSoup(html, "html.parser")
    data = {}
    for sid_el in soup.select("span.section-id"):
        section = sid_el.find_parent(class_="section") or sid_el.parent
        course = None
        for anc in section.parents:
            if anc.has_attr("id") and COURSE_RE.match(anc["id"]):
                course = anc["id"]
                break
        if not course:
            continue
        wl = section.select("span.waitlist-count")  # [waitlist, holdfile]
        inst = section.select_one(".section-instructor")
        data.setdefault(course, {})[sid_el.get_text(strip=True)] = {
            "total": num(section.select_one("span.total-seats-count")),
            "open": num(section.select_one("span.open-seats-count")),
            "waitlist": num(wl[0]) if wl else None,
            "holdfile": num(wl[1]) if len(wl) > 1 else None,
            "instructor": inst.get_text(" ", strip=True) if inst else "",
        }
    return data


def snapshot():
    courses = find_courses()
    if not courses:
        raise RuntimeError("Search returned no courses (page layout may have changed).")
    html = get(f"{BASE}/{TERM}/sections", {"courseIds": ",".join(courses)})
    data = parse_sections(html)
    for c in courses:
        data.setdefault(c, {})
    return data


def notify(title, msg):
    print(f"[{title}] {msg}")
    if TOPIC:
        requests.post(f"https://ntfy.sh/{TOPIC}", data=msg.encode(),
                      headers={"Title": title, "Tags": "mortar_board",
                               "Click": f"{BASE}/search?courseId={SEARCH}&termId={TERM}"},
                      timeout=30)


def watched(course):
    return not COURSES or course in COURSES


def main():
    new = snapshot()
    if not STATE_FILE.exists():
        STATE_FILE.write_text(json.dumps(new, indent=2, sort_keys=True))
        n = sum(len(s) for c, s in new.items() if watched(c))
        notify("Testudo watcher running", f"Baseline saved: {n} sections across "
               f"{len([c for c in new if watched(c)])} courses.")
        return

    old = json.loads(STATE_FILE.read_text())
    alerts = []
    for course, sections in new.items():
        if not watched(course):
            continue
        if course not in old:
            alerts.append(f"New course listed: {course} ({len(sections)} sections)")
            continue
        for sid, s in sections.items():
            o = old[course].get(sid)
            if o is None:
                alerts.append(f"{course}-{sid} NEW section added "
                              f"({s['open']}/{s['total']} open, {s['instructor']})")
                continue
            if s["waitlist"] is not None and o.get("waitlist") is not None \
                    and s["waitlist"] < o["waitlist"]:
                alerts.append(f"{course}-{sid} waitlist {o['waitlist']} → {s['waitlist']}")
            if (o.get("open") or 0) == 0 and (s["open"] or 0) > 0:
                alerts.append(f"{course}-{sid} seat opened: {s['open']} open")

    if alerts:
        notify(f"Testudo: {len(alerts)} change(s)", "\n".join(alerts))
    STATE_FILE.write_text(json.dumps(new, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        notify("Testudo watcher error", str(e))
        sys.exit(1)
