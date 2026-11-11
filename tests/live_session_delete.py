"""Live check of session deletion on a running LocalHarness (port 7788).

Creates a throwaway session, deletes it through the API and checks the list,
the history endpoint, repeat delete and a path-traversal attempt. The app
config is not touched.

Usage: start the app, then `python tests/live_session_delete.py`.
"""
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:7788"
FAILURES = []

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def req(path, payload=None):
    if payload is None:
        resp = urllib.request.urlopen(BASE + path, timeout=15)
    else:
        r = urllib.request.Request(BASE + path, data=json.dumps(payload).encode("utf-8"),
                                   headers={"Content-Type": "application/json"}, method="POST")
        resp = urllib.request.urlopen(r, timeout=15)
    return json.loads(resp.read().decode("utf-8"))


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + (" " + str(detail) if detail else ""))
    if not cond:
        FAILURES.append(name)


def expect_http_error(name, path, payload, codes):
    try:
        req(path, payload)
    except urllib.error.HTTPError as exc:
        check(name, exc.code in codes, exc.code)
        return
    check(name, False, "ожидалась ошибка HTTP, а запрос прошёл")


def main():
    sid = req("/api/new", {"title": "тест удаления сессии"}).get("session")
    check("create session", bool(sid), sid)
    check("session listed", any(s["id"] == sid for s in req("/api/state").get("sessions", [])))

    res = req("/api/session/delete", {"session": sid})
    check("delete ok", res.get("ok") is True)
    check("gone from list", not any(s["id"] == sid for s in req("/api/state").get("sessions", [])))
    expect_http_error("history after delete = 404", "/api/history?session=" + sid, None, (404,))
    expect_http_error("repeat delete = 404", "/api/session/delete", {"session": sid}, (404,))
    expect_http_error("traversal blocked", "/api/session/delete", {"session": "../../config"}, (400, 404))
    expect_http_error("empty sid blocked", "/api/session/delete", {"session": ""}, (400, 404))
    check("app alive after traversal attempt", bool(req("/api/state").get("provider")))

    print("FAILURES:", len(FAILURES))
    print("VERDICT:", "DELETE LIVE OK" if not FAILURES else "DELETE LIVE FAIL")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())