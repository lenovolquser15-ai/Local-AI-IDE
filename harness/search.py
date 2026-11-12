"""Глобальный веб-поиск: несколько движков и вертикальные базы.

Один поисковик перестал справляться: то отдаёт «anomaly», то пустую выдачу, то капчу.
Здесь движки разных провайдеров опрашиваются параллельно (внутри одного провайдера —
последовательно, чтобы не ловить капчу всем семейством сразу), результаты склеиваются
через reciprocal rank fusion и дедуплицируются, а вертикальные API (Wikipedia,
StackOverflow, GitHub, arXiv, npm, PyPI, Hacker News) добирают выдачу, когда поисковик
молчит. Каждый источник изолирован: его ошибка не роняет поиск, а попадает в отчёт
об источниках. Успешные ответы кэшируются на CACHE_TTL, отказавший движок отдыхает
COOLDOWN_* секунд — повторные запросы агента не добивают rate-limit.
"""
import base64
import copy
import json
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from html import unescape as html_unescape

from . import netio

DEFAULT_TIMEOUT = 20.0

CACHE_TTL = 600.0
CACHE_MAX = 128
COOLDOWN_BLOCKED = 300.0
COOLDOWN_ERROR = 60.0
RRF_K = 60

_state_lock = threading.Lock()
_cache = {}
_cooldown = {}


def reset_state():
    """Сброс кэша и кулдаунов (для тестов)."""
    with _state_lock:
        _cache.clear()
        _cooldown.clear()


def _cache_get(key):
    now = time.monotonic()
    with _state_lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return copy.deepcopy(hit[1])
        _cache.pop(key, None)
    return None


def _cache_put(key, result):
    now = time.monotonic()
    with _state_lock:
        for stale in [k for k, v in _cache.items() if v[0] <= now]:
            _cache.pop(stale, None)
        while len(_cache) >= CACHE_MAX:
            _cache.pop(next(iter(_cache)))
        _cache[key] = (now + CACHE_TTL, copy.deepcopy(result))


def _cooldown_left(name):
    with _state_lock:
        return max(0.0, _cooldown.get(name, 0.0) - time.monotonic())


def _cooldown_set(name, exc):
    text = str(exc).lower()
    blocked = any(w in text for w in ("anomaly", "captcha", "капча", "заглушк", "unusual traffic"))
    with _state_lock:
        _cooldown[name] = time.monotonic() + (COOLDOWN_BLOCKED if blocked else COOLDOWN_ERROR)


def _fetch(url, timeout=DEFAULT_TIMEOUT, headers=None, data=None):
    """Единая точка выхода в сеть — её подменяют в тестах."""
    res = netio.request("GET" if data is None else "POST", url, headers=headers, data=data,
                        timeout=timeout, max_bytes=3_000_000)
    if res["status"] >= 400:
        raise RuntimeError("HTTP %s" % res["status"])
    return res.get("text") or ""


def _clean(text, limit=400):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html_unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _norm_url(url):
    """Ключ для дедупа: без схемы, www и завершающего слэша."""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    host = (parsed.netloc or "").lower().replace("www.", "")
    path = (parsed.path or "/").rstrip("/") or "/"
    return host + path + (("?" + parsed.query) if parsed.query else "")


def _item(title, url, snippet="", source=""):
    return {"title": _clean(title, 300) or url, "url": url,
            "snippet": _clean(snippet, 500), "source": source}


def _bing_url(href):
    """Bing прячет адрес в base64-параметре u=a1<base64url> редиректа ck/a."""
    if "bing.com/ck/a" not in href:
        return href
    match = re.search(r"[?&]u=a1([A-Za-z0-9_\-]+)", href)
    if not match:
        return href
    raw = match.group(1)
    try:
        padded = raw + "=" * (-len(raw) % 4)
        return base64.urlsafe_b64decode(padded).decode("utf-8", "replace")
    except Exception:
        return href


# ---------- поисковики ----------

STOPWORDS = {
    "что", "как", "где", "кто", "это", "для", "или", "а", "и", "не", "в", "на", "с", "по",
    "the", "and", "for", "what", "how", "why", "when", "which", "with", "you", "are",
    "does", "into", "from", "your", "can", "best", "near", "mean", "use", "using",
}

EOL_PRODUCTS = {
    "python": "python", "nodejs": "nodejs", "node": "nodejs", "django": "django",
    "laravel": "laravel", "php": "php", "ruby": "ruby", "rails": "rails",
    "openjdk": "openjdk", "jdk": "openjdk", "java": "openjdk", "mysql": "mysql",
    "postgresql": "postgresql", "postgres": "postgresql", "mongodb": "mongodb",
    "ubuntu": "ubuntu", "debian": "debian", "dotnet": "dotnet", "nginx": "nginx",
    "apache": "apache-http-server", "elasticsearch": "elasticsearch", "redis": "redis",
    "kubernetes": "kubernetes", "docker": "docker", "terraform": "terraform",
    "gcc": "gcc", "chromium": "chromium", "chrome": "chromium", "firefox": "firefox",
    "windows": "windows", "android": "android", "ios": "ios", "macos": "macos",
}


def _tokens(query):
    """Значимые слова запроса: длинные или заглавные аббревиатуры, без служебных."""
    out = []
    for word in re.findall(r"\w+", (query or "").lower()):
        if len(word) < 3 and not (len(word) == 2 and word.isupper()):
            continue
        if word in STOPWORDS:
            continue
        out.append(word)
    return out


def _words(text):
    return re.findall(r"\w+", (text or "").lower())


def _hit(token, words):
    """Совпадает ли значимое слово запроса со словом выдачи.

    Сравнение по словам, а не по подстроке: иначе «mit» находится внутри
    «Microsoft» и «submit», а кириллическое «лицензия» — внутри «лицензионный».
    Для длинных слов берём общий префикс из 6 букв, чтобы переживать падежи
    («лицензия» / «лицензионный»).
    """
    if len(token) >= 6:
        head = token[:6]
        return any(word[:6] == head for word in words if len(word) >= 4)
    return any(word == token or (word.startswith(token) and len(word) - len(token) <= 2)
               for word in words)


def _relevant(items, query):
    """Отсекает выдачу, не относящуюся к запросу, и ставит точные совпадения выше.

    Поисковики любят отдавать мусор: Bing с этого адреса присылает чужие страницы,
    а DuckDuckGo — заглушку с фальшивыми ссылками. Результат, в котором нет ни
    одного значимого слова запроса, — не ответ на запрос. Среди оставшихся
    больше слов запроса — значит ближе к теме.

    Совпадение в заголовке или в домене весит больше, чем в сниппете: иначе
    «Активация Windows» проходит на слове «лицензия» из описания.
    """
    tokens = _tokens(query)
    if not tokens:
        return items
    scored = []
    for item in items:
        strong = _words(item.get("title"))
        host = re.sub(r"^https?://", "", (item.get("url") or "").split("/")[0]).lower()
        strong += _words(host)
        weak = _words(item.get("snippet"))
        score = 0
        for token in tokens:
            if _hit(token, strong):
                score += 2
            elif _hit(token, weak):
                score += 1
        if score:
            scored.append((score, item))
    scored.sort(key=lambda pair: -pair[0])
    return [item for _, item in scored]


def _blocked(html):
    """DuckDuckGo на частые запросы отдаёт страницу-заглушку с фальшивыми результатами."""
    low = (html or "").lower()
    return "anomaly" in low or "captcha" in low or "unusual traffic" in low


def duckduckgo(query, count=8, timeout=DEFAULT_TIMEOUT):
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query, "kl": "wt-wt"})
    html = _fetch(url, timeout)
    if _blocked(html):
        html = _fetch("https://duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query}), timeout)
    if _blocked(html):
        raise RuntimeError("duckduckgo отдал заглушку (anomaly) — запрос для него слишком частый")
    results = []
    for match in re.finditer(
        r'<a[^>]+class="result__a"[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>'
        r'(?P<rest>.*?)(?=<a[^>]+class="result__a"|<div class="nav-link|$)',
        html, re.S,
    ):
        href = html_unescape(match.group("href"))
        target = href
        if "uddg=" in href:
            found = re.search(r"[?&]uddg=([^&]+)", href)
            if found:
                target = urllib.parse.unquote(found.group(1))
        snippet = ""
        snip = re.search(r'class="result__snippet"[^>]*>(.*?)</(?:a|div)>', match.group("rest") or "", re.S)
        if snip:
            snippet = html_unescape(snip.group(1))
        if target.startswith("//"):
            target = "https:" + target
        if target.startswith("http"):
            results.append(_item(match.group("title"), target, snippet, "duckduckgo"))
        if len(results) >= count:
            break
    if not results:
        raise RuntimeError("duckduckgo не вернул результатов")
    return results


def ddg_lite(query, count=8, timeout=DEFAULT_TIMEOUT):
    html = _fetch("https://lite.duckduckgo.com/lite/?" + urllib.parse.urlencode({"q": query}), timeout)
    if _blocked(html):
        raise RuntimeError("duckduckgo lite отдал заглушку (anomaly)")
    results = []
    for match in re.finditer(r"<a\b[^>]*>(?P<title>.*?)</a>(?P<rest>.*?)(?=<a\b[^>]*class=['\"]result-link|\Z)",
                             html, re.S | re.I):
        tag = match.group(0)[:match.group(0).find(">") + 1]
        if "result-link" not in tag:
            continue
        href = re.search(r"href=['\"]([^'\"]+)['\"]", tag)
        if not href:
            continue
        target = html_unescape(href.group(1))
        if "uddg=" in target:
            found = re.search(r"[?&]uddg=([^&]+)", target)
            if found:
                target = urllib.parse.unquote(found.group(1))
        if target.startswith("//"):
            target = "https:" + target
        snippet = ""
        snip = re.search(r"class=['\"]result-snippet['\"][^>]*>(.*?)</td>", match.group("rest") or "", re.S)
        if snip:
            snippet = html_unescape(snip.group(1))
        if target.startswith("http"):
            results.append(_item(match.group("title"), target, snippet, "ddg_lite"))
        if len(results) >= count:
            break
    if not results:
        raise RuntimeError("duckduckgo lite не вернул результатов")
    return results


def bing(query, count=8, timeout=DEFAULT_TIMEOUT):
    url = "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query, "count": max(10, count)})
    html = _fetch(url, timeout)
    results = []
    for block in re.findall(r'<li class="b_algo".*?(?=<li class="b_algo"|</ol>|\Z)', html, re.S):
        head = re.search(r"<h2>(.*?)</h2>", block, re.S)
        if not head:
            continue
        link = re.search(r'<a[^>]+href="([^"]+)"', head.group(1))
        if not link:
            continue
        target = _bing_url(html_unescape(link.group(1)))
        title = re.sub(r"<[^>]+>", "", head.group(1))
        snippet = ""
        snip = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
        if snip:
            snippet = snip.group(1)
        if target.startswith("http"):
            results.append(_item(title, target, snippet, "bing"))
        if len(results) >= count:
            break
    if not results:
        raise RuntimeError("bing не вернул результатов")
    return results


def rightdao(query, count=8, timeout=DEFAULT_TIMEOUT):
    """Right Dao — независимый индекс, не блокирует по частоте запросов."""
    html = _fetch("https://rightdao.com/search?" + urllib.parse.urlencode({"q": query}), timeout)
    results = []
    for block in re.findall(r'<div class="item".*?(?=<div class="item"|<div class="niche"|\Z)', html, re.S):
        head = re.search(r'<div class="title">.*?<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not head:
            continue
        target = html_unescape(head.group(1))
        title = re.sub(r"<[^>]+>", " ", head.group(2))
        snippet = ""
        desc = re.search(r'<div class="description">(.*?)</div>', block, re.S)
        if desc:
            snippet = desc.group(1)
        if target.startswith("http"):
            results.append(_item(title, target, snippet, "rightdao"))
        if len(results) >= count:
            break
    if not results:
        raise RuntimeError("rightdao не вернул результатов")
    return results


def searxng(query, count=8, timeout=DEFAULT_TIMEOUT, base_url="http://127.0.0.1:8080"):
    """Свой инстанс SearXNG: агрегирует десятки движков с твоего IP, без общих лимитов.
    Включается галочкой в настройках; инстанс должен отдавать JSON (search.formats)."""
    url = (base_url or "").rstrip("/") + "/search?" + urllib.parse.urlencode({
        "q": query, "format": "json", "pageno": 1})
    text = _fetch(url, timeout)
    try:
        data = json.loads(text)
    except ValueError:
        raise RuntimeError("searxng вернул не JSON — включи search.formats: [html, json] в settings.yml инстанса")
    results = []
    for row in data.get("results") or []:
        if row.get("url"):
            results.append(_item(row.get("title") or "", row["url"], row.get("content") or "", "searxng"))
        if len(results) >= count:
            break
    if not results:
        raise RuntimeError("searxng не вернул результатов")
    return results


def _searxng_engine(base_url):
    def fn(query, count=8, timeout=DEFAULT_TIMEOUT):
        return searxng(query, count, timeout, base_url=base_url)
    fn.__name__ = "searxng"
    return fn


# ---------- вертикальные базы ----------

def ddg_ia(query, count=5, timeout=DEFAULT_TIMEOUT):
    """Instant Answer API DuckDuckGo — короткий ответ и смежные темы.
    Работает, когда HTML-выдача отдаёт заглушку."""
    url = "https://api.duckduckgo.com/?" + urllib.parse.urlencode({
        "q": query, "format": "json", "no_html": 1, "skip_disambig": 1})
    data = json.loads(_fetch(url, timeout))
    results = []
    abstract = (data.get("AbstractText") or "").strip()
    if abstract:
        link = data.get("AbstractURL") or "https://duckduckgo.com/?q=%s" % urllib.parse.quote(query)
        results.append(_item(data.get("Heading") or query, link, abstract, "duckduckgo:ia"))
    for key, label in (("Definition", "definition"), ("Answer", "answer")):
        text = (data.get(key) or "").strip()
        if text:
            link = data.get(key + "URL") or "https://duckduckgo.com/?q=%s" % urllib.parse.quote(query)
            results.append(_item("%s: %s" % (label, text[:120]), link, text, "duckduckgo:ia"))
    for topic in data.get("RelatedTopics") or []:
        if topic.get("FirstURL"):
            results.append(_item(topic.get("Text") or "", topic["FirstURL"], "", "duckduckgo:ia"))
        for sub in topic.get("Topics") or []:
            if sub.get("FirstURL"):
                results.append(_item(sub.get("Text") or "", sub["FirstURL"], "", "duckduckgo:ia"))
        if len(results) >= count:
            break
    if not results:
        raise RuntimeError("duckduckgo (instant answer) не нашёл ничего")
    return results[:count]


def versions(query, count=5, timeout=DEFAULT_TIMEOUT):
    """Версии и сроки поддержки продуктов (endoflife.date): «последняя версия Python»."""
    words = re.findall(r"\w+", (query or "").lower().replace(".", " ").replace("_", " "))
    product = None
    for name in sorted(EOL_PRODUCTS, key=len, reverse=True):
        if all(part in words for part in name.split()):
            product = EOL_PRODUCTS[name]
            break
    if not product:
        raise RuntimeError("не понял, о каком продукте спросили версию")
    data = json.loads(_fetch("https://endoflife.date/api/%s.json" % product, timeout))
    results = []
    for row in data[:count]:
        latest = row.get("latest") or row.get("cycle")
        title = "%s %s" % (product, latest)
        parts = []
        if row.get("releaseDate"):
            parts.append("вышел %s" % row["releaseDate"])
        if row.get("eol"):
            parts.append("поддержка до %s" % row["eol"])
        if row.get("lts"):
            parts.append("LTS")
        results.append(_item(title, "https://endoflife.date/%s" % product,
                             ", ".join(parts), "versions"))
    if not results:
        raise RuntimeError("endoflife.date не знает такого продукта")
    return results


def wikipedia(query, count=5, timeout=DEFAULT_TIMEOUT):
    results = []
    for lang in ("ru", "en"):
        url = "https://%s.wikipedia.org/w/api.php?" % lang + urllib.parse.urlencode({
            "action": "query", "list": "search", "srsearch": query, "format": "json",
            "srlimit": count, "utf8": 1})
        try:
            data = json.loads(_fetch(url, timeout))
        except Exception:
            continue
        for item in (data.get("query") or {}).get("search") or []:
            title = item.get("title") or ""
            link = "https://%s.wikipedia.org/wiki/%s" % (lang, urllib.parse.quote(title.replace(" ", "_")))
            results.append(_item(title, link, item.get("snippet") or "", "wikipedia:" + lang))
        if results:
            break
    if not results:
        raise RuntimeError("wikipedia не нашла ничего")
    return results


def stackexchange(query, count=5, timeout=DEFAULT_TIMEOUT):
    url = "https://api.stackexchange.com/2.3/search/advanced?" + urllib.parse.urlencode({
        "order": "desc", "sort": "relevance", "q": query, "site": "stackoverflow",
        "pagesize": count, "filter": "default"})
    data = json.loads(_fetch(url, timeout))
    results = []
    for item in data.get("items") or []:
        results.append(_item(item.get("title") or "", item.get("link") or "",
                             item.get("body") or "", "stackoverflow"))
    if not results:
        raise RuntimeError("stackoverflow не нашёл ничего")
    return results


def github(query, count=5, timeout=DEFAULT_TIMEOUT):
    url = "https://api.github.com/search/repositories?" + urllib.parse.urlencode({
        "q": query, "per_page": count})
    text = _fetch(url, timeout, headers={"accept": "application/vnd.github+json"})
    data = json.loads(text)
    results = []
    for item in data.get("items") or []:
        snippet = item.get("description") or ""
        if item.get("stargazers_count"):
            snippet = "%s ★%s" % (snippet, item["stargazers_count"])
        results.append(_item(item.get("full_name") or "", item.get("html_url") or "",
                             snippet, "github"))
    if not results:
        raise RuntimeError("github не нашёл ничего")
    return results


def arxiv(query, count=5, timeout=DEFAULT_TIMEOUT):
    url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode({
        "search_query": "all:%s" % query, "max_results": count})
    xml = _fetch(url, timeout)
    results = []
    for entry in re.findall(r"<entry>(.*?)</entry>", xml, re.S):
        title = re.search(r"<title>(.*?)</title>", entry, re.S)
        link = re.search(r'<id>(.*?)</id>', entry, re.S)
        summary = re.search(r"<summary>(.*?)</summary>", entry, re.S)
        if not title or not link:
            continue
        results.append(_item(title.group(1), link.group(1).strip(), summary.group(1) if summary else "", "arxiv"))
        if len(results) >= count:
            break
    if not results:
        raise RuntimeError("arxiv не нашёл ничего")
    return results


def npm(query, count=5, timeout=DEFAULT_TIMEOUT):
    url = "https://registry.npmjs.org/-/v1/search?" + urllib.parse.urlencode({
        "text": query, "size": count})
    data = json.loads(_fetch(url, timeout))
    results = []
    for obj in data.get("objects") or []:
        pkg = obj.get("package") or {}
        links = (pkg.get("links") or obj.get("links") or {})
        url = links.get("npm") or ("https://www.npmjs.com/package/%s" % pkg.get("name") or "")
        results.append(_item(pkg.get("name") or "", url,
                             "%s %s" % (pkg.get("description") or "", pkg.get("version") or ""), "npm"))
    if not results:
        raise RuntimeError("npm не нашёл ничего")
    return results


def pypi(query, count=5, timeout=DEFAULT_TIMEOUT):
    name = re.sub(r"[^A-Za-z0-9._-]", "", (query or "").strip().split()[0] if query else "")
    if not name:
        raise RuntimeError("пустое имя пакета")
    data = json.loads(_fetch("https://pypi.org/pypi/%s/json" % name, timeout))
    info = data.get("info") or {}
    results = [_item(info.get("name") or name, info.get("package_url") or
                     "https://pypi.org/project/%s/" % name,
                     "%s %s" % (info.get("summary") or "", info.get("version") or ""), "pypi")]
    for url in (info.get("project_urls") or {}).values():
        if isinstance(url, str) and url.startswith("http"):
            results.append(_item(info.get("name") or name, url, info.get("summary") or "", "pypi"))
            break
    return results


def hackernews(query, count=5, timeout=DEFAULT_TIMEOUT):
    url = "https://hn.algolia.com/api/v1/search?" + urllib.parse.urlencode({
        "query": query, "hitsPerPage": count})
    data = json.loads(_fetch(url, timeout))
    results = []
    for hit in data.get("hits") or []:
        link = hit.get("url") or ("https://news.ycombinator.com/item?id=%s" % hit.get("objectID"))
        snippet = hit.get("story_text") or ""
        if hit.get("points"):
            snippet = "%s ★%s" % (snippet, hit["points"])
        results.append(_item(hit.get("title") or hit.get("story_title") or "", link, snippet, "hackernews"))
    if not results:
        raise RuntimeError("hackernews не нашёл ничего")
    return results


GENERAL = [duckduckgo, ddg_lite, bing, rightdao]
VERTICALS = [ddg_ia, versions, wikipedia, stackexchange, github, hackernews, arxiv, npm, pypi]
ENGINES = {fn.__name__: fn for fn in GENERAL + VERTICALS}
ALIASES = {"ddg": "duckduckgo", "duckduckgo_html": "duckduckgo", "lite": "ddg_lite",
           "so": "stackexchange", "stack": "stackexchange", "stackoverflow": "stackexchange",
           "wiki": "wikipedia", "hn": "hackernews", "pypi": "pypi", "npm": "npm",
           "github": "github", "arxiv": "arxiv", "bing": "bing", "rightdao": "rightdao",
           "ddg_ia": "ddg_ia", "ia": "ddg_ia", "versions": "versions", "eol": "versions",
           "version": "versions"}


def engine_names():
    return sorted(ENGINES)


def _norm_engine(name):
    """«Right Dao», «right-dao», «DDG Lite» — одно и то же имя движка."""
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


ENGINES_NORM = {_norm_engine(key): fn for key, fn in ENGINES.items()}
ALIASES_NORM = {_norm_engine(key): value for key, value in ALIASES.items()}


def _run(fn, query, count, timeout):
    return fn(query, count, timeout) or []


# Один провайдер — одна очередь: html и lite DuckDuckGo делят rate-limit,
# одновременный запрос к обоим ловит капчу быстрее.
PROVIDERS = {"duckduckgo": "ddg", "ddg_lite": "ddg", "ddg_ia": "ddg"}


def _run_group(fns, query, count, timeout, errors, relevance, respect_cooldown=True):
    got = []
    for fn in fns:
        name = fn.__name__
        if respect_cooldown:
            left = _cooldown_left(name)
            if left:
                errors[name] = "cooldown: пропущен, отдохнёт ещё %.0f с" % left
                continue
        try:
            items = _run(fn, query, count, timeout)
        except Exception as exc:
            errors[name] = str(exc)[:200]
            _cooldown_set(name, exc)
            continue
        if relevance:
            items = _relevant(items, query)
            if not items:
                errors[name] = "выдача не относится к запросу"
                continue
        got.append((name, items))
        if sum(len(part) for _, part in got) >= count:
            break
    return got


def _run_parallel(groups, query, count, timeout, errors, relevance):
    if len(groups) == 1:
        return _run_group(groups[0][1], query, count, timeout, errors, relevance)
    with ThreadPoolExecutor(max_workers=len(groups)) as pool:
        futures = [pool.submit(_run_group, fns, query, count, timeout, errors, relevance)
                   for _, fns in groups]
        return [pair for fut in futures for pair in fut.result()]


def _rrf(lists):
    """Reciprocal rank fusion: результат, который несколько движков поставили высоко,
    обгоняет одиночного фаворита. Дедуп по URL; при равенстве — порядок плана."""
    fused = {}
    for idx, (_, items) in enumerate(lists):
        for rank, item in enumerate(items):
            key = _norm_url(item.get("url") or "")
            if not key:
                continue
            entry = fused.get(key)
            if entry is None:
                fused[key] = [1.0 / (RRF_K + rank + 1), idx, rank, item]
            else:
                entry[0] += 1.0 / (RRF_K + rank + 1)
                if (idx, rank) < (entry[1], entry[2]):
                    entry[1], entry[2], entry[3] = idx, rank, item
    return [e[3] for e in sorted(fused.values(), key=lambda e: (-e[0], e[1], e[2]))]


def _groups(plan):
    groups = []
    for fn in plan:
        provider = PROVIDERS.get(fn.__name__, fn.__name__)
        if groups and groups[-1][0] == provider:
            groups[-1][1].append(fn)
        else:
            groups.append((provider, [fn]))
    return groups


def search(query, count=8, engine="auto", timeout=DEFAULT_TIMEOUT, searxng_url=None):
    """Ищет по нескольким источникам. engine='auto' — параллельно с RRF-слиянием.
    searxng_url — включённый в настройках свой инстанс; опрашивается первым."""
    query = (query or "").strip()
    if not query:
        return {"query": "", "engine": engine, "results": [], "sources": [], "errors": {}}
    count = max(1, min(int(count or 8), 25))
    raw_name = (engine or "auto").strip()
    name = _norm_engine(raw_name)
    if name != "auto":
        name = ALIASES_NORM.get(name, name)

    general = list(GENERAL)
    engines_norm = dict(ENGINES_NORM)
    if searxng_url:
        searxng_fn = _searxng_engine(searxng_url)
        general.insert(0, searxng_fn)
        engines_norm["searxng"] = searxng_fn

    if name != "auto" and name not in engines_norm:
        if name == "searxng":
            err = "searxng не включён: отметь галочку в настройках и укажи URL своего инстанса"
        else:
            err = "неизвестный движок. Доступны: %s" % ", ".join(
                engine_names() + (["searxng"] if searxng_url else []))
        return {"query": query, "engine": raw_name, "results": [], "sources": [],
                "errors": {raw_name: err}}

    key = (query.lower(), count, name, searxng_url or "")
    cached = _cache_get(key)
    if cached is not None:
        cached["cached"] = True
        return cached

    errors = {}
    general_names = {fn.__name__ for fn in general}
    if name != "auto":
        fn = engines_norm[name]
        lists = _run_group([fn], query, count, timeout, errors,
                           relevance=fn.__name__ in general_names, respect_cooldown=False)
        results = _rrf(lists)
        sources = [n for n, items in lists if items]
    else:
        lists = _run_parallel(_groups(general), query, count, timeout, errors, relevance=True)
        results = _rrf(lists)
        sources = [n for n, items in lists if items]
        if len(results) < count:
            vlists = _run_parallel(_groups(VERTICALS), query, count, timeout, errors, relevance=False)
            sources += [n for n, items in vlists if items]
            seen = {_norm_url(r.get("url") or "") for r in results}
            for item in _rrf(vlists):
                if len(results) >= count:
                    break
                k = _norm_url(item.get("url") or "")
                if k and k not in seen:
                    seen.add(k)
                    results.append(item)

    res = {"query": query, "engine": name, "count": count, "results": results[:count],
           "sources": sources, "errors": errors}
    if res["results"]:
        _cache_put(key, res)
    return res