#!/usr/bin/env python3
"""
Tests for Eventbrite attendee pagination.

The bug this guards against: Eventbrite returns attendees one page at a time and
only sends the rest if you pass the `continuation` token back. Reading a single
response silently under-reports the roster, which would quietly under-count
enrollment and hide students from the register.

Run:  python3 test_pagination.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import integrations

PASS = FAIL = 0

def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}\n         got:  {got!r}\n         want: {want!r}")

def attendee(n, refunded=False, cancelled=False):
    return {"id": str(n), "refunded": refunded, "cancelled": cancelled,
            "profile": {"name": f"Student {n}", "email": f"s{n}@example.com",
                        "cell_phone": f"(302) 555-{n:04d}"}}

def paged_api(pages):
    """A fake _req that serves `pages` in order and records the URLs it was asked
    for, so we can assert the continuation token is actually sent back."""
    calls = []
    def fake_req(url, method="POST", token=None, json_body=None, form=None, headers=None):
        calls.append(url)
        return pages[len(calls) - 1]
    return fake_req, calls


print("\nEventbrite attendee pagination")
print("-" * 62)

# ---------------------------------------------------------------- 3 pages ----
# 50 + 50 + 7 = 107 attendees. A non-paginating client would report 50.
pages = [
    {"pagination": {"object_count": 107, "page_number": 1, "page_size": 50,
                    "page_count": 3, "has_more_items": True, "continuation": "TOKEN_PAGE_2"},
     "attendees": [attendee(i) for i in range(1, 51)]},
    {"pagination": {"object_count": 107, "page_number": 2, "page_size": 50,
                    "page_count": 3, "has_more_items": True, "continuation": "TOKEN_PAGE_3"},
     "attendees": [attendee(i) for i in range(51, 101)]},
    {"pagination": {"object_count": 107, "page_number": 3, "page_size": 50,
                    "page_count": 3, "has_more_items": False, "continuation": None},
     "attendees": [attendee(i) for i in range(101, 108)]},
]
fake, calls = paged_api(pages)
got = integrations.fetch_attendees("EV123", {"eventbrite_token": "t"}, _req_fn=fake)

check("collects every attendee across 3 pages", len(got), 107)
check("makes exactly 3 requests", len(calls), 3)
check("first request has no continuation", "continuation" in calls[0], False)
check("2nd request sends page 1's token", "continuation=TOKEN_PAGE_2" in calls[1], True)
check("3rd request sends page 2's token", "continuation=TOKEN_PAGE_3" in calls[2], True)
check("stops once has_more_items is false", len(calls), 3)
check("keeps them in order, first", got[0]["id"], "1")
check("keeps them in order, last", got[-1]["id"], "107")
check("no duplicates", len({a["id"] for a in got}), 107)

# --------------------------------------------------------------- 1 page ----
single = [{"pagination": {"object_count": 2, "has_more_items": False}, "attendees": [attendee(1), attendee(2)]}]
fake, calls = paged_api(single)
got = integrations.fetch_attendees("EV1", {"eventbrite_token": "t"}, _req_fn=fake)
check("single page: one request only", len(calls), 1)
check("single page: both attendees", len(got), 2)

# ------------------------------------------------------------ empty event ----
empty = [{"pagination": {"object_count": 0, "has_more_items": False}, "attendees": []}]
fake, calls = paged_api(empty)
check("event with no attendees", integrations.fetch_attendees("EV0", {"eventbrite_token": "t"}, _req_fn=fake), [])

# --------------------------------------------- has_more_items but no token ----
# A malformed response must not spin forever re-requesting page one.
broken = [{"pagination": {"has_more_items": True, "continuation": None},
           "attendees": [attendee(1)]}]
fake, calls = paged_api(broken)
got = integrations.fetch_attendees("EVX", {"eventbrite_token": "t"}, _req_fn=fake)
check("missing continuation does not loop", len(calls), 1)
check("missing continuation keeps what it got", len(got), 1)

# ------------------------------------------------------------ page cap ----
# Every page claims more, forever. The safety limit must stop it.
class Endless:
    def __init__(self): self.n = 0
    def __call__(self, url, method="POST", token=None, json_body=None, form=None, headers=None):
        self.n += 1
        return {"pagination": {"has_more_items": True, "continuation": f"t{self.n}"},
                "attendees": [attendee(self.n)]}
endless = Endless()
got = integrations.fetch_attendees("EVLOOP", {"eventbrite_token": "t"}, _req_fn=endless)
check("runaway pagination hits the cap", endless.n, integrations.MAX_ATTENDEE_PAGES)

# ------------------------------------------------------------ normalizing ----
n = integrations.normalize_attendee(attendee(7))
check("maps name",  n["name"],  "Student 7")
check("maps email", n["email"], "s7@example.com")
check("maps phone", n["phone"], "(302) 555-0007")
check("refunded attendee flagged",  integrations.normalize_attendee(attendee(8, refunded=True))["refunded"], True)
check("cancelled attendee flagged", integrations.normalize_attendee(attendee(9, cancelled=True))["refunded"], True)
check("first/last name fallback",
      integrations.normalize_attendee({"id": "3", "profile": {"first_name": "Ada", "last_name": "Lovelace"}})["name"],
      "Ada Lovelace")

# ------------------------------------------------- end to end through sync ----
fake, calls = paged_api(pages)
people = integrations.sync_attendees(
    {"external_ids": {"eventbrite_id": "EV123"}},
    cfg={"eventbrite_token": "t", "live": True}, _req_fn=fake)
check("sync_attendees returns all 107 normalized", len(people), 107)
check("sync_attendees paged through", len(calls), 3)
check("no eventbrite id -> nothing to sync",
      integrations.sync_attendees({"external_ids": {}}, cfg={"eventbrite_token": "t", "live": True}), None)

print("-" * 62)
print(f"{PASS} passed, {FAIL} failed\n")
sys.exit(1 if FAIL else 0)
