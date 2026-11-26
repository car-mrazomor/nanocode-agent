"""Community skills (the agentskills.io format: a folder with a SKILL.md) installed from GitHub.

    /skills                                  list your skills (as before)
    /skills hub [query]                      browse the skills in the index repo (anthropics/skills)
    /skills install <name>                   install one from the index by name
    /skills install owner/repo[/path]        or from any repo (a GitHub URL works too)
    /skills remove <name>                    delete one
    --project installs into (or removes from) .nanocode/skills instead of ~/.nanocode/skills.
"""
from __future__ import annotations

import asyncio
import io
import os
import re
import shutil
import time
import zipfile
from pathlib import PurePosixPath

import nanocode as nc
from nanokit import command

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
DEFAULT_INDEX = "anthropics/skills"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
MAX_ZIP = 100 * 1024 * 1024
MAX_SKILL = 20 * 1024 * 1024
HUB_CACHE = {}  # repo -> (time, [skills])
USAGE = ("/skills hub [query] · /skills install <name | owner/repo[/path] | github url> [--project] · "
         "/skills remove <name> [--project]")


class HubError(Exception):
    pass


def headers():
    found = {"Accept": "application/vnd.github+json", "User-Agent": "nanocode"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        found["Authorization"] = f"Bearer {token}"
    return found


def index_repo():
    return str(nc.st.config.get("skills_hub") or DEFAULT_INDEX).strip("/")


def parse_source(source):
    """(owner, repo, ref or None, path) from owner/repo[/path] or a github.com URL; None if it isn't one."""
    text = re.sub(r"^(https?://)?(www\.)?github\.com/", "", source.strip()).strip("/")
    parts = [p for p in text.split("/") if p]
    if len(parts) < 2:
        return None
    owner, repo, rest, ref = parts[0], re.sub(r"\.git$", "", parts[1]), parts[2:], None
    if not (REPO_RE.match(owner) and REPO_RE.match(repo)):
        return None
    if rest[:1] in (["tree"], ["blob"]) and len(rest) >= 2:
        ref, rest = rest[1], rest[2:]
    if rest and rest[-1] == "SKILL.md":
        rest = rest[:-1]
    return owner, repo, ref, "/".join(rest)


def frontmatter(text):
    """(meta, body) of a SKILL.md, or raises HubError if it isn't a valid one."""
    text = text.lstrip("﻿")
    if not text.startswith("---"):
        raise HubError("SKILL.md has no frontmatter (it must start with ---)")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise HubError("SKILL.md's frontmatter isn't closed with ---")
    meta = {}
    for line in parts[1].splitlines():
        if ":" in line and not line[:1].isspace():
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip().strip("\"'")
    if not NAME_RE.match(meta.get("name", "")):
        raise HubError(f"SKILL.md's name {meta.get('name')!r} isn't valid (lowercase letters, digits and -)")
    if not meta.get("description"):
        raise HubError(f"skill {meta['name']} has no description")
    return meta, parts[2].strip()


async def get(url, **params):
    async with nc.httpx.AsyncClient(timeout=60, follow_redirects=True, headers=headers()) as client:
        response = await client.get(url, params=params or None)
    return check(response, url)


def check(response, url):
    if response.status_code == 404:
        raise HubError(f"not found on GitHub: {url.replace(API + '/repos/', '')}")
    if response.status_code in (403, 429):
        raise HubError("GitHub's rate limit was hit; try again later or set GITHUB_TOKEN")
    if response.status_code >= 400:
        raise HubError(f"GitHub answered {response.status_code}")
    return response


def safe_path(rel):
    path = PurePosixPath(rel)
    return not path.is_absolute() and ".." not in path.parts and ":" not in rel and "\\" not in rel


def skills_in_zip(data, path=""):
    """The skills in a repository zip (under `path`): [{"name", "description", "files": {rel: bytes}, "where"}].
    Raises HubError when there are none, or one is invalid."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise HubError("the download isn't a zip file") from None
    entries = {}
    for info in archive.infolist():
        name = info.filename.split("/", 1)[1] if "/" in info.filename else ""   # drop "owner-repo-sha/"
        is_link = (info.external_attr >> 16) & 0o170000 == 0o120000
        if name and not info.is_dir() and not is_link and safe_path(name):
            entries[name] = info
    path = path.strip("/")
    folders = sorted({str(PurePosixPath(n).parent) for n in entries if PurePosixPath(n).name == "SKILL.md"})
    folders = [f for f in folders if not path or f == path or f.startswith(path + "/")]
    if not folders:
        raise HubError(f"no SKILL.md found{' under ' + path if path else ''}")
    found = []
    for folder in folders:
        prefix = "" if folder == "." else folder + "/"
        files, size = {}, 0
        for name, info in entries.items():
            if name.startswith(prefix):
                size += info.file_size
                if size > MAX_SKILL:
                    raise HubError(f"{folder} is over {MAX_SKILL // 1024 // 1024} MB")
                files[name[len(prefix):]] = archive.read(info)
        meta, _ = frontmatter(files["SKILL.md"].decode("utf-8", "replace"))
        found.append({"name": meta["name"], "description": " ".join(meta["description"].split()),
                      "files": files, "where": folder})
    return found


async def download(owner, repo, ref=None):
    """The repository's zip, streamed: a download over MAX_ZIP stops as soon as it's known to be too big."""
    url = f"{API}/repos/{owner}/{repo}/zipball" + (f"/{ref}" if ref else "")
    too_big = HubError("the repository is too big to download")
    chunks, size = [], 0
    async with nc.httpx.AsyncClient(timeout=60, follow_redirects=True, headers=headers()) as client:
        async with client.stream("GET", url) as response:
            check(response, url)
            length = response.headers.get("content-length", "")
            if length.strip().isdigit() and int(length) > MAX_ZIP:
                raise too_big
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_ZIP:
                    raise too_big
                chunks.append(chunk)
    return b"".join(chunks)


async def hub_index(repo=None):
    """The skills in the index repo: [{"name", "description", "path"}], cached for an hour."""
    repo = repo or index_repo()
    cached = HUB_CACHE.get(repo)
    if cached and time.time() - cached[0] < 3600:
        return cached[1]
    tree = (await get(f"{API}/repos/{repo}/git/trees/HEAD", recursive="1")).json().get("tree") or []
    paths = [item["path"] for item in tree if item.get("type") == "blob" and
             PurePosixPath(item.get("path", "")).name == "SKILL.md"]
    limit = asyncio.Semaphore(8)

    async def describe(path):
        async with limit:
            try:
                meta, _ = frontmatter((await get(f"{RAW}/{repo}/HEAD/{path}")).text)
                return {"name": meta["name"], "description": " ".join(meta["description"].split()),
                        "path": str(PurePosixPath(path).parent)}
            except (HubError, nc.httpx.HTTPError):
                return None

    skills = [s for s in await asyncio.gather(*(describe(p) for p in paths[:200])) if s]
    skills.sort(key=lambda s: s["name"])
    HUB_CACHE[repo] = (time.time(), skills)
    return skills


def skills_root(project):
    return nc.project_dir() / "skills" if project else nc.home_dir() / "skills"


async def confirm(what, preview):
    if nc.st.headless:
        nc.out(f"{nc.RED}⏺ {what}: add --yes to do this without asking{nc.RESET}\n")
        return False
    return await nc.ask_permission(what, preview, False)


def install_files(root, skill):
    """Writes a skill folder (replacing an older copy) and returns its path."""
    root.mkdir(parents=True, exist_ok=True)
    target = root / skill["name"]
    staging = root / f".{skill['name']}.installing"
    shutil.rmtree(staging, ignore_errors=True)
    for rel, data in skill["files"].items():
        path = staging.joinpath(*PurePosixPath(rel).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    if target.exists():
        shutil.rmtree(target)
    staging.rename(target)
    return target


async def install(source, project=False, yes=False):
    parsed = parse_source(source)
    if parsed is None:                         # a name from the index
        match = next((s for s in await hub_index() if s["name"] == source.strip().lower()), None)
        if match is None:
            raise HubError(f"no skill named '{source}' in {index_repo()} (/skills hub lists them)")
        owner, repo = index_repo().split("/", 1)
        parsed = (owner, repo, None, match["path"])
    owner, repo, ref, path = parsed
    nc.out(f"  {nc.DIM}⎿  downloading {owner}/{repo}{'@' + ref if ref else ''}…{nc.RESET}")
    skills = await asyncio.to_thread(skills_in_zip, await download(owner, repo, ref), path)
    root = skills_root(project)
    lines = [f"{s['name']} ({len(s['files'])} files){' replaces yours' if (root / s['name']).exists() else ''}"
             for s in skills]
    nc.out(f"{nc.BOLD}Skills in {owner}/{repo}{'/' + path if path else ''}{nc.RESET}\n" +
           "\n".join(f"  {nc.CYAN}{s['name']:<24}{nc.RESET} {s['description'][:80]}" for s in skills) +
           f"\n  {nc.DIM}skills are instructions (and scripts) the model will follow; read them if unsure{nc.RESET}")
    if not yes and not await confirm("install skills", f"{', '.join(lines)} → {nc.tilde(root)}"):
        return []
    if project:
        nc.ensure_project_dir()
    return await asyncio.to_thread(lambda: [install_files(root, s) for s in skills])


@command("/skills", "skills: list, /skills hub [query], install <name|owner/repo>, remove <name>")
async def skills_command(arg):
    words = arg.split()
    sub = words[0].lower() if words else ""
    flags = {w for w in words[1:] if w.startswith("--")}
    rest = [w for w in words[1:] if not w.startswith("--")]
    try:
        if sub == "install" and rest:
            installed = await install(rest[0], project="--project" in flags, yes="--yes" in flags)
            for path in installed:
                nc.out(f"{nc.GREEN}⏺ Installed{nc.RESET} {path.name} {nc.DIM}→ {nc.tilde(path)} "
                       f"(run it with /{path.name}){nc.RESET}")
            if installed:
                nc.out()
        elif sub == "hub":
            await show_hub(" ".join(rest).lower())
        elif sub == "remove" and rest:
            await remove(rest[0], project="--project" in flags, yes="--yes" in flags)
        else:
            nc.show_skills()
            nc.out(f"  {nc.DIM}{USAGE}{nc.RESET}\n")
    except (HubError, nc.httpx.HTTPError, OSError) as e:
        nc.out(f"{nc.RED}⏺ {e}{nc.RESET}\n")


async def show_hub(query):
    nc.out(f"  {nc.DIM}⎿  reading {index_repo()}…{nc.RESET}")
    skills = await hub_index()
    shown = [s for s in skills if not query or query in s["name"] or query in s["description"].lower()]
    installed = nc.list_skills()
    lines = [f"{nc.BOLD}Skills hub{nc.RESET} {nc.DIM}({index_repo()}: {len(shown)} of {len(skills)}; "
             f"/skills install <name>){nc.RESET}"]
    for s in shown:
        mark = f" {nc.fg('success')}installed{nc.RESET}" if s["name"] in installed else ""
        lines.append(f"  {nc.CYAN}{s['name']:<24}{nc.RESET} {s['description'][:90]}{mark}")
    if not shown:
        lines.append(f"  {nc.DIM}nothing matches '{query}'{nc.RESET}")
    nc.out("\n".join(lines) + "\n")


async def remove(name, project=False, yes=False):
    roots = [skills_root(project)] if project else [skills_root(False), skills_root(True)]
    target = next((root / name for root in roots if NAME_RE.match(name) and (root / name / "SKILL.md").is_file()),
                  None)
    if target is None:
        raise HubError(f"no installed skill named '{name}' in {' or '.join(nc.tilde(r) for r in roots)}")
    if not yes and not await confirm("remove skill", nc.tilde(target)):
        return
    shutil.rmtree(target)
    nc.out(f"{nc.GREEN}⏺ Removed{nc.RESET} {name} {nc.DIM}({nc.tilde(target)}){nc.RESET}\n")


nc.COMMANDS["/skills"] = "skills: list, /skills hub [query], install <name|owner/repo>, remove <name>"
