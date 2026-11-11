import gzip
import io
import json
import re
import urllib.parse
from html import unescape as html_unescape
import zlib
from html.parser import HTMLParser

from . import dnsfix

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "template"}

    def __init__(self):
        HTMLParser.__init__(self)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in ("p", "br", "div", "li", "h1", "h2", "h3", "h4", "tr", "section", "article"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.parts.append(data)


def html_to_text(html, limit=12000):
    parser = _TextExtractor()
    try:
        parser.feed(html or "")
    except Exception:
        pass
    text = " ".join(parser.parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    text = "\n".join(line.strip() for line in text.split("\n") if line.strip())
    if len(text) > limit:
        return text[:limit] + "\n…[обрезано %d знаков]" % (len(text) - limit)
    return text


def _decode_body(raw, encoding):
    if not raw:
        return b""
    if encoding == "gzip":
        try:
            return gzip.decompress(raw)
        except Exception:
            return raw
    if encoding == "deflate":
        try:
            return zlib.decompress(raw)
        except Exception:
            try:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
            except Exception:
                return raw
    return raw


def _charset(headers, body):
    ctype = headers.get("content-type", "")
    match = re.search(r"charset=([\w-]+)", ctype, re.I)
    if match:
        return match.group(1)
    sample = body[:2048].decode("latin1", "replace")
    match = re.search(r'charset=["\']?([\w-]+)', sample, re.I)
    if match:
        return match.group(1)
    return "utf-8"


def request(method, url, headers=None, data=None, timeout=30.0, max_bytes=3_000_000, _hops=0):
    if _hops > 5:
        raise RuntimeError("слишком много редиректов: %s" % url)
    parsed = urllib.parse.urlsplit(url)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    send_headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/json,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "ru,en;q=0.8",
        "Connection": "close",
    }
    send_headers.update({k.lower(): v for k, v in (headers or {}).items()})
    if isinstance(data, (dict, list)):
        body = json.dumps(data).encode("utf-8")
        send_headers.setdefault("content-type", "application/json")
    elif isinstance(data, str):
        body = data.encode("utf-8")
    elif data is None:
        body = None
    else:
        body = data
    conn = dnsfix.connection_for(url, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=send_headers)
        resp = conn.getresponse()
        raw = resp.read(max_bytes)
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        status = resp.status
        location = resp_headers.get("location")
    finally:
        try:
            conn.close()
        except Exception:
            pass
    raw = _decode_body(raw, resp_headers.get("content-encoding", ""))
    if status in (301, 302, 303, 307, 308) and location:
        nxt = urllib.parse.urljoin(url, location)
        return request("GET" if status in (301, 302, 303) else method, nxt, headers, None, timeout, max_bytes, _hops + 1)
    text = raw.decode(_charset(resp_headers, raw), "replace")
    return {"status": status, "headers": resp_headers, "url": url, "body": raw, "text": text}


def get(url, timeout=30.0, headers=None):
    return request("GET", url, headers=headers, timeout=timeout)


def web_search(query, count=8, engine="duckduckgo"):
    if engine == "duckduckgo_html_post":
        url = "https://html.duckduckgo.com/html/"
        res = request("POST", url, data=urllib.parse.urlencode({"q": query, "kl": "wt-wt"}),
                      headers={"content-type": "application/x-www-form-urlencoded"})
    else:
        url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query, "kl": "wt-wt"})
        res = request("GET", url)
    html = res.get("text", "")
    if res["status"] >= 400 or "anomaly" in html[:2000].lower():
        alt = "https://duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
        res = request("GET", alt)
        html = res.get("text", "")
    results = []
    for link in re.finditer(
        r'<a[^>]+class="result__a"[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>'
        r'(?P<rest>.*?)(?=<a[^>]+class="result__a"|<div class="nav-link|$)',
        html, re.S,
    ):
        href = html_unescape(link.group("href"))
        target = href
        if "uddg=" in href:
            match = re.search(r"[?&]uddg=([^&]+)", href)
            if match:
                target = urllib.parse.unquote(match.group(1))
        title = html_to_text(html_unescape(link.group("title")), 300)
        snippet = ""
        snip = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', link.group("rest") or "", re.S)
        if snip:
            snippet = html_to_text(html_unescape(snip.group(1)), 400)
        if target.startswith("//"):
            target = "https:" + target
        if target.startswith("http") and title:
            results.append({"title": title, "url": target, "snippet": snippet})
        if len(results) >= count:
            break
    return {"query": query, "engine": engine, "results": results, "status": res["status"]}


def web_fetch(url, limit=12000, timeout=30.0):
    res = get(url, timeout=timeout)
    ctype = res["headers"].get("content-type", "")
    if "json" in ctype or res["text"].lstrip()[:1] in "{[":
        try:
            json.loads(res["text"])
            return {"url": res["url"], "status": res["status"], "kind": "json",
                    "text": res["text"][:limit], "title": ""}
        except Exception:
            pass
    if "html" in ctype or "<html" in res["text"][:400].lower() or "<!doctype" in res["text"][:200].lower():
        title = ""
        match = re.search(r"<title[^>]*>(.*?)</title>", res["text"], re.S | re.I)
        if match:
            title = html_to_text(match.group(1), 200)
        return {"url": res["url"], "status": res["status"], "kind": "html",
                "title": title, "text": html_to_text(res["text"], limit)}
    return {"url": res["url"], "status": res["status"], "kind": "text",
            "title": "", "text": res["text"][:limit]}
