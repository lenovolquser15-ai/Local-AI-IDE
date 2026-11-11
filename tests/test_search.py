"""Регрессы глобального поиска: перебор движков, дедуп, вертикали, диагностика ошибок."""
import json
import os
import sys

sys.path.insert(0, "C:/LocalHarness")
os.environ["LH_DATA"] = "C:/LocalHarness/tests/.tmp"
from harness import search as search_mod  # noqa: E402
from harness.agent import Bridge  # noqa: E402

failures = []


def check(cond, label):
    if not cond:
        failures.append(label)


def fake_fetch(pages):
    """pages: url-подстрока -> текст ответа (или Exception, чтобы движок упал)."""
    calls = []

    def fetch(url, timeout=20.0, headers=None, data=None):
        calls.append(url)
        for needle, answer in pages.items():
            if needle in url:
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise RuntimeError("404: %s" % url)

    fetch.calls = calls
    return fetch


DDG_HTML = """
<a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa">Example A запрос</a>
<div class="result__snippet">Snippet A запрос</div>
<a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fb">Example B запрос</a>
<div class="result__snippet">Snippet B запрос</div>
"""

BING_HTML = """
<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?u=a1aHR0cHM6Ly9leGFtcGxlLm9yZy9i">Example B запрос</a></h2>
<div class="b_caption"><p>Snippet B from bing запрос</p></div></li>
"""

LITE_HTML = """
<a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.net%2Fc" class='result-link'>Example C запрос</a>
<td class='result-snippet'>Snippet C запрос</td>
"""

JUNK_BING_HTML = """
<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?u=a1aHR0cHM6Ly9zaHVwLmV4YW1wbGUv">Магазин одежды</a></h2>
<div class="b_caption"><p>Каталог товаров со скидками</p></div></li>
"""

RIGHTDAO_HTML = """
<div class="item"><div class="title"><a href="https://docs.python.org/3/asyncio.html">
<div class="ellipsis-lg">asyncio документация</div></a></div>
<div class="description"><span class="date"></span>asyncio — асинхронный ввод-вывод</div></div>
"""

DDG_IA_JSON = json.dumps({
    "Heading": "Python", "AbstractText": "Python — язык программирования",
    "AbstractURL": "https://ru.wikipedia.org/wiki/Python",
    "RelatedTopics": [{"Text": "Python (язык)", "FirstURL": "https://ru.wikipedia.org/wiki/Python_(язык)"}]})

WIKI_JSON = json.dumps({"query": {"search": [
    {"title": "Асинхронизация", "snippet": "<span>вики-текст</span>"}]}})

STACK_JSON = json.dumps({"items": [
    {"title": "How to gather", "link": "https://stackoverflow.com/questions/1", "body": "<p>body</p>"}]})

GH_JSON = json.dumps({"items": [
    {"full_name": "user/repo", "html_url": "https://github.com/user/repo",
     "description": "repo", "stargazers_count": 7}]})

NPM_JSON = json.dumps({"objects": [{"package": {"name": "axios", "version": "1.0.0",
                                                "description": "http client",
                                                "links": {"npm": "https://www.npmjs.com/package/axios"}}}]})

HN_JSON = json.dumps({"hits": [{"title": "HN story", "url": "https://news.ycombinator.com/item?id=1",
                                 "points": 42}]})

ARXIV_XML = """<feed><entry><title>Paper title</title>
<id>http://arxiv.org/abs/1234</id><summary>Paper summary</summary></entry></feed>"""


def main():
    # 1. разбор выдачи DuckDuckGo
    old_fetch = search_mod._fetch
    try:
        search_mod._fetch = fake_fetch({"html.duckduckgo.com": DDG_HTML})
        rows = search_mod.duckduckgo("запрос", count=5)
        check(len(rows) == 2, "ddg: распарсено %d результатов" % len(rows))
        check(rows[0]["url"] == "https://example.com/a", "ddg: uddg-ссылка не раскрыта (%s)" % rows[0]["url"])
        check(rows[0]["snippet"] == "Snippet A запрос", "ddg: сниппет не вытащен (%r)" % rows[0]["snippet"])
        check(rows[0]["source"] == "duckduckgo", "ddg: нет источника")

        search_mod._fetch = fake_fetch({"lite.duckduckgo.com": LITE_HTML})
        rows = search_mod.ddg_lite("запрос", count=5)
        check(len(rows) == 1 and rows[0]["url"] == "https://example.net/c",
              "lite: разбор lite-выдачи неверен (%s)" % rows[:1])

        search_mod._fetch = fake_fetch({"bing.com": BING_HTML})
        rows = search_mod.bing("запрос", count=5)
        check(len(rows) == 1 and rows[0]["url"] == "https://example.org/b",
              "bing: ck/a-редирект не раскрыт (%s)" % rows[:1])
        check(rows[0]["snippet"] == "Snippet B from bing запрос", "bing: сниппет не вытащен")
    finally:
        search_mod._fetch = old_fetch

    # 2. перебор движков: первый молчит — берём второй, дедуп по URL
    try:
        search_mod._fetch = fake_fetch({"html.duckduckgo.com": DDG_HTML,
                                        "lite.duckduckgo.com": LITE_HTML,
                                        "bing.com": BING_HTML})
        res = search_mod.search("запрос", count=3)
        urls = [r["url"] for r in res["results"]]
        check(len(urls) == len(set(urls)), "поиск: дубли не убраны (%s)" % urls)
        check("https://example.org/b" not in urls or "https://example.net/c" in urls,
              "поиск: стратегия отката не сработала (%s)" % urls)
        check(res["sources"], "поиск: не отчитались об источниках")
    finally:
        search_mod._fetch = old_fetch

    # 3. движок падает — поиск не падает, ошибка попадает в отчёт
    try:
        search_mod._fetch = fake_fetch({"html.duckduckgo.com": RuntimeError("anomaly"),
                                        "lite.duckduckgo.com": LITE_HTML})
        res = search_mod.search("запрос", count=2)
        check(res["results"], "поиск: после падения движка результатов нет")
        check("duckduckgo" in res["errors"], "поиск: ошибка движка не попала в отчёт (%s)" % res["errors"])
        check(res["sources"][0] == "ddg_lite", "поиск: откат не на тот движок (%s)" % res["sources"])
    finally:
        search_mod._fetch = old_fetch

    # 4. вертикали
    cases = [
        ("wikipedia", "ru.wikipedia.org", WIKI_JSON,
         lambda rows: rows and rows[0]["url"].startswith("https://ru.wikipedia.org/wiki/") and rows[0]["snippet"] == "вики-текст"),
        ("stackexchange", "api.stackexchange.com", STACK_JSON,
         lambda rows: rows and rows[0]["url"].startswith("https://stackoverflow.com/questions/")),
        ("github", "api.github.com", GH_JSON,
         lambda rows: rows and rows[0]["url"] == "https://github.com/user/repo" and "★7" in rows[0]["snippet"]),
        ("npm", "registry.npmjs.org", NPM_JSON,
         lambda rows: rows and rows[0]["url"] == "https://www.npmjs.com/package/axios"),
        ("hackernews", "hn.algolia.com", HN_JSON,
         lambda rows: rows and rows[0]["url"].startswith("https://news.ycombinator.com/")),
        ("arxiv", "export.arxiv.org", ARXIV_XML,
         lambda rows: rows and rows[0]["url"] == "http://arxiv.org/abs/1234"),
    ]
    for name, needle, payload, predicate in cases:
        try:
            search_mod._fetch = fake_fetch({needle: payload})
            rows = search_mod.ENGINES[name]("запрос", count=3)
            check(predicate(rows), "%s: разбор ответа неверен (%s)" % (name, rows[:1]))
        finally:
            search_mod._fetch = old_fetch

    # 5. вертикали добирают выдачу, когда поисковики молчат
    try:
        search_mod._fetch = fake_fetch({"html.duckduckgo.com": RuntimeError("anomaly"),
                                        "lite.duckduckgo.com": RuntimeError("anomaly"),
                                        "bing.com": RuntimeError("капча"),
                                        "ru.wikipedia.org": WIKI_JSON})
        res = search_mod.search("запрос", count=5)
        check(res["results"], "вертикали: пусто, хотя wikipedia отвечала")
        check("wikipedia" in res["sources"], "вертикали: wikipedia не позвали (%s)" % res["sources"])
        check(len(res["errors"]) >= 3, "вертикали: ошибки движков не собраны (%s)" % res["errors"])
    finally:
        search_mod._fetch = old_fetch

    # 6. неизвестный движок и алиасы
    res = search_mod.search("запрос", engine="яндекс")
    check(not res["results"] and res["errors"], "неизвестный движок: нет понятной ошибки")
    check("duckduckgo" in res["errors"]["яндекс"], "неизвестный движок: не перечислены доступные")
    try:
        search_mod._fetch = fake_fetch({"api.stackexchange.com": STACK_JSON})
        res = search_mod.search("запрос", engine="stackoverflow")
        check(res["results"], "алиас stackoverflow не работает")
    finally:
        search_mod._fetch = old_fetch

    # 6a. заглушка DuckDuckGo (anomaly) считается отказом, а не результатом
    try:
        search_mod._fetch = fake_fetch({"lite.duckduckgo.com": LITE_HTML,
                                        "html.duckduckgo.com": "<html>anomaly.js</html>",
                                        "duckduckgo.com": "<html>anomaly.js</html>"})
        rows = search_mod.duckduckgo("частый запрос", count=3)
        check(False, "ddg: заглушка отдана как результат (%s)" % rows)
    except RuntimeError as exc:
        check("anomaly" in str(exc), "ddg: заглушка не распознана (%s)" % exc)
    finally:
        search_mod._fetch = old_fetch

    # 6b. в auto-режиме заглушка первого движка не съедает выдачу
    try:
        search_mod._fetch = fake_fetch({"lite.duckduckgo.com": LITE_HTML,
                                        "html.duckduckgo.com": "<html>anomaly.js</html>",
                                        "duckduckgo.com": "<html>anomaly.js</html>",
                                        "bing.com": BING_HTML})
        res = search_mod.search("частый запрос", count=2)
        check(res["results"] and res["results"][0]["source"] == "ddg_lite",
              "auto: заглушка не переключила источник (%s)" % res["sources"])
        check("duckduckgo" in res["errors"], "auto: отказ DDG не попал в отчёт")
    finally:
        search_mod._fetch = old_fetch

    # 6c. выдача не по теме считается отказом движка, а не ответом
    try:
        search_mod._fetch = fake_fetch({"html.duckduckgo.com": JUNK_BING_HTML,
                                        "lite.duckduckgo.com": JUNK_BING_HTML,
                                        "bing.com": JUNK_BING_HTML,
                                        "rightdao.com": RIGHTDAO_HTML})
        res = search_mod.search("asyncio", count=3)
        check(not any("Магазин" in (r["title"] or "") for r in res["results"]),
              "мусорная выдача попала в результаты (%s)" % res["results"][:2])
        check("bing" in res["errors"], "мусорная выдача не отмечена как отказ (%s)" % res["errors"])
        check(res["results"] and res["results"][0]["source"] == "rightdao",
              "auto: после мусора не дошёл до рабочего движка (%s)" % res["sources"])
    finally:
        search_mod._fetch = old_fetch

    # 6d. новые движки: Right Dao, instant answer DuckDuckGo, версии продуктов
    try:
        search_mod._fetch = fake_fetch({"rightdao.com": RIGHTDAO_HTML})
        rows = search_mod.rightdao("asyncio", count=3)
        check(rows and rows[0]["url"] == "https://docs.python.org/3/asyncio.html",
              "rightdao: разбор неверен (%s)" % rows[:1])
    finally:
        search_mod._fetch = old_fetch
    try:
        search_mod._fetch = fake_fetch({"api.duckduckgo.com": DDG_IA_JSON})
        rows = search_mod.ddg_ia("python", count=3)
        check(rows and "Python" in (rows[0]["title"] or ""),
              "ddg_ia: разбор неверен (%s)" % rows[:1])
    finally:
        search_mod._fetch = old_fetch
    EOL_JSON = json.dumps([{"cycle": "3.15", "latest": "3.15.2", "releaseDate": "2026-10-09",
                             "eol": "2031-10-31"}])
    try:
        search_mod._fetch = fake_fetch({"endoflife.date": EOL_JSON})
        rows = search_mod.versions("latest python version", count=3)
        check(rows and "3.15.2" in rows[0]["title"],
              "versions: версия не отдана (%s)" % rows[:1])
        try:
            search_mod.versions("javascript version", count=3)
            check(False, "versions: лишний продукт не отсеян")
        except RuntimeError as exc:
            check("не понял" in str(exc), "versions: невнятная ошибка (%s)" % exc)
    finally:
        search_mod._fetch = old_fetch

    # 6e. имя движка нечувствительно к регистру, пробелам и дефисам
    try:
        search_mod._fetch = fake_fetch({"rightdao.com": RIGHTDAO_HTML})
        res = search_mod.search("asyncio", engine="Right Dao", count=2)
        check(res["results"] and res["engine"] == "rightdao",
              "имя движка не нормализовано (%s)" % res["engine"])
    finally:
        search_mod._fetch = old_fetch

    # 6f. тема проверяется по словам, а не по подстроке
    try:
        rows = [{"title": "Активация Windows | Microsoft Support",
                 "snippet": "Купить ключ", "url": "https://support.microsoft.com/activate"},
                {"title": "Лицензия MIT — Википедия",
                 "snippet": "Свободная лицензия", "url": "https://ru.wikipedia.org/wiki/MIT_License"}]
        kept = search_mod._relevant(rows, "лицензия MIT что это")
        check(len(kept) == 1 and "Википедия" in kept[0]["title"],
              "фильтр по подстроке: мусор прошёл (%s)" % [r["title"] for r in kept])
        kept2 = search_mod._relevant(rows, "submit form")
        check(not kept2, "фильтр: «submit» нашлось внутри слова (%s)" % [r["title"] for r in kept2])
    finally:
        search_mod._fetch = old_fetch

    # 7. инструмент web_search отдаёт источники и ошибки
    from harness import tools as tools_mod
    tk = tools_mod.Toolkit({"workspace": "C:/LocalHarness/tests/.tmp", "search_engine": "auto"})
    try:
        search_mod._fetch = fake_fetch({"html.duckduckgo.com": DDG_HTML})
        res = tk.call("web_search", {"query": "запрос"})
        check(res.get("ok") and res.get("results"), "инструмент: поиск не отдал результатов")
        check("sources" in res, "инструмент: нет списка источников")
    finally:
        search_mod._fetch = old_fetch

    # 8. повтор одного и того же успешного поиска замечается агентом
    from harness.agent import Agent
    cfg = json.load(open("C:/LocalHarness/config.json", encoding="utf-8"))
    cfg["workspace"] = "C:/LocalHarness/tests/.tmp"
    agent = Agent(cfg, Bridge(), "test-repeat")
    hints = []
    for _ in range(5):
        hints.append(agent._watch_repeat_ok("web_search", {"query": "node.js версия"}))
    check(hints[0] == "" and hints[1] == "", "повтор поиска: подсказка слишком рано (%s)" % hints[:2])
    check("ПОВТОРЯЕШЬ ТОТ ЖЕ ЗАПРОС" in hints[2], "повтор поиска: нет подсказки на третьем (%r)" % hints[2])
    check("engine=wikipedia" in hints[2], "повтор поиска: подсказка не советует другой источник (%r)" % hints[2])
    check("СТОП" in hints[4], "повтор поиска: нет стопа на пятом (%r)" % hints[4])
    check(agent.stalled, "повтор поиска: подход не оборвался")
    check(agent._watch_repeat_ok("list_dir", {"path": "C:/LocalHarness"}) == "",
          "повтор поиска: лишние инструменты не должны считаться повтором")

    print("FAILURES: %d" % len(failures))
    for item in failures:
        print(" -", item)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
