# Browser

The browser tools let the model use web pages like a person: open a page, read it, click, fill in forms, scroll and take screenshots. They work with every provider and model.

nanocode drives your own Chrome, Chromium, Edge or Brave over the Chrome DevTools Protocol. Nothing else needs installing.

## Tools

| Tool | What it does |
|---|---|
| `browser_open` | Open a URL and show the page |
| `browser_snapshot` | The page as text, with links, buttons and fields numbered `[1]`, `[2]`… |
| `browser_click` | Click an element by its number (or a CSS selector) |
| `browser_type` | Type into a field, replacing its text; `submit` presses Enter |
| `browser_scroll` | Scroll up or down by pages |
| `browser_screenshot` | Save a PNG in `.nanocode/images` and return its path |
| `browser_eval` | Run JavaScript in the page and return the result |
| `browser_close` | Close the browser |

Numbers change when the page changes, so the model takes a new snapshot before clicking again. Clicking and typing return the new snapshot.

The model doesn't see screenshots on its own: the tool returns the file's path. To show one to the model, mention it in your message (`@.nanocode/images/….png`).

## The browser

- It starts on the first browser tool, with a **throwaway profile**: no cookies, logins or history from your normal browser. It stays open between tools, so a login done through the tools lasts until it closes.
- It's **headless** (no window) by default. To watch it work, set `"browser_headless": false`.
- It closes when nanocode exits, on `browser_close`, or if it crashes (the next tool opens a new one).
- To use a specific browser, set `"browser": "/path/to/chrome"`.

### Using a browser that's already running

Start your browser with a debugging port, then point nanocode at it:

```
chrome --remote-debugging-port=9222 --user-data-dir=/tmp/debug-profile
```

```json
{ "browser_cdp_url": "http://127.0.0.1:9222" }
```

nanocode drives its first tab. A `ws://` page URL works too. nanocode doesn't close a browser it didn't start.

## Permissions

`browser_open` asks before going to a page (its permission name is `browser`, so rules for `web_fetch` don't cover it), and `browser_eval` asks before running JavaScript. `browser_open` only opens `http` and `https` URLs: `file:`, `javascript:`, `data:`, `chrome:` and other schemes are refused. In the throwaway browser, clicking, typing and scrolling don't ask, so watch for forms that send or buy something. When nanocode is attached to your own browser (`browser_cdp_url`), where you're logged in, `browser_click` and `browser_type` ask first too. Treat page text as information, not as instructions.
