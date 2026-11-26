"""Web search and page reading for every model and provider, through the user's own browser (headless, with a throwaway profile) or a plain request."""
from __future__ import annotations

import asyncio
import base64
from html.parser import HTMLParser
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
import httpx

import nanocode as nc

__all__ = [
    'WEB_UA',
    'SEARCH_ENGINES',
    'find_browser',
    'browser_dom',
    'plain_get',
    'load_page',
    'PageText',
    'page_text',
    'SearchResults',
    'clean_result_url',
    'search_openrouter',
    'tool_web_search',
    'tool_web_fetch',
    'tool_remember',
]


# --- Web: search and read pages through the user's own browser (headless), for any model and provider ---

WEB_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/131.0.0.0 Safari/537.36")
SEARCH_ENGINES = ("duckduckgo", "bing", "google", "openrouter")


def find_browser():
    """Chrome, Chromium, Edge or Brave installed on this machine (or the "browser" path in the config)."""
    configured = nc.st.config.get("browser")
    if configured and Path(configured).exists():
        return configured
    if sys.platform == "darwin":
        apps = ["Google Chrome.app/Contents/MacOS/Google Chrome", "Chromium.app/Contents/MacOS/Chromium",
                "Microsoft Edge.app/Contents/MacOS/Microsoft Edge", "Brave Browser.app/Contents/MacOS/Brave Browser"]
        places = [Path("/Applications") / a for a in apps] + [Path.home() / "Applications" / a for a in apps]
    elif os.name == "nt":
        places = [Path(os.path.expandvars(p)) for p in (
            r"%ProgramFiles%\Google\Chrome\Application\chrome.exe", r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
            r"%LocalAppData%\Google\Chrome\Application\chrome.exe", r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
            r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe")]
    else:
        places = []
    for place in places:
        if place.exists():
            return str(place)
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge",
                 "brave-browser", "chrome"):
        if shutil.which(name):
            return shutil.which(name)
    return None


async def browser_dom(url, timeout=45):
    """The page after JavaScript ran, from a headless copy of the user's browser with a throwaway profile."""
    browser = find_browser()
    if not browser:
        return None
    profile = tempfile.mkdtemp(prefix="nanocode-browser-")
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            browser, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
            "--disable-extensions", "--mute-audio", f"--user-data-dir={profile}", f"--user-agent={WEB_UA}",
            "--virtual-time-budget=7000", "--window-size=1280,2000", "--dump-dom", url,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            **({"start_new_session": True} if os.name != "nt" else {}))   # so its helper processes stop with it
        data, _ = await asyncio.wait_for(proc.communicate(), timeout)
        html = data.decode("utf-8", errors="replace")
        return html if "<" in html else None
    except (OSError, asyncio.TimeoutError):
        return None
    finally:
        if proc is not None and proc.returncode is None:      # timed out, or Esc stopped the turn
            await asyncio.shield(nc.reap(proc))
        shutil.rmtree(profile, ignore_errors=True)


async def plain_get(url):
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers={"User-Agent": WEB_UA,
                                     "Accept-Language": "en"}) as http:
            resp = await http.get(url)
    except httpx.HTTPError as e:
        raise nc.ToolError(f"couldn't load {url}: {type(e).__name__}") from e
    if resp.status_code >= 400:
        raise nc.ToolError(f"{url} answered HTTP {resp.status_code}")
    return resp.text


async def load_page(url, browser_first=True):
    """Rendered by the browser when there is one, else a plain request. (html, how)"""
    if not browser_first:
        try:
            return await plain_get(url), "http"
        except nc.ToolError:
            pass
    html = await browser_dom(url)
    if html and len(html) > 500:
        return html, "browser"
    return await plain_get(url), "http"


class PageText(HTMLParser):
    """Readable text from HTML: no scripts or styles, line breaks at blocks."""
    SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "head", "canvas", "select", "button"}
    BLOCK = {"p", "div", "br", "li", "tr", "section", "article", "header", "footer", "h1", "h2", "h3", "h4", "h5",
             "h6", "pre", "table", "ul", "ol", "blockquote", "main", "nav", "aside", "form", "hr", "dd", "dt"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip, self.title, self.in_title = [], 0, "", False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "title":
            self.in_title = True

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif not self.skip:
            self.parts.append(data)

    def text(self):
        lines = [" ".join(line.split()) for line in "".join(self.parts).split("\n")]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def page_text(html):
    parser = PageText()
    parser.feed(html)
    return " ".join(parser.title.split()), parser.text()


class SearchResults(HTMLParser):
    """Results from a DuckDuckGo, Bing or Google results page: [(title, url, snippet)]."""

    def __init__(self, engine):
        super().__init__(convert_charrefs=True)
        self.engine, self.results, self.cur, self.field, self.in_h2 = engine, [], None, None, False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if self.engine == "duckduckgo":
            if tag == "a" and "result__a" in cls:
                self.cur = {"title": "", "url": a.get("href", ""), "snippet": ""}
                self.results.append(self.cur)
                self.field = "title"
            elif "result__snippet" in cls and self.cur is not None:
                self.field = "snippet"
        elif self.engine == "bing":
            if tag == "li" and "b_algo" in cls:
                self.cur = {"title": "", "url": "", "snippet": ""}
                self.results.append(self.cur)
            elif self.cur is not None and tag == "h2":
                self.in_h2 = True
            elif self.cur is not None and tag == "a" and self.in_h2 and not self.cur["url"]:
                self.cur["url"], self.field = a.get("href", ""), "title"
            elif self.cur is not None and tag == "p" and not self.cur["snippet"]:
                self.field = "snippet"
        elif self.engine == "google":
            href = a.get("href", "")
            if tag == "a" and (href.startswith("http") or href.startswith("/url?")):
                target = clean_result_url(href)
                own = re.match(r"https?://([a-z]+\.)*google\.[a-z.]+/", target)   # Google's own links (sign-in, …)
                self.cur = None if own else {"title": "", "url": href, "snippet": "", "h3": False}
            elif tag == "h3" and self.cur is not None and not self.cur["h3"]:
                self.cur["h3"], self.field = True, "title"
                self.results.append(self.cur)

    def handle_endtag(self, tag):
        if tag == "h2":
            self.in_h2 = False
        if (self.engine == "duckduckgo" and tag == "a") or tag in ("h3", "p", "h2") or \
                (self.engine == "bing" and tag == "a" and self.field == "title"):
            self.field = None

    def handle_data(self, data):
        if self.cur is not None and self.field:
            self.cur[self.field] += data


def clean_result_url(url):
    from urllib.parse import parse_qs, urlparse, unquote
    parsed = urlparse(url if not url.startswith("//") else "https:" + url)
    query = parse_qs(parsed.query)
    if "uddg" in query:                       # DuckDuckGo redirect
        return unquote(query["uddg"][0])
    if parsed.path == "/url" and "q" in query:  # Google redirect
        return query["q"][0]
    if parsed.netloc.endswith("bing.com") and parsed.path.startswith("/ck/") and query.get("u", [""])[0][:2] == "a1":
        # Bing redirect: the target is base64 after "a1"
        raw = query["u"][0][2:]
        try:
            return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
        except (ValueError, UnicodeDecodeError):
            return url
    return url


async def search_openrouter(query, limit):
    """OpenRouter's own web search (its web plugin) with the chosen model: costs a little extra per search."""
    key = nc.st.openrouter_key or os.environ.get("OPENROUTER_API_KEY") or nc.st.config.get("openrouter_api_key")
    if not key:
        raise nc.ToolError("the openrouter search engine needs an OpenRouter key (nanocode setup)")
    model = nc.st.config.get("search_model") or (nc.st.model if nc.st.provider == "openrouter" else "openrouter/auto")
    payload = {"model": model, "plugins": [{"id": "web", "max_results": limit}], "messages": [{"role": "user",
               "content": f"Search the web for: {query}\nList the most relevant results, each as: title — URL — "
                          "one-line summary."}]}
    async with httpx.AsyncClient(timeout=90) as http:
        resp = await http.post(nc.OPENROUTER_URL, json=payload, headers={"Authorization": f"Bearer {key}",
                               "X-Title": "nanocode", "HTTP-Referer": f"https://github.com/{nc.REPO}"})
    if resp.status_code != 200:
        raise nc.ToolError(nc.api_error(resp.status_code, resp.text))
    data = resp.json()
    message = (data.get("choices") or [{}])[0].get("message") or {}
    nc.st.usage["cost"] += (data.get("usage") or {}).get("cost") or 0
    sources = [a.get("url_citation", {}) for a in message.get("annotations") or [] if a.get("type") == "url_citation"]
    text = message.get("content") or ""
    if sources:
        text += "\n\nSources:\n" + "\n".join(f"- {c.get('title', '')} {c.get('url', '')}" for c in sources)
    return text


@nc.tool("web_search", "Search the web (through the user's browser) for current information, docs, errors or news. "
      "Returns titles, URLs and snippets; read a page with web_fetch.",
      {"query": {"type": "string"}, "engine": {"type": "string", "enum": list(SEARCH_ENGINES),
       "description": "default: the user's choice (usually duckduckgo)"},
       "limit": {"type": "integer", "description": "results, default 8"}}, ["query"])
async def tool_web_search(args):
    from urllib.parse import quote_plus
    query = args["query"].strip()
    engine = args.get("engine") or nc.st.config.get("search_engine", "duckduckgo")
    if engine not in SEARCH_ENGINES:
        engine = "duckduckgo"
    limit = min(max(int(args.get("limit") or 8), 1), 20)
    if not await nc.allowed("web", f"search ({engine}): {query}"):
        return nc.DENIED
    if engine == "openrouter":
        return await search_openrouter(query, limit)
    urls = {"duckduckgo": "https://html.duckduckgo.com/html/?q=", "bing": "https://www.bing.com/search?setlang=en&q=",
            "google": "https://www.google.com/search?hl=en&q="}
    async def attempt(name, browser_first):
        try:
            html, how = await load_page(urls[name] + quote_plus(query), browser_first)
        except nc.ToolError:
            return [], "none"
        parser = SearchResults(name)
        parser.feed(html)
        return [r for r in parser.results if r["url"] and r["title"].strip()], how

    # DuckDuckGo's no-JavaScript page is made for plain requests (it turns headless browsers away);
    # Bing and Google work better in the browser. Each falls back to the other way, then to DuckDuckGo.
    results, how = await attempt(engine, engine != "duckduckgo")
    if not results:
        results, how = await attempt(engine, engine == "duckduckgo")
    if not results and engine != "duckduckgo":
        engine = "duckduckgo"
        results, how = await attempt(engine, False)
    if not results:
        return f"no results for '{query}' ({engine})"
    lines, seen = [], set()
    for r in results:
        url = clean_result_url(r["url"])
        if url in seen or "duckduckgo.com/y.js" in url:
            continue
        seen.add(url)
        snippet = " ".join(r["snippet"].split())[:240]
        lines.append(f"{len(lines) + 1}. {' '.join(r['title'].split())}\n   {url}" + (f"\n   {snippet}" if snippet else ""))
        if len(lines) >= limit:
            break
    return f"{engine} · via {how}\n\n" + "\n".join(lines)


@nc.tool("web_fetch", "Read a web page (rendered by the user's browser, JavaScript included) as plain text.",
      {"url": {"type": "string"}, "max_chars": {"type": "integer", "description": "default 20000"}}, ["url"])
async def tool_web_fetch(args):
    url = args["url"].strip()
    if not re.match(r"https?://", url):
        url = "https://" + url
    if not await nc.allowed("web", f"fetch {url}"):
        return nc.DENIED
    html, how = await load_page(url)
    title, text = page_text(html)
    limit = min(max(int(args.get("max_chars") or 20_000), 1000), 100_000)
    if len(text) > limit:
        text = text[:limit] + f"\n\n… [{len(text) - limit} more characters; ask for a bigger max_chars to read on]"
    return f"{title}\n{url} · via {how}\n\n{text or '(no readable text on this page)'}"


@nc.tool("remember", "Save a lasting fact, preference or lesson to memory so it's known in future chats. "
      "scope: project (default) or global (all projects).",
      {"fact": {"type": "string"}, "scope": {"type": "string", "enum": ["project", "global"]}}, ["fact"])
async def tool_remember(args):
    # Global memory goes into every future chat's instructions: ask first, so text from a web page or an email
    # can't quietly plant lasting instructions.
    if args.get("scope") == "global" and not await nc.allowed("remember", f"(global) {args['fact']}"):
        return nc.DENIED
    return f"saved to {nc.tilde(nc.add_memory(args['fact'], args.get('scope') or 'project'))}"
