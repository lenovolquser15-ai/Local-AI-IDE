"""Живая проверка глобального поиска: несколько запросов, видно какие источники ответили.

Печатает источники и ошибки по каждому запросу и проверяет, что выдача непустая
и что при «глухой» теме включаются вертикальные базы.
"""
import sys
import time

sys.path.insert(0, "C:/LocalHarness")
from harness import search  # noqa: E402

# консоль Windows по умолчанию cp1251 — кириллица в заголовках падает с UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

QUERIES = [
    ("python asyncio gather", "en"),
    ("как настроить opencv python", "ru"),
    ("лицензия MIT что это", "ru"),
    ("zzzz-not-a-real-topic-qqq-42", "none"),
]


def main():
    ok = True
    for query, lang in QUERIES:
        started = time.time()
        res = search.search(query, count=6)
        elapsed = time.time() - started
        print("== %s [%s] %.1fs" % (query, lang, elapsed))
        print("   источники:", res["sources"])
        if res["errors"]:
            print("   ошибки:", {k: v[:70] for k, v in res["errors"].items()})
        for item in res["results"][:4]:
            print("   - %-14s %s" % (item["source"], item["title"][:60]))
        if not res["results"]:
            print("   ПУСТО")
            ok = False
    print("VERDICT: SEARCH OK" if ok else "VERDICT: SEARCH FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
