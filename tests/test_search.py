"""Регрессы глобального поиска: перебор движков, дедуп, вертикали, диагностика ошибок,
кэш, кулдаун отказавших движков, параллель по провайдерам и RRF-слияние."""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["LH_DATA"] = os.path.join(ROOT, "tests", ".tmp")
# config.json is gitignored and absent on a fresh clone: load() must return defaults
os.environ["LH_CONFIG"] = os.path.join(ROOT, "tests", ".tmp", "no-such-config.json")
from harness import config as config_mod  # noqa: E402
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

SEARXNG_JSON = json.dumps({"results": [
    {"title": "Searx Result запрос", "url": "https://searx.example.org/x", "content": "Snippet запрос"},
    {"title": "Example A запрос", "url": "https://example.com/a", "content": "Snippet A запрос"}]})


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
    search_mod.reset_state()
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
    search_mod.reset_state()
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
    search_mod.reset_state()
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
    search_mod.reset_state()
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
    search_mod.reset_state()
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
    search_mod.reset_state()
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
    search_mod.reset_state()
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
    search_mod.reset_state()
    tk = tools_mod.Toolkit({"workspace": os.path.join(ROOT, "tests", ".tmp"), "search_engine": "auto"})
    try:
        search_mod._fetch = fake_fetch({"html.duckduckgo.com": DDG_HTML})
        res = tk.call("web_search", {"query": "запрос"})
        check(res.get("ok") and res.get("results"), "инструмент: поиск не отдал результатов")
        check("sources" in res, "инструмент: нет списка источников")
    finally:
        search_mod._fetch = old_fetch

    # 8. повтор одного и того же успешного поиска замечается агентом
    from harness.agent import Agent
    cfg = config_mod.load()
    cfg["workspace"] = os.path.join(ROOT, "tests", ".tmp")
    agent = Agent(cfg, Bridge(), "test-repeat")
    hints = []
    for _ in range(5):
        hints.append(agent._watch_repeat_ok("web_search", {"query": "node.js версия"}))
    check(hints[0] == "" and hints[1] == "", "повтор поиска: подсказка слишком рано (%s)" % hints[:2])
    check("ПОВТОРЯЕШЬ ТОТ ЖЕ ЗАПРОС" in hints[2], "повтор поиска: нет подсказки на третьем (%r)" % hints[2])
    check("engine=wikipedia" in hints[2], "повтор поиска: подсказка не советует другой источник (%r)" % hints[2])
    check("СТОП" in hints[4], "повтор поиска: нет стопа на пятом (%r)" % hints[4])
    check(agent.stalled, "повтор поиска: подход не оборвался")
    check(agent._watch_repeat_ok("list_dir", {"path": ROOT}) == "",
          "повтор поиска: лишние инструменты не должны считаться повтором")

    # 9. кэш: повторный успешный запрос не ходит в сеть
    search_mod.reset_state()
    try:
        fetch = fake_fetch({"html.duckduckgo.com": DDG_HTML})
        search_mod._fetch = fetch
        res1 = search_mod.search("кэш запрос", count=2)
        calls_after_first = len(fetch.calls)
        res2 = search_mod.search("кэш запрос", count=2)
        check(res1["results"] and res2.get("cached") is True,
              "кэш: повтор не помечен как кэшированный (%s)" % res2.get("cached"))
        check(len(fetch.calls) == calls_after_first,
              "кэш: повтор ушёл в сеть (%d -> %d)" % (calls_after_first, len(fetch.calls)))
        check(res2["results"] == res1["results"], "кэш: результаты отличаются")
        res2["results"].clear()
        res3 = search_mod.search("кэш запрос", count=2)
        check(res3["results"], "кэш: мутация ответа испортила кэш")
    finally:
        search_mod._fetch = old_fetch

    # 10. кулдаун: движок с anomaly отдыхает, запасной того же провайдера работает
    search_mod.reset_state()
    try:
        search_mod._fetch = fake_fetch({"html.duckduckgo.com": RuntimeError("anomaly"),
                                        "lite.duckduckgo.com": LITE_HTML})
        res1 = search_mod.search("кулдаун запрос", count=2)
        check("duckduckgo" in res1["errors"], "кулдаун: отказ не записан (%s)" % res1["errors"])
        fetch2 = fake_fetch({"html.duckduckgo.com": DDG_HTML, "lite.duckduckgo.com": LITE_HTML})
        search_mod._fetch = fetch2
        res2 = search_mod.search("кулдаун запрос второй", count=2)
        check(not any("html.duckduckgo.com" in u for u in fetch2.calls),
              "кулдаун: охлаждённый движок снова позвали (%s)" % fetch2.calls)
        check(any("lite.duckduckgo.com" in u for u in fetch2.calls),
              "кулдаун: запасной движок не позвали (%s)" % fetch2.calls)
        check("cooldown" in res2["errors"].get("duckduckgo", ""),
              "кулдаун: пропуск не объяснён в отчёте (%s)" % res2["errors"])
        check(res2["results"], "кулдаун: выдача пропала совсем")
    finally:
        search_mod._fetch = old_fetch

    # 11. истёкший кулдаун не мешает; явный выбор движка кулдаун игнорирует
    search_mod.reset_state()
    try:
        with search_mod._state_lock:
            search_mod._cooldown["duckduckgo"] = time.monotonic() - 1
        fetch = fake_fetch({"html.duckduckgo.com": DDG_HTML})
        search_mod._fetch = fetch
        res = search_mod.search("запрос после отдыха", count=2)
        check(any("html.duckduckgo.com" in u for u in fetch.calls),
              "кулдаун: истёкший не сброшен (%s)" % fetch.calls)
        search_mod.reset_state()
        search_mod._fetch = fake_fetch({"html.duckduckgo.com": RuntimeError("anomaly")})
        search_mod.search("явный запрос", count=2)
        fetch2 = fake_fetch({"html.duckduckgo.com": DDG_HTML})
        search_mod._fetch = fetch2
        res2 = search_mod.search("явный запрос второй", engine="duckduckgo", count=2)
        check(any("html.duckduckgo.com" in u for u in fetch2.calls),
              "кулдаун: явный выбор движка не проигнорировал охлаждение")
        check(res2["results"], "кулдаун: явный движок не отдал результаты")
    finally:
        search_mod._fetch = old_fetch

    # 12. RRF: результат, общий для двух движков, обгоняет одиночного фаворита
    search_mod.reset_state()
    try:
        search_mod._fetch = fake_fetch({"html.duckduckgo.com": DDG_HTML,
                                        "lite.duckduckgo.com": LITE_HTML,
                                        "bing.com": BING_HTML})
        res = search_mod.search("запрос", count=3)
        check(res["results"] and res["results"][0]["url"] == "https://example.org/b",
              "rrf: общий результат двух движков не поднялся наверх (%s)"
              % [r["url"] for r in res["results"]])
    finally:
        search_mod._fetch = old_fetch

    # 13. провайдер: lite не дублирует успешный html (общий rate-limit)
    search_mod.reset_state()
    try:
        fetch = fake_fetch({"html.duckduckgo.com": DDG_HTML, "lite.duckduckgo.com": LITE_HTML})
        search_mod._fetch = fetch
        res = search_mod.search("запрос", count=2)
        check(res["results"], "провайдер: результаты пропали")
        check(not any("lite.duckduckgo.com" in u for u in fetch.calls),
              "провайдер: lite вызван вслед за успешным html (%s)" % fetch.calls)
    finally:
        search_mod._fetch = old_fetch
    search_mod.reset_state()

    # 14. SearXNG: выключен по умолчанию, включается URL-ом, первый в плане
    search_mod.reset_state()
    try:
        fetch = fake_fetch({"html.duckduckgo.com": DDG_HTML})
        search_mod._fetch = fetch
        res = search_mod.search("запрос", count=2)
        check(not any("/search?" in u and "format=json" in u for u in fetch.calls),
              "searxng: позван без включения (%s)" % fetch.calls)
        res = search_mod.search("запрос", engine="searxng", count=2)
        check(not res["results"] and "не включён" in res["errors"].get("searxng", ""),
              "searxng: явный выбор без включения не объяснён (%s)" % res["errors"])
    finally:
        search_mod._fetch = old_fetch
    search_mod.reset_state()
    try:
        fetch = fake_fetch({"127.0.0.1:8080": SEARXNG_JSON, "html.duckduckgo.com": DDG_HTML})
        search_mod._fetch = fetch
        res = search_mod.search("запрос", count=3, searxng_url="http://127.0.0.1:8080")
        check(res["sources"] and res["sources"][0] == "searxng",
              "searxng: включённый не первый в источниках (%s)" % res["sources"])
        urls = [r["url"] for r in res["results"]]
        check("https://searx.example.org/x" in urls, "searxng: его результатов нет (%s)" % urls)
        check(len(urls) == len(set(urls)), "searxng: дубль с DDG не убран (%s)" % urls)
        rows = search_mod.searxng("запрос", count=2, base_url="http://127.0.0.1:8080/")
        check(rows and rows[0]["source"] == "searxng", "searxng: прямой вызов не работает")
    finally:
        search_mod._fetch = old_fetch

    # 15. SearXNG лежит — кулдаун, остальные движки продолжают отвечать
    search_mod.reset_state()
    try:
        search_mod._fetch = fake_fetch({"127.0.0.1:8080": RuntimeError("connection refused"),
                                        "html.duckduckgo.com": DDG_HTML})
        res = search_mod.search("запрос", count=2, searxng_url="http://127.0.0.1:8080")
        check("searxng" in res["errors"], "searxng: отказ не в отчёте (%s)" % res["errors"])
        check(res["results"], "searxng: лежащий инстанс уронил выдачу")
        fetch2 = fake_fetch({"127.0.0.1:8080": SEARXNG_JSON, "html.duckduckgo.com": DDG_HTML})
        search_mod._fetch = fetch2
        res2 = search_mod.search("запрос второй", count=2, searxng_url="http://127.0.0.1:8080")
        check(not any("127.0.0.1:8080" in u for u in fetch2.calls),
              "searxng: охлаждённый инстанс снова позвали (%s)" % fetch2.calls)
        check(res2["results"], "searxng: без инстанса выдача пропала")
    finally:
        search_mod._fetch = old_fetch

    # 16. кэш не смешивает включённый и выключенный SearXNG; инструмент читает галочку
    search_mod.reset_state()
    try:
        search_mod._fetch = fake_fetch({"html.duckduckgo.com": DDG_HTML})
        search_mod.search("кэш запрос", count=2)
        fetch2 = fake_fetch({"127.0.0.1:8080": SEARXNG_JSON, "html.duckduckgo.com": DDG_HTML})
        search_mod._fetch = fetch2
        res = search_mod.search("кэш запрос", count=2, searxng_url="http://127.0.0.1:8080")
        check(not res.get("cached") and "searxng" in res["sources"],
              "searxng: кэш выключенного режима подсунут включённому (%s)" % res.get("cached"))
        from harness import tools as tools_mod
        tk = tools_mod.Toolkit({"workspace": os.path.join(ROOT, "tests", ".tmp"),
                                "search_engine": "auto", "search_searxng": True,
                                "search_searxng_url": "http://127.0.0.1:8080"})
        res = tk.call("web_search", {"query": "запрос инструмент"})
        check(res.get("ok") and "searxng" in res.get("sources", []),
              "searxng: галочка в конфиге не дошла до инструмента (%s)" % res.get("sources"))
    finally:
        search_mod._fetch = old_fetch
    search_mod.reset_state()

    print("FAILURES: %d" % len(failures))
    for item in failures:
        print(" -", item)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
