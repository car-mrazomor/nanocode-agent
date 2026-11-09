# Web search

`web_search` and `web_fetch` work with every provider and model, because nanocode runs them itself.

## How pages are loaded

- **Your browser.** Pages load in your installed Chrome, Chromium, Edge or Brave, headless, with a throwaway profile. JavaScript-heavy pages work.
- **No browser?** A plain HTTP request is used instead.
- **A specific browser:** set `"browser": "/path/to/chrome"` in the config.

## Search engines

| `"search_engine"` | Notes |
|---|---|
| `duckduckgo` | Default. Fast, works with a plain request |
| `bing` | Through the browser |
| `google` | Often blocks headless browsers; nanocode then falls back to DuckDuckGo |
| `openrouter` | OpenRouter's web plugin does the search, with your model or `"search_model"`. Costs a little extra per search |

Result links are cleaned of redirect wrappers (DuckDuckGo, Google, Bing), so the model sees the real URLs.

## Permission

Going online asks first. Answer `a` to allow web access for the rest of the session.
