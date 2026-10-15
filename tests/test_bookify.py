#!/usr/bin/env -S uv run --script --quiet
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Regression tests for bookify.

The default suite is stdlib-only and fast: it builds books from fixtures and
drives the local server over HTTP. Every check here corresponds to a bug that
was actually found and fixed, so a failure means a regression, not a style
preference.

    ./tests/test_bookify.py              # build + server suite
    ./tests/test_bookify.py --browser    # also drive the UI (needs playwright)
    ./tests/test_bookify.py -v           # show each check

The --browser suite needs the Playwright Python package and a browser:
    uv tool install playwright && playwright install chromium
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOOKIFY = ROOT / "bookify"
# Windows can't exec a shebang script, so go through uv explicitly there
BOOKIFY_CMD = ([str(BOOKIFY)] if os.name != "nt"
               else ["uv", "run", "--script", "--quiet", str(BOOKIFY)])

PASS: list[str] = []
FAIL: list[str] = []
VERBOSE = False


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        PASS.append(name)
        if VERBOSE:
            print(f"  ok   {name}")
    else:
        FAIL.append(f"{name}{' — ' + detail if detail else ''}")
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def child_env(extra: dict | None = None) -> dict:
    env = {**os.environ, **(extra or {})}
    # uv resolves this script's deps on first run; on networks that intercept
    # TLS it needs the system trust store or the fetch fails with UnknownIssuer
    env.setdefault("UV_NATIVE_TLS", "1")
    return env


def run_build(src: Path, out: Path, *extra: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*BOOKIFY_CMD, str(src), "--no-serve", "-o", str(out), *extra],
        capture_output=True, text=True, cwd=ROOT, timeout=600, env=child_env(env))


def get(url: str, timeout: float = 10) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def post(url: str, payload, timeout: float = 30) -> tuple[int, bytes]:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# ---------------------------------------------------------------- fixtures

def write_fixture(root: Path) -> None:
    (root / "guide").mkdir(parents=True, exist_ok=True)
    (root / "img").mkdir(parents=True, exist_ok=True)
    # README.md and README.rst both want README.html -- the collision bug
    (root / "README.md").write_text(
        "# Readme MD\n\nMARKER_MD. See [the rst](README.rst), "
        "[the guide](guide/page.md) and [alt](guide/alt.markdown).\n\n"
        "![diagram](img/diagram.png)\n", encoding="utf-8")
    (root / "README.rst").write_text(
        "Readme RST\n==========\n\nMARKER_RST\n", encoding="utf-8")
    (root / "guide" / "page.md").write_text(
        "# Guide Page\n\nBack to [readme](../README.md).\n\n"
        "![up](../img/diagram.png)\n", encoding="utf-8")
    (root / "guide" / "alt.markdown").write_text(
        "# Alt Page\n\nMARKER_ALT, a sibling with a different md extension.\n",
        encoding="utf-8")
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000d4944415478da63f8cfc0f00f0005000100ffab8e36"
        "890000000049454e44ae426082")
    (root / "img" / "diagram.png").write_bytes(png)


class Serve:
    """bookify serving a built book, torn down on exit."""

    def __init__(self, src: Path, out: Path, *extra: str, env: dict | None = None):
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        # a browser command that succeeds and opens nothing
        no_browser = "true" if os.name != "nt" else "cmd /c rem"
        full_env = child_env({**(env or {}), "BROWSER": no_browser})
        self.proc = subprocess.Popen(
            [*BOOKIFY_CMD, str(src), "-o", str(out), "--keep-alive",
             "--serve", str(self.port), *extra],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            cwd=ROOT, env=full_env, start_new_session=True)

    def __enter__(self) -> "Serve":
        for _ in range(200):
            if self.proc.poll() is not None:
                raise RuntimeError("bookify exited: " + (self.proc.stdout.read() or ""))
            try:
                if get(self.url + "/index.html", timeout=2)[0] == 200:
                    return self
            except OSError:
                pass
            time.sleep(0.1)
        raise RuntimeError("server never came up")

    def __exit__(self, *exc) -> None:
        import signal
        try:
            if os.name == "nt":   # uv spawns python; /T takes the tree
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                               capture_output=True, check=True)
            else:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        except (OSError, subprocess.CalledProcessError):
            self.proc.kill()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class FakeOllama:
    """Stands in for Ollama so provider streaming is testable offline."""

    ANSWER = "A reconcile loop is idempotent, so a second pass changes nothing."

    def __init__(self):
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        answer = self.ANSWER

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/api/tags"):
                    b = json.dumps({"models": [{"name": "llama3.2:3b"}]}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(b)))
                    self.end_headers()
                    self.wfile.write(b)
                    return
                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(n) or b"{}")
                last = (req.get("messages") or [{}])[-1].get("content", "")
                if "TRIGGER_ERROR" in last:
                    b = json.dumps({"error": "stub exploded"}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(b)))
                    self.end_headers()
                    self.wfile.write(b)
                    return
                self.close_connection = True
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                try:
                    for word in answer.split():
                        self.wfile.write(
                            (json.dumps({"message": {"content": word + " "}}) + "\n").encode())
                        self.wfile.flush()
                        time.sleep(0.01)
                    self.wfile.write((json.dumps({"done": True}) + "\n").encode())
                    self.wfile.flush()
                except OSError:
                    pass

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), H)

    def __enter__(self) -> "FakeOllama":
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


# ---------------------------------------------------------------- build suite

def broken_links(html: Path) -> list[str]:
    broken = []
    for page in html.rglob("*.html"):
        text = page.read_text(encoding="utf-8", errors="replace")
        for href in set(part.split('"')[0] for part in text.split('href="')[1:]):
            target = href.split("#")[0]
            if not target or target.startswith(
                    ("http://", "https://", "mailto:", "data:", "/")):
                continue
            if not (page.parent / target).exists():
                broken.append(f"{page.name} -> {href}")
    return broken


def test_build(tmp: Path) -> None:
    print("build")
    src, out = tmp / "src", tmp / "out"
    write_fixture(src)
    r = run_build(src, out)
    ok = r.returncode == 0
    check("build succeeds", ok, (r.stderr or r.stdout).strip()[-300:])
    html, md = out / "html", out / "markdown"
    if not ok or not (html / "README.html").exists():
        check("html book produced", False, "build did not produce a book; "
              "skipping the checks that depend on it")
        return

    # sources differing only by extension must not overwrite each other
    check("README.md keeps README.html",
          (html / "README.html").exists()
          and "MARKER_MD" in (html / "README.html").read_text())
    check("README.rst gets its own page",
          (html / "README.rst.html").exists()
          and "MARKER_RST" in (html / "README.rst.html").read_text())
    check("no duplicate html output paths",
          len({p.relative_to(html) for p in html.rglob("*.html")})
          == len(list(html.rglob("*.html"))))

    # links point at the real output names, not a guessed suffix swap
    readme = (html / "README.html").read_text()
    check("link to .rst retargeted", 'href="README.rst.html"' in readme)
    check("link to .markdown retargeted", 'href="guide/alt.html"' in readme)

    broken = broken_links(html)
    check("every relative link resolves", not broken, "; ".join(broken[:4]))

    # assets a page points at must exist in the output
    check("asset copied into html book", (html / "img" / "diagram.png").is_file())
    check("asset copied into md book", (md / "img" / "diagram.png").is_file())
    check("asset bytes unchanged",
          (html / "img" / "diagram.png").read_bytes()
          == (src / "img" / "diagram.png").read_bytes())

    # markdown book
    check("SUMMARY.md written", (md / "SUMMARY.md").is_file())
    check("BOOK.md written", (md / "BOOK.md").is_file())
    check("md book has no name collision",
          (md / "README.md").is_file() and (md / "README.rst.md").is_file())
    check("md links retargeted",
          "](README.rst.md)" in (md / "README.md").read_text())

    # generated client script must be valid JS if node is around
    if shutil.which("node"):
        rc = subprocess.run(["node", "--check", str(html / "book.js")],
                            capture_output=True, text=True)
        check("book.js parses", rc.returncode == 0, rc.stderr.strip()[-200:])

    check("output dir is gitignored",
          (out / ".gitignore").read_text().strip() == "*")

    search = (html / "search.js").read_text(encoding="utf-8") if (html / "search.js").is_file() else ""
    check("search index written",
          search.startswith("window.BOOKIFY_SEARCH=") and "MARKER_ALT" in search and "Alt Page" in search)
    if shutil.which("node") and search:
        rc = subprocess.run(["node", "--check", str(html / "search.js")], capture_output=True, text=True)
        check("search.js parses", rc.returncode == 0, rc.stderr.strip()[-200:])
    check("search button in every page", 'id="searchbtn"' in (html / "README.html").read_text(encoding="utf-8"))

    # a large book ships its sidebar once instead of inside every page
    shared = tmp / "out-shared-nav"
    r = run_build(src, shared, env={"BOOKIFY_INLINE_NAV_LIMIT": "1"})
    readme = (shared / "html" / "README.html").read_text(encoding="utf-8") if r.returncode == 0 else ""
    nav = (shared / "html" / "nav.js").read_text(encoding="utf-8") if (shared / "html" / "nav.js").is_file() else ""
    check("shared sidebar written once",
          nav.startswith("window.BOOKIFY_NAV=") and "guide/page.html" in nav, (r.stderr or "")[-200:])
    check("pages load the shared sidebar instead of carrying it",
          'data-shared="1"' in readme and 'src="nav.js"' in readme and ">Guide Page</a>" not in readme)


def sidebar(page: Path) -> list[str]:
    """The sidebar in reading order: page links as their text, sections as §label."""
    text = page.read_text(encoding="utf-8")
    tree = text.split('<div class="tree">', 1)[1].split("</nav>", 1)[0]
    return [("§" + m.group(1)) if m.group(1) is not None else m.group(2)
            for m in re.finditer(r'<summary>([^<]*)</summary>|<a href="[^"]*"(?: class="active")?>([^<]*)</a>',
                                 tree)]


def write_files(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")


def test_navigation(tmp: Path) -> None:
    print("navigation")
    cases = []

    # MkDocs: nav in mkdocs.yml one level above docs_dir, with a tag PyYAML doesn't know
    write_files(tmp / "nav-mkdocs", {
        "mkdocs.yml": ("site_name: T\ndocs_dir: docs\nmarkdown_extensions:\n"
                       "  - pymdownx.emoji:\n"
                       "      emoji_index: !!python/name:material.extensions.emoji.twemoji\n"
                       "nav:\n  - Home: index.md\n  - Guide:\n    - guide/b.md\n"
                       "    - Custom A: guide/a.md\n  - About: about.md\n"
                       "  - External: https://example.com\n"),
        "docs/index.md": "# Home\n", "docs/guide/a.md": "# A\n", "docs/guide/b.md": "# B\n",
        "docs/about.md": "# About\n", "docs/zextra.md": "# Extra\n"})
    cases.append(("mkdocs nav", tmp / "nav-mkdocs" / "docs", "index.html", "mkdocs.yml nav",
                  ["Home", "§Guide", "B", "Custom A", "About", "Extra"]))

    # mdBook: SUMMARY.md with a prefix chapter, part title, nesting and a draft chapter
    write_files(tmp / "nav-mdbook", {
        "src/SUMMARY.md": ("# Summary\n\n[Introduction](README.md)\n\n# User guide\n\n"
                           "- [Install](guide/install.md)\n- [CLI](cli/README.md)\n"
                           "    - [build](cli/build.md)\n    - [Draft]()\n"),
        "src/README.md": "# Intro\n", "src/guide/install.md": "# Installing\n",
        "src/cli/README.md": "# Command line\n", "src/cli/build.md": "# Build\n"})
    cases.append(("mdbook summary", tmp / "nav-mdbook" / "src", "README.html", "SUMMARY.md",
                  ["Introduction", "§User guide", "Install", "§CLI", "Command line", "build"]))

    # Sphinx: captions, a borrowed heading, a titled entry, nesting, glob, and MyST
    write_files(tmp / "nav-sphinx", {
        "conf.py": 'project = "P"\nroot_doc = "index"\n',
        "index.rst": ("Project\n=======\n\n.. toctree::\n   :caption: Guide\n\n   usage\n"
                      "   Setup <install>\n   notes\n\nAPI\n---\n\n.. toctree::\n   :glob:\n\n   api/*\n"),
        "usage.rst": "Usage\n=====\n\nText.\n\n.. toctree::\n\n   advanced\n",
        "advanced.rst": "Advanced\n========\n", "install.rst": "Install\n=======\n",
        "notes.md": "# Notes\n\n```{toctree}\n:caption: More\ndeep\n```\n",
        "deep.md": "# Deep\n", "api/b.rst": "B\n=\n", "api/a.rst": "A\n=\n"})
    cases.append(("sphinx toctree", tmp / "nav-sphinx", "index.html", "Sphinx toctree",
                  ["Project", "§Guide", "§Usage", "Usage", "Advanced", "Setup", "§Notes", "Notes",
                   "§More", "Deep", "§API", "A", "B"]))

    # Jekyll: _data/docs_nav.yml pointing at permalinks, with templates that aren't pages
    write_files(tmp / "nav-jekyll", {
        "_config.yml": "title: Site\n",
        "_data/docs_nav.yml": ("- title: Getting Started\n  docs:\n    - link: /docs/\n"
                               "    - link: /docs/installation/\n- title: Build\n  docs:\n"
                               "    - link: /docs/usage/\n"),
        "_docs/index.md": "---\ntitle: Home\npermalink: /docs/\n---\nWelcome.\n",
        "_docs/installation.md": '---\ntitle: Installation\npermalink: "/docs/installation/"\n---\nSteps.\n',
        "_docs/usage.md": "# Usage\n",
        "_layouts/default.html": "<html><body>{{ content }}</body></html>\n",
        "_includes/note.html": "<div>note</div>\n"})
    cases.append(("jekyll data nav", tmp / "nav-jekyll", "_docs/index.html", "_data/docs_nav.yml",
                  ["§Getting Started", "Home", "Installation", "§Build", "Usage"]))

    # no nav config: Docusaurus _category_.json, Hugo _index.md, number prefixes, natural sort
    write_files(tmp / "nav-folders", {
        "index.md": "# Welcome\n",
        "guide/_category_.json": '{"label": "The Guide", "position": 1}',
        "guide/page10.md": "# Page 10\n", "guide/page2.md": "# Page 2\n",
        "api/_index.md": "---\ntitle: API Reference\nweight: 2\n---\nAll of it.\n",
        "3-early/y.md": "# Y\n", "10-late/x.md": "# X\n"})
    cases.append(("folder metadata", tmp / "nav-folders", "index.html", "folders",
                  ["Welcome", "§The Guide", "Page 2", "Page 10", "§API Reference", "API Reference",
                   "§Early", "Y", "§Late", "X"]))

    # Docusaurus: a typed sidebars.ts with categories, doc links, ids, autogenerated
    # folders, external links and a spread the parser can't know
    write_files(tmp / "nav-docusaurus", {
        "docusaurus.config.js": "module.exports = {title: 'T'};\n",
        "sidebars.ts": (
            "import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';\n"
            "// a comment { with braces ]\nconst extraItems = [];\n"
            "const sidebars: SidebarsConfig = {\n  docs: [\n    'intro',\n    {\n"
            "      type: 'category',\n      label: 'Guides',\n"
            "      link: {type: 'doc', id: 'guides/overview'},\n"
            "      items: ['guides/setup', {type: 'doc', id: 'guides/custom-id', label: 'Renamed'}],\n"
            "    },\n    {type: 'category', label: 'Reference', items: [{type: 'autogenerated', dirName: 'reference'}]},\n"
            "    {type: 'link', label: 'External', href: 'https://example.com'},\n"
            "    ...extraItems,\n  ],\n};\nexport default sidebars;\n"),
        "docs/intro.md": "# Intro\n", "docs/guides/overview.md": "# Overview\n",
        "docs/guides/setup.mdx": "# Setup\n",
        "docs/guides/zz-other.md": "---\nid: custom-id\n---\n# Other Page\n",
        "docs/reference/b.md": "# B\n", "docs/reference/a.md": "# A\n", "docs/stray.md": "# Stray\n"})
    cases.append(("docusaurus sidebars", tmp / "nav-docusaurus" / "docs", "intro.html", "sidebars.ts",
                  ["Intro", "§Guides", "Overview", "Setup", "Renamed", "§Reference", "A", "B", "Stray"]))

    # Starlight: slugs, autogenerate, translations to ignore, and a locale folder to drop
    write_files(tmp / "nav-starlight", {
        "astro.config.mjs": (
            "import { defineConfig } from 'astro/config';\nimport starlight from '@astrojs/starlight';\n"
            "export const locales = { root: { label: 'English', lang: 'en' }, de: { label: 'Deutsch' } };\n"
            "export default defineConfig({\n  integrations: [\n    starlight({\n      title: 'Docs',\n"
            "      locales,\n      sidebar: [\n"
            "        { label: 'Start Here', translations: { de: 'Hier' }, items: ['getting-started', "
            "{ label: 'Setup Guide', slug: 'manual-setup' }] },\n"
            "        { label: 'Guides', items: [{ autogenerate: { directory: 'guides' } }] },\n"
            "      ],\n    }),\n  ],\n});\n"),
        "src/content/docs/index.mdx": "---\ntitle: Welcome\n---\nHi.\n",
        "src/content/docs/getting-started.mdx": "---\ntitle: Getting Started\n---\nGo.\n",
        "src/content/docs/manual-setup.mdx": "---\ntitle: Manual Setup\n---\nSet.\n",
        "src/content/docs/guides/b.md": "---\ntitle: B guide\n---\nB.\n",
        "src/content/docs/guides/a.md": "---\ntitle: A guide\n---\nA.\n",
        "src/content/docs/de/getting-started.mdx": "---\ntitle: Erste Schritte\n---\nLos.\n"})
    cases.append(("starlight sidebar", tmp / "nav-starlight" / "src" / "content" / "docs", "index.html",
                  "astro.config.mjs sidebar",
                  ["Welcome", "§Start Here", "Getting Started", "Setup Guide", "§Guides", "A guide", "B guide"]))

    # VitePress: per-path sidebars, a helper function, base prefixes, rewrites and locales
    write_files(tmp / "nav-vitepress", {
        ".vitepress/config.mts": (
            "import { defineConfig, type DefaultTheme } from 'vitepress'\n"
            "export default defineConfig({\n  rewrites: { 'en/:rest*': ':rest*' },\n"
            "  locales: { root: { label: 'English', lang: 'en-US' }, zh: { label: 'Chinese', lang: 'zh-Hans' } },\n"
            "  themeConfig: {\n    sidebar: {\n"
            "      '/guide/': { base: '/guide/', items: sidebarGuide() },\n"
            "      '/reference/': [ { text: 'Reference', items: [ { text: 'CLI', link: '/reference/cli' } ] } ]\n"
            "    }\n  }\n})\n\n"
            "function sidebarGuide(): DefaultTheme.SidebarItem[] {\n  return [\n"
            "    { text: 'Introduction', collapsed: false, items: [\n"
            "      { text: 'What is it?', link: 'what-is-it' },\n"
            "      { text: 'Getting Started', link: 'getting-started' }\n    ] },\n"
            "    { text: 'Theme', base: '/guide/theme-', items: [ { text: 'Colors', link: 'colors' } ] }\n"
            "  ]\n}\n"),
        "en/index.md": "# Home\n", "en/guide/what-is-it.md": "# What\n",
        "en/guide/getting-started.md": "# Start\n", "en/guide/theme-colors.md": "# Colours\n",
        "en/reference/cli.md": "# CLI Reference\n", "zh/index.md": "# Shouye\n"})
    cases.append(("vitepress sidebar", tmp / "nav-vitepress", "en/index.html", ".vitepress/config.mts sidebar",
                  ["Home", "§Introduction", "What is it?", "Getting Started", "§Theme", "Colors",
                   "§Reference", "CLI"]))

    for name, src, page, source, expected in cases:
        out = tmp / ("out-" + src.parent.name + "-" + src.name)
        r = run_build(src, out)
        if r.returncode != 0:
            check(f"{name}: build succeeds", False, (r.stderr or r.stdout).strip()[-300:])
            continue
        check(f"{name}: banner names the source", f"Navigation:    {source}" in r.stdout,
              r.stdout.strip()[-200:])
        got = sidebar(out / "html" / page)
        check(f"{name}: sidebar order", got == expected, f"got {got}")

    check("starlight: translations are left out",
          not (tmp / "out-content-docs" / "html" / "de").exists())
    check("vitepress: translations are left out",
          not (tmp / "out-nav-vitepress-nav-vitepress" / "html" / "zh").exists())
    check("mdbook: SUMMARY.md is not a chapter",
          not (tmp / "out-nav-mdbook-src" / "html" / "SUMMARY.html").exists())
    check("jekyll: layouts and includes are not pages",
          not (tmp / "out-nav-jekyll-nav-jekyll" / "html" / "_layouts").exists()
          and not (tmp / "out-nav-jekyll-nav-jekyll" / "html" / "_includes").exists())
    summary = (tmp / "out-nav-mkdocs-docs" / "markdown" / "SUMMARY.md").read_text(encoding="utf-8")
    check("markdown SUMMARY shows sections", "- **Guide**" in summary and "  - [B](" in summary,
          summary[:300])


def test_wikilinks(tmp: Path) -> None:
    print("wikilinks")
    src, out = tmp / "vault", tmp / "out-vault"
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000d4944415478da63f8cfc0f00f0005000100ffab8e36"
        "890000000049454e44ae426082")
    write_files(src, {
        ".obsidian/app.json": "{}",
        "Home.md": (
            "# Home\n\nSee [[Target Note]], [[Target Note|an alias]], "
            "[[Folder/Deep Note#Section Two]], [[#Local Heading]] and [[Missing Page]].\n\n"
            "Inline code `[[not a link]]` stays.\n\n"
            "| Where | Link |\n|---|---|\n| table | [[Target Note\\|piped alias]] |\n\n"
            "![[diagram.png|300]]\n\n"
            "![[Target Note#Part A]]\n\n"
            "> [!tip]- Folded tip\n> MARKER_FOLD with [[Target Note]]\n\n"
            "> [!note] See [[Folder/Deep Note|the deep note]] first\n> Body.\n\n"
            "Formatted alias: [[Target Note|`code alias`]].\n\n"
            "<details>\n<summary>More</summary>\n\n**MARKER_DETAILS**\n\n</details>\n\n"
            "## Local Heading\n\nText.\n"),
        "Target Note.md": "# Target Note\n\nIntro.\n\n## Part A\n\nMARKER_PART_A\n\n## Part B\n\nMARKER_PART_B\n",
        "Folder/Deep Note.md": "# Deep Note\n\n## Section Two\n\nDeep.\n",
        "Folder/Other.md": "# Other\n\nBack to [[Home]].\n"})
    (src / "attachments").mkdir(parents=True, exist_ok=True)
    (src / "attachments" / "diagram.png").write_bytes(png)
    r = run_build(src, out)
    if r.returncode != 0:
        check("vault build succeeds", False, (r.stderr or r.stdout).strip()[-300:])
        return
    home = (out / "html" / "Home.html").read_text(encoding="utf-8").split("<main>", 1)[1].split("</main>", 1)[0]
    check("wikilink resolves by name", 'href="Target Note.html"' in home, home[:400])
    check("wikilink alias is the link text", ">an alias</a>" in home)
    check("escaped alias pipe inside a table",
          ">piped alias</a>" in home and "<td>" in home and "Target Note\\" not in home)
    check("wikilink with folder and heading",
          'href="Folder/Deep Note.html#section-two"' in home)
    check("same-page heading link", 'href="#local-heading"' in home)
    check("unresolved wikilink is marked missing",
          'class="wikilink missing"' in home and "Missing Page" in home)
    check("wikilinks in inline code are left alone", "<code>[[not a link]]</code>" in home)
    check("image embed with width",
          'src="attachments/diagram.png"' in home and 'width="300"' in home)
    check("embedded image copied", (out / "html" / "attachments" / "diagram.png").is_file())
    embed = home.split('class="embed"', 1)[-1] if 'class="embed"' in home else ""
    check("note section is transcluded",
          "MARKER_PART_A" in embed and "MARKER_PART_B" not in home, embed[:300])
    check("foldable callout becomes details",
          '<details class="admonition tip"' in home and "Folded tip</summary>" in home
          and "MARKER_FOLD" in home)
    check("wikilink inside a callout resolves",
          home.count('href="Target Note.html"') >= 2)
    title = home.split('class="admonition-title">', 1)[-1].split("</p>", 1)[0]
    check("wikilink in a callout title becomes a link",
          'href="Folder/Deep Note.html"' in title and ">the deep note</a>" in title, title)
    check("inline code inside a wikilink alias",
          "<code>code alias</code></a>" in home)
    check("markdown inside <details> renders", "<strong>MARKER_DETAILS</strong>" in home)
    other = (out / "html" / "Folder" / "Other.html").read_text(encoding="utf-8")
    check("wikilink from a subfolder climbs out", 'href="../Home.html"' in other)
    broken = broken_links(out / "html")
    check("every vault link resolves", not broken, "; ".join(broken[:4]))
    md = (out / "markdown" / "Home.md").read_text(encoding="utf-8")
    check("markdown book gets plain links", "[an alias](<Target Note.md>)" in md, md[:300])


def test_templates(tmp: Path) -> None:
    print("templates")

    def main_of(path: Path) -> str:
        return path.read_text(encoding="utf-8").split("<main>", 1)[1].split("</main>", 1)[0]

    # Hugo shortcodes
    site = tmp / "hugo-site"
    write_files(site, {
        "hugo.toml": "title = 'Site'\n",
        "content/_index.md": "---\ntitle: Home\n---\nWelcome to {{< param title >}}.\n\n{{< new-in 0.140.0 />}}\n",
        "content/docs/guide.md": (
            "---\ntitle: Guide\n---\n"
            "{{< hint warning >}}\nMARKER_HINT with **bold**\n{{< /hint >}}\n\n"
            '{{< details title="More info" >}}\nMARKER_DETAILS\n{{< /details >}}\n\n'
            '{{< tabs >}}\n{{< tab "macOS" >}}\nMARKER_MAC\n{{< /tab >}}\n{{< /tabs >}}\n\n'
            '{{< highlight go >}}\nfunc main() { fmt.Println("{{ not a template }}") }\n{{< /highlight >}}\n\n'
            "{{< code-toggle file=hugo >}}\n[params]\nx = 1\n{{< /code-toggle >}}\n\n"
            "{{< code-toggle config=imaging />}}\n\nMARKER_AFTER_SELF_CLOSING\n\n"
            '{{< img\n  src="shot.png"\n  alt="A screenshot"\n>}}\n\n'
            '{{% render-table-of-pages-in-section\n  path="/functions"\n%}}\n\n'
            "1. A step with config:\n\n    {{< code-toggle file=hugo >}}\n    baseURL = 'x'\n    {{< /code-toggle >}}\n\n"
            'See [the home]({{< ref "/_index.md" >}}) and [setup]({{< relref "setup" >}}).\n\n'
            '{{% include "/_common/snippet.md" %}}\n\n'
            "{{< youtube 0RKpf3rK57I >}}\n\n"
            '{{< mystery-shortcode foo="bar" />}}\n\n'
            "Escaped: `{{</* hint */>}}` and {{</* note */>}} literal.\n"),
        "content/docs/setup.md": "# Setup\n",
        "content/_common/snippet.md": "MARKER_INCLUDED from a shared file.\n"})
    out = tmp / "out-hugo-site"
    r = run_build(site / "content", out)
    if r.returncode != 0:
        check("hugo build succeeds", False, (r.stderr or r.stdout).strip()[-300:])
    else:
        guide = main_of(out / "html" / "docs" / "guide.html")
        check("hint shortcode becomes a callout",
              'class="admonition warning"' in guide and "MARKER_HINT" in guide
              and "<strong>bold</strong>" in guide, guide[:300])
        check("details shortcode becomes details",
              "<details" in guide and "More info</summary>" in guide and "MARKER_DETAILS" in guide)
        check("tab shortcode becomes a labelled section",
              'class="tab-label">macOS' in guide and "MARKER_MAC" in guide)
        check("highlight shortcode becomes highlighted code",
              'class="highlight"' in guide and "not a template" in guide)
        check("code-toggle content stays code", "[params]" in guide.split("<pre", 2)[-1])
        check("a self-closing code shortcode doesn't swallow the page",
              "<p>MARKER_AFTER_SELF_CLOSING</p>" in guide, guide[-600:])
        check("a multi-line shortcode is read whole",
              'alt="A screenshot"' in guide and "render-table" not in guide, guide[:500])
        check("a code shortcode inside a list item stays in the item",
              re.search(r"<li>.*?baseURL.*?</li>", guide, re.S) is not None)
        check("ref and relref become links",
              'href="../_index.html"' in guide and 'href="setup.html"' in guide)
        check("include shortcode inlines the file", "MARKER_INCLUDED" in guide)
        check("youtube shortcode becomes a link",
              'href="https://www.youtube.com/watch?v=0RKpf3rK57I"' in guide)
        check("unknown shortcodes are removed", "mystery" not in guide and "{{&lt;" not in
              guide.replace("{{&lt; hint &gt;}}", "").replace("{{&lt; note &gt;}}", ""))
        check("escaped shortcodes stay literal",
              "{{&lt; hint &gt;}}" in guide and "{{&lt; note &gt;}}" in guide, guide[-400:])
        home = main_of(out / "html" / "_index.html")
        check("param shortcode reads front matter", "Welcome to Home." in home, home[:200])
        check("new-in shortcode becomes a badge", 'class="badge">New in 0.140.0' in home)
        broken = broken_links(out / "html")
        check("every hugo link resolves", not broken, "; ".join(broken[:4]))

    # Jekyll: Liquid expressions, links, includes, comments, raw and highlight blocks
    write_files(tmp / "jekyll-site", {
        "_config.yml": "title: My Site\nbaseurl: /base\n",
        "_includes/note.md": "MARKER_INCLUDE_MD\n",
        "_includes/badge.html": '<span class="site-badge">{{ site.title }}</span>\n',
        "_includes/loop.html": "{% for x in site.data %}MARKER_LOOP{{ x }}{% endfor %}\n",
        "_docs/conduct.md": "---\ntitle: Conduct\npermalink: /docs/conduct/\n---\nBe nice.\n",
        "_posts/2016-07-26-launch.md": "---\ntitle: Launch\n---\nLaunched.\n",
        "about.html": ("---\ntitle: About Us\npermalink: /about/\n---\n<h1>{{ page.title }}</h1>\n"
                       "<p><a href=\"{{ 'docs/conduct/' | relative_url }}\">Conduct</a></p>\n"),
        "index.md": (
            "---\ntitle: Home Page\n---\n# {{ page.title }} for {{ site.title }}\n\n"
            "Read the [code of conduct]({{ '/docs/conduct/' | relative_url }}), "
            "[the launch]({% post_url 2016-07-26-launch %}) and "
            "[conduct again]({% link _docs/conduct.md %}).\n\n"
            "{% include note.md %}\n\n{% include badge.html %}\n\n{% include loop.html %}\n\n"
            "{% comment %}\nMARKER_COMMENT_HIDDEN\n{% endcomment %}\n\n"
            "{% if site.title %}MARKER_IF_BODY{% endif %}\n\n"
            '{% highlight ruby %}\ndef hello; puts "{% raw %}{{ not_liquid }}{% endraw %}"; end\n{% endhighlight %}\n\n'
            "```liquid\n{% raw %}\n{{ page.title }} stays literal\n{% endraw %}\n```\n")})
    out = tmp / "out-jekyll-site"
    r = run_build(tmp / "jekyll-site", out)
    if r.returncode != 0:
        check("jekyll build succeeds", False, (r.stderr or r.stdout).strip()[-300:])
    else:
        home = main_of(out / "html" / "index.html")
        body = home.split('<nav class="pager">', 1)[0]   # the pager links pages too
        check("liquid page and site variables", ">Home Page for My Site</h1>" in home, home[:300])
        check("relative_url and link tags resolve to pages",
              body.count('href="_docs/conduct.html"') == 2, body[:600])
        check("post_url resolves", 'href="_posts/2016-07-26-launch.html"' in home)
        check("markdown include inlined", "MARKER_INCLUDE_MD" in home)
        check("static html include inlined with variables", 'class="site-badge">My Site' in home)
        check("looping template fragment skipped", "MARKER_LOOP" not in home)
        check("liquid comment hidden", "MARKER_COMMENT_HIDDEN" not in home)
        check("control tags vanish, their content stays", "MARKER_IF_BODY" in home and "{%" not in home)
        check("highlight tag becomes code with raw content kept",
              'class="highlight"' in home and "{{ not_liquid }}" in home)
        # highlighting splits the text into spans, so compare it with the tags removed
        check("raw block inside a fence stays literal",
              "{{ page.title }} stays literal" in re.sub(r"<[^>]+>", "", home))
        about = main_of(out / "html" / "about.html")
        check("jekyll html page: front matter and liquid",
              "<h1>About Us</h1>" in about and 'href="_docs/conduct.html"' in about
              and "permalink" not in about, about[:300])
        broken = broken_links(out / "html")
        check("every jekyll link resolves", not broken, "; ".join(broken[:4]))

    # MkDocs: mkdocs-macros variables and includes, pymdownx.snippets
    write_files(tmp / "macros-site", {
        "mkdocs.yml": ("site_name: Macro Docs\nplugins:\n  - search\n  - macros:\n"
                       "      include_dir: snippets_inc\nmarkdown_extensions:\n"
                       "  - pymdownx.snippets:\n      base_path: [shared]\n"
                       "extra:\n  version: 2.4.1\n  product:\n    name: Widget\n"),
        "docs/index.md": (
            "# {{ config.site_name }}\n\nVersion {{ version }} of {{ product.name }}.\n\n"
            '{% include "partial.md" %}\n\n--8<-- "common.md"\n\n;--8<-- "escaped.md"\n\n'
            "--8<--\nmulti1.md\nmulti2.md\n--8<--\n\n{% raw %}{{ kept }}{% endraw %}\n"),
        "snippets_inc/partial.md": "MARKER_MACRO_INCLUDE\n",
        "shared/common.md": "MARKER_SNIPPET\n",
        "shared/multi1.md": "MARKER_MULTI1\n", "shared/multi2.md": "MARKER_MULTI2\n"})
    out = tmp / "out-macros-site"
    r = run_build(tmp / "macros-site" / "docs", out)
    if r.returncode != 0:
        check("macros build succeeds", False, (r.stderr or r.stdout).strip()[-300:])
    else:
        home = main_of(out / "html" / "index.html")
        check("macros: config and extra variables",
              ">Macro Docs</h1>" in home and "Version 2.4.1 of Widget." in home, home[:300])
        check("macros: include from include_dir", "MARKER_MACRO_INCLUDE" in home)
        check("snippets: single and block forms",
              "MARKER_SNIPPET" in home and "MARKER_MULTI1" in home and "MARKER_MULTI2" in home)
        check("snippets: escaped marker stays literal", "--8&lt;--" in home)
        check("macros: raw block kept", "{{ kept }}" in home)

    # MDX components (Docusaurus and Starlight)
    write_files(tmp / "mdx-site", {
        "page.mdx": (
            "---\ntitle: Components\n---\n"
            "import Tabs from '@theme/Tabs';\nimport TabItem from '@theme/TabItem';\n"
            "import { Aside, Steps, LinkCard } from '@astrojs/starlight/components';\n\n"
            "{/* a comment that must vanish */}\n\n"
            "<Tabs>\n  <TabItem\n    value=\"npm\"\n    label=\"npm\">\n\n```bash\nnpm install\n```\n\n  </TabItem>\n</Tabs>\n\n"
            '<Aside type="caution" title="Careful now">\n\tMARKER_ASIDE with [a link](other.md)\n</Aside>\n\n'
            "<Steps>\n\n1. MARKER_STEP\n\n</Steps>\n\n"
            '<LinkCard title="Read more" href="https://example.com" />\n\n'
            "<BrowserWindow>\n\nMARKER_WINDOW\n\n</BrowserWindow>\n\n"
            "<FancyWidget foo={1} />\n\n"
            '```js title="src/example.js" {2} showLineNumbers\nconst MARKER_TITLED = 1;\n```\n\n'
            '<Aside type="tip">MARKER_ONE_LINE</Aside>\n\n'
            'Status: <Badge text="New" variant="tip" /> and <URLPath /> here.\n\n'
            "<SidebarPreview\n  config={[\n    { label: 'A', link: () => '/a' },\n  ]}\n/>\n\n"
            "MARKER_AFTER_PREVIEW\n"),
        "other.md": "# Other\n"})
    out = tmp / "out-mdx-site"
    r = run_build(tmp / "mdx-site", out)
    if r.returncode != 0:
        check("mdx build succeeds", False, (r.stderr or r.stdout).strip()[-300:])
        return
    page = main_of(out / "html" / "page.html")
    check("mdx comments vanish", "comment that must vanish" not in page)
    check("TabItem with a multi-line tag becomes a label",
          'class="tab-label">npm' in page and 'class="highlight"' in page, page[:400])
    check("Aside becomes a callout with its title",
          'class="admonition caution"' in page and "Careful now" in page
          and "MARKER_ASIDE" in page and 'href="other.html"' in page)
    check("tab-indented component content is not a code block",
          "MARKER_ASIDE" not in "".join(re.findall(r"<pre.*?</pre>", page, re.S)))
    check("Steps content renders as a list", re.search(r"<li>\s*MARKER_STEP", page) is not None)
    check("LinkCard becomes a link",
          'href="https://example.com"' in page and ">Read more</a>" in page)
    check("unknown wrapper components are unwrapped", "<p>MARKER_WINDOW</p>" in page)
    check("no JSX tags leak into the page",
          not re.search(r"</?(Tabs|TabItem|Aside|Steps|BrowserWindow|FancyWidget|URLPath)\b", page))
    check("code block title from the info string",
          'class="code-title">src/example.js' in page
          and re.search(r'code-title.*?class="highlight".*?MARKER_TITLED', page, re.S) is not None)
    check("one-line component becomes a callout",
          re.search(r'class="admonition tip".*?MARKER_ONE_LINE', page, re.S) is not None)
    check("inline Badge keeps its text", 'class="badge">New</span>' in page)
    check("a multi-line live demo with arrow functions is dropped",
          "SidebarPreview" not in page and "<p>MARKER_AFTER_PREVIEW</p>" in page, page[-400:])


def write_docs_systems_fixture(root: Path) -> None:
    """One source per docs-system dialect bookify is expected to read as-is."""
    (root / "guide").mkdir(parents=True, exist_ok=True)
    (root / "_build" / "html").mkdir(parents=True, exist_ok=True)
    files = {
        "index.md": "# Home\n\nSee [setup](guide/setup) and [the API](api.rst).\n",
        # Docusaurus: title and position in front matter, no H1 in the body
        "guide/zeta.md": '---\ntitle: "Getting Started"\nsidebar_position: 1\n---\n\nMARKER_FM\n',
        "guide/setup.mdx": (
            "---\ntitle: Setup\nsidebar_position: 2\n---\n"
            "import Tabs from '@theme/Tabs';\n"
            "export const meta = {\n  draft: false,\n};\n\n"
            "MARKER_MDX\n\n"
            ":::tip[Pro tip]\nMARKER_TIP\n\n```bash\nnpm run build\n```\n:::\n\n"
            "> [!WARNING]\n> MARKER_ALERT\n\n"
            "```md\n:::note\nMARKER_FENCED\n:::\n```\n"),
        "guide/alpha.md": "# Alpha\n\nMARKER_ALPHA, no front matter.\n",
        # Hugo: TOML front matter
        "hugo.md": '+++\ntitle = "Hugo Page"\nweight = 5\n+++\n\nMARKER_TOML\n',
        # Sphinx: a standard directive plus one plain docutils doesn't know
        "api.rst": ("API Reference\n=============\n\nMARKER_RST with *emphasis*.\n\n"
                    ".. note::\n\n   MARKER_RST_NOTE\n\n"
                    ".. toctree::\n   :maxdepth: 2\n\n   guide/setup\n"),
        # a generator's build output must not become a second copy of the book
        "_build/html/stale.md": "# Stale\n\nMARKER_BUILD_OUTPUT\n",
    }
    for rel, text in files.items():
        (root / rel).write_text(text, encoding="utf-8")


def test_docs_systems(tmp: Path) -> None:
    print("docs systems")
    src, out = tmp / "docsys", tmp / "out-docsys"
    write_docs_systems_fixture(src)
    r = run_build(src, out)
    ok = r.returncode == 0
    check("docs-system build succeeds", ok, (r.stderr or r.stdout).strip()[-300:])
    html, md = out / "html", out / "markdown"
    if not ok or not (html / "guide" / "setup.html").exists():
        check("docs-system book produced", False, "skipping the checks that depend on it")
        return

    def main_of(rel: str) -> str:
        text = (html / rel).read_text(encoding="utf-8")
        return text.split("<main>", 1)[1].split("</main>", 1)[0]

    zeta_page = (html / "guide" / "zeta.html").read_text(encoding="utf-8")
    zeta = main_of("guide/zeta.html")
    check("front matter is not rendered", "sidebar_position" not in zeta and "MARKER_FM" in zeta)
    check("front-matter title becomes the heading", ">Getting Started</h1>" in zeta, zeta[:200])

    tree = zeta_page.split('<div class="tree">', 1)[1].split("</nav>", 1)[0]
    at = [tree.find(t) for t in (">Getting Started<", ">Setup<", ">Alpha<")]
    check("sidebar follows front-matter order", -1 not in at and at == sorted(at), str(at))
    check("rst title used in the sidebar", ">API Reference<" in tree)

    setup = main_of("guide/setup.html")
    check("mdx page rendered", "MARKER_MDX" in setup)
    check("mdx import/export dropped",
          "import Tabs" not in setup and "export const" not in setup and "draft" not in setup)
    check(":::tip becomes an admonition",
          'class="admonition tip"' in setup and "Pro tip" in setup and "MARKER_TIP" in setup)
    tip = setup.split('class="admonition tip"', 1)[-1].split("MARKER_ALERT", 1)[0]
    check("fenced code inside a callout still highlights",
          'class="highlight"' in tip and "```" not in setup)
    check("GitHub alert becomes an admonition",
          'class="admonition warning"' in setup and "MARKER_ALERT" in setup
          and "[!WARNING]" not in setup)
    check("callout syntax inside a code fence is left alone",
          "MARKER_FENCED" in setup and setup.count('class="admonition ') == 2)

    check("extensionless link resolves to the page",
          'href="guide/setup.html"' in main_of("index.html"))
    hugo = main_of("hugo.html")
    check("toml front matter read", ">Hugo Page</h1>" in hugo and "weight" not in hugo)

    api = main_of("api.html")
    check("rst rendered as html", "<em>emphasis</em>" in api and "plaintext" not in api)
    check("rst directives render", 'class="admonition note"' in api and "MARKER_RST_NOTE" in api)
    check("unknown sphinx directives stay silent",
          "toctree" not in api and "System Message" not in api)

    everything = "".join(p.read_text(encoding="utf-8") for p in html.rglob("*.html"))
    check("generator build output skipped", "MARKER_BUILD_OUTPUT" not in everything)
    broken = broken_links(html)
    check("every docs-system link resolves", not broken, "; ".join(broken[:4]))

    setup_md = (md / "guide" / "setup.md").read_text(encoding="utf-8")
    check("md book drops front matter and mdx imports",
          setup_md.startswith("# Setup") and "sidebar_position" not in setup_md
          and "import Tabs" not in setup_md, setup_md[:200])


# ---------------------------------------------------------------- server suite

def test_annotations(tmp: Path) -> None:
    print("annotations api")
    src, out = tmp / "src", tmp / "out-serve"
    with Serve(src, out) as s:
        code, body = get(s.url + "/__annotations__")
        db = json.loads(body)
        check("GET returns a store", code == 200 and isinstance(db.get("pages"), dict))

        post(s.url + "/__annotations__", {"pages": {}})
        one = {"id": "a1", "exact": "MARKER_MD", "occ": 0, "note": "one", "ts": 1}
        two = {"id": "b1", "exact": "Back to", "occ": 0, "note": "two", "ts": 2}
        post(s.url + "/__annotations__", {"merge": {"README.html": [one]}})
        code, body = post(s.url + "/__annotations__",
                          {"merge": {"guide/page.html": [two]}})
        db = json.loads(body)
        check("merge keeps other pages",
              code == 200
              and len(db["pages"].get("README.html", [])) == 1
              and len(db["pages"].get("guide/page.html", [])) == 1,
              json.dumps(db)[:200])

        code, _ = post(s.url + "/__annotations__", b"not json")
        check("garbage body rejected", code == 400)
        db = json.loads(get(s.url + "/__annotations__")[1])
        check("store survives a rejected write", len(db["pages"]) == 2)

        code, body = post(s.url + "/__annotations__",
                          {"merge": {"README.html": []}})
        db = json.loads(body)
        check("empty list drops the page key", "README.html" not in db["pages"])

    # notes survive a rebuild of the same output dir
    (out / "annotations.json").write_text(
        json.dumps({"version": 1, "pages": {"README.html": [
            {"id": "keep", "exact": "MARKER_MD", "occ": 0, "note": "kept", "ts": 1}]}}),
        encoding="utf-8")
    run_build(src, out)
    kept = json.loads((out / "annotations.json").read_text())
    check("annotations survive a rebuild",
          kept["pages"]["README.html"][0]["id"] == "keep")
    check("no temp file left behind", not list(out.glob("*.tmp")))


def test_ask(tmp: Path) -> None:
    print("ask ai api")
    src = tmp / "src"

    # nothing installed: probe says so, and never claims to be ready
    with Serve(src, tmp / "out-noai", "--ask-provider", "ollama",
               env={"BOOKIFY_OLLAMA": "http://127.0.0.1:9"}) as s:
        probe = json.loads(get(s.url + "/__ask__")[1])
        check("probe reports unavailable", probe["ok"] is False and not probe["providers"])
        check("probe explains why", "No local assistant" in probe["reason"])

    # --no-ask refuses outright
    with Serve(src, tmp / "out-off", "--no-ask") as s:
        probe = json.loads(get(s.url + "/__ask__")[1])
        check("--no-ask disables", probe["ok"] is False and "--no-ask" in probe["reason"])
        code, _ = post(s.url + "/__ask__", {"token": probe["token"]})
        check("--no-ask refuses POST", code == 403)

    # a reachable Ollama: detection, token gate, streaming, error passthrough
    with FakeOllama() as fake, Serve(
            src, tmp / "out-ai", "--ask-provider", "ollama",
            env={"BOOKIFY_OLLAMA": fake.url}) as s:
        probe = json.loads(get(s.url + "/__ask__")[1])
        check("provider detected",
              probe["ok"] and probe["default"] == "ollama"
              and probe["providers"][0]["detail"] == "llama3.2:3b",
              json.dumps(probe)[:200])
        check("probe issues a token", len(probe.get("token") or "") > 10)

        code, _ = post(s.url + "/__ask__",
                       {"provider": "ollama", "messages": [{"role": "user", "content": "hi"}]})
        check("POST without a token is refused", code == 403)

        code, body = post(s.url + "/__ask__", {
            "token": probe["token"], "provider": "ollama",
            "quote": "This loop is idempotent.",
            "context": "The control plane reconciles state. This loop is idempotent.",
            "messages": [{"role": "user", "content": "what does idempotent mean?"}]})
        lines = [json.loads(x) for x in body.decode().splitlines() if x.strip()]
        text = "".join(o.get("t", "") for o in lines)
        check("answer streams as ndjson", code == 200 and len(lines) > 3)
        check("stream ends with done", any(o.get("done") for o in lines))
        check("answer text reassembles", "idempotent" in text, text[:120])

        code, body = post(s.url + "/__ask__", {
            "token": probe["token"], "provider": "ollama", "quote": "q",
            "messages": [{"role": "user", "content": "TRIGGER_ERROR"}]})
        lines = [json.loads(x) for x in body.decode().splitlines() if x.strip()]
        check("provider error is surfaced",
              any("exploded" in str(o.get("error", "")) for o in lines),
              body.decode()[:160])

        code, _ = post(s.url + "/__ask__",
                       {"token": probe["token"], "provider": "nonesuch",
                        "messages": [{"role": "user", "content": "hi"}]})
        check("unknown provider rejected", code == 400)

        # a tombstone stays until it has had time to reach every store, then goes
        now = time.time() * 1000
        fresh = {"id": "t-new", "exact": "MARKER_MD", "occ": 0,
                 "deleted": True, "updated": now}
        stale = {"id": "t-old", "exact": "MARKER_MD", "occ": 0,
                 "deleted": True, "updated": now - 400 * 86400 * 1000}
        _, body = post(s.url + "/__annotations__",
                       {"merge": {"README.html": [fresh, stale]}})
        ids = {a["id"] for a in json.loads(body)["pages"].get("README.html", [])}
        check("a recent tombstone is kept", "t-new" in ids)
        check("an expired tombstone is pruned", "t-old" not in ids)


# ---------------------------------------------------------------- browser suite

def installed_chromium() -> "str | None":
    """Any chromium already in the Playwright cache, newest revision first.

    Lets the suite run on a machine that has browsers from some other tool
    without a fresh 150 MB download just because the pinned revision differs.
    """
    roots = [Path.home() / "Library/Caches/ms-playwright",          # macOS
             Path.home() / ".cache/ms-playwright"]                   # Linux
    if os.environ.get("LOCALAPPDATA"):                               # Windows
        roots.append(Path(os.environ["LOCALAPPDATA"]) / "ms-playwright")
    names = ("chrome-headless-shell", "headless_shell", "chrome", "Chromium")
    if os.name == "nt":
        names = tuple(n + ".exe" for n in names)
    found: list[tuple[int, str]] = []
    for root in roots:
        if not root.is_dir():
            continue
        for d in root.glob("chromium*-*"):
            try:
                rev = int(str(d.name).rsplit("-", 1)[-1])
            except ValueError:
                continue
            for name in names:
                for exe in d.rglob(name):
                    if exe.is_file() and os.access(exe, os.X_OK):
                        found.append((rev, str(exe)))
                        break
    return max(found)[1] if found else None


def test_browser(tmp: Path) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("browser: skipped — no playwright package "
              "(uv run --with playwright tests/test_bookify.py --browser)")
        return
    print("browser ui")
    src = tmp / "src"
    with FakeOllama() as fake, Serve(
            src, tmp / "out-ui", "--ask-provider", "ollama",
            env={"BOOKIFY_OLLAMA": fake.url}) as s, sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception:                                    # noqa: BLE001
            exe = installed_chromium()
            if not exe:
                print("browser: skipped — no chromium available "
                      "(run: playwright install chromium)")
                return
            print(f"browser: using {exe}")
            browser = pw.chromium.launch(executable_path=exe)
        page = browser.new_page(viewport={"width": 1400, "height": 880})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(s.url + "/README.html")
        page.evaluate("async()=>{await fetch('/__annotations__',"
                      "{method:'POST',body:JSON.stringify({pages:{}})})}")
        page.goto(s.url + "/README.html")
        page.wait_for_timeout(700)

        def count() -> int:
            """Live notes on the server. Deletes leave a tombstone behind so the
            removal can reach every store, and those are not notes."""
            return page.evaluate(
                "async()=>{const d=await (await fetch('/__annotations__',"
                "{cache:'no-store'})).json();"
                "return Object.values(d.pages)"
                ".reduce((n,l)=>n+l.filter(a=>!a.deleted).length,0)}")

        def tombstones() -> int:
            return page.evaluate(
                "async()=>{const d=await (await fetch('/__annotations__',"
                "{cache:'no-store'})).json();"
                "return Object.values(d.pages)"
                ".reduce((n,l)=>n+l.filter(a=>a.deleted).length,0)}")

        def painted() -> int:
            return page.evaluate(
                "()=>Array.from(CSS.highlights.get('bk-hl')).length"
                "+Array.from(CSS.highlights.get('bk-note')).length")

        def idb_notes() -> int:
            return page.evaluate("""async()=>{
              const d=await new Promise(r=>{const q=indexedDB.open('bookify');
                q.onsuccess=()=>r(q.result);q.onerror=()=>r(null);q.onblocked=()=>r(null)});
              if(!d)return -1;
              if(![...d.objectStoreNames].includes('annotations')){d.close();return -1}
              const rows=await new Promise(r=>{const tx=d.transaction(['annotations'],'readonly');
                const rq=tx.objectStore('annotations').getAll();
                rq.onsuccess=()=>r(rq.result||[]);rq.onerror=()=>r([])});
              d.close();return rows.filter(a=>!a.deleted).length}""")

        def clear_idb() -> str:
            # clearing the stores never blocks on the connection the page holds
            return page.evaluate("""async()=>{
              const d=await new Promise(r=>{const q=indexedDB.open('bookify');
                q.onsuccess=()=>r(q.result);q.onerror=()=>r(null);q.onblocked=()=>r(null)});
              if(!d)return 'no-db';
              const names=[...d.objectStoreNames]
                .filter(n=>['annotations','books','chats'].includes(n));
              if(!names.length){d.close();return 'no-stores'}
              await new Promise(r=>{const tx=d.transaction(names,'readwrite');
                for(const n of names)tx.objectStore(n).clear();
                tx.oncomplete=tx.onerror=tx.onabort=()=>r()});
              d.close();return 'cleared'}""")

        def wipe_server() -> None:
            page.evaluate("async()=>{await fetch('/__annotations__',"
                          "{method:'POST',body:JSON.stringify({pages:{}})})}")

        def drag(a: float, b: float) -> None:
            """Select a run of text in the first paragraph and wait for the
            toolbar. Clears any leftover selection and scrolls the paragraph
            into view first, so this does not depend on what ran before."""
            para = page.locator("main p").first
            para.scroll_into_view_if_needed()
            page.evaluate("()=>getSelection().removeAllRanges()")
            page.wait_for_timeout(60)
            box = para.bounding_box()
            page.mouse.move(box["x"] + box["width"] * a, box["y"] + 8)
            page.mouse.down()
            page.mouse.move(box["x"] + box["width"] * b, box["y"] + 8, steps=10)
            page.mouse.up()
            page.wait_for_function(
                "()=>document.querySelector('.bk-bar')"
                ".getBoundingClientRect().height>0", timeout=5000)

        drag(0.05, 0.45)
        check("toolbar appears on selection",
              page.evaluate("()=>document.querySelector('.bk-bar')"
                            ".getBoundingClientRect().height>0"))
        page.click('.bk-bar button[data-act="hl"]')
        page.wait_for_timeout(400)
        check("highlight is created", count() == 1)
        check("toolbar hides after acting",
              page.evaluate("()=>document.querySelector('.bk-bar')"
                            ".getBoundingClientRect().height===0"))

        # triple-click selects a paragraph: element-boundary ranges must work.
        # Both stores have to be emptied -- clearing only the server file would
        # leave IndexedDB to restore the note on reload (which it should).
        wipe_server()
        clear_idb()
        page.reload()
        page.wait_for_timeout(700)
        check("clearing both stores leaves nothing painted", painted() == 0,
              f"painted={painted()}")
        box = page.locator("main p").first.bounding_box()
        page.mouse.click(box["x"] + 40, box["y"] + 8, click_count=3)
        page.wait_for_timeout(250)
        page.click('.bk-bar button[data-act="hl"]')
        page.wait_for_timeout(400)
        check("triple-click can be highlighted", count() == 1)

        # one click on a highlight opens the editor; each dismissal keeps the note
        for name, act in (
                ("close button", lambda: page.click(".bk-pop button.x")),
                ("backdrop", lambda: page.mouse.click(20, 800)),
                ("escape", lambda: page.keyboard.press("Escape")),
                ("cancel", lambda: page.click('.bk-pop button[data-act="cancel"]:not(.x)'))):
            page.mouse.click(box["x"] + 40, box["y"] + 8)
            page.wait_for_timeout(320)
            opened = page.evaluate("()=>getComputedStyle(document.querySelector"
                                   "('.bk-pop')).display!=='none'")
            act()
            page.wait_for_timeout(250)
            closed = page.evaluate("()=>getComputedStyle(document.querySelector"
                                   "('.bk-pop')).display==='none'")
            check(f"dialog dismissed by {name}", opened and closed and count() == 1)

        # removal, both ways
        drag(0.10, 0.35)
        check("unhighlight offered over a highlight",
              page.evaluate("()=>getComputedStyle(document.querySelector"
                            "('.bk-bar [data-act=\"unhl\"]')).display!=='none'"))
        page.click('.bk-bar button[data-act="unhl"]')
        page.wait_for_timeout(400)
        check("unhighlight removes it", count() == 0)

        drag(0.05, 0.45)
        page.click('.bk-bar button[data-act="hl"]')
        page.wait_for_timeout(400)
        page.mouse.click(box["x"] + 40, box["y"] + 8)
        page.wait_for_timeout(320)
        page.click('.bk-pop button[data-act="del"]')
        page.wait_for_timeout(400)
        check("dialog removes it", count() == 0)

        # ask ai sidebar
        drag(0.05, 0.5)
        check("ask button is offered",
              page.evaluate("()=>{const b=document.querySelector"
                            "('.bk-bar [data-act=\"ask\"]');"
                            "return !!b && b.style.display!=='none'}"))
        page.click('.bk-bar button[data-act="ask"]')
        page.wait_for_timeout(400)
        check("chat sidebar opens",
              page.evaluate("()=>document.body.classList.contains('ask-open')"))
        page.wait_for_function(
            "()=>document.querySelector('#askpanel [data-act=\"stop\"]').disabled",
            timeout=30000)
        check("answer arrives",
              page.evaluate("()=>{const m=[...document.querySelectorAll"
                            "('#askpanel .ap-msg.ai')];"
                            "return m.length>0 && m[m.length-1].textContent"
                            ".includes('idempotent')}"))
        page.fill("#askpanel textarea", "and if it runs twice?")
        page.keyboard.press("Enter")
        page.wait_for_function(
            "()=>document.querySelector('#askpanel [data-act=\"stop\"]').disabled",
            timeout=30000)
        check("follow-up keeps the transcript",
              page.evaluate("()=>document.querySelectorAll"
                            "('#askpanel .ap-msg').length===4"))
        page.click('#askpanel [data-act="note"]')
        page.wait_for_timeout(700)
        check("answer saves onto the highlight",
              page.evaluate("async()=>{const d=await (await fetch"
                            "('/__annotations__',{cache:'no-store'})).json();"
                            "const l=(d.pages['README.html']||[])"
                            ".filter(a=>!a.deleted);"
                            "return l.length===1 && (l[0].note||'').startsWith('AI: ')}"))
        page.keyboard.press("Escape")
        page.wait_for_timeout(350)
        check("escape closes the chat",
              page.evaluate("()=>!document.body.classList.contains('ask-open')"))

        # sidebar filter
        page.fill("#navfilter", "zzzz")
        page.wait_for_timeout(200)
        check("filter reports no matches",
              page.evaluate("()=>{const p=document.querySelector"
                            "('nav.sidebar .tree > p');"
                            "return !!p && getComputedStyle(p).display!=='none'}"))
        check("filter hides emptied folders",
              page.evaluate("()=>[...document.querySelectorAll('nav.sidebar details')]"
                            ".every(d=>getComputedStyle(d).display==='none')"))
        page.fill("#navfilter", "")
        page.wait_for_timeout(200)
        check("clearing the filter restores the tree",
              page.evaluate("()=>[...document.querySelectorAll"
                            "('nav.sidebar li[data-t]')]"
                            ".every(l=>getComputedStyle(l).display!=='none')"))

        # ---- storage durability: IndexedDB and the server file back each other
        # up, and a delete never comes back ----
        clear_idb()
        wipe_server()
        page.reload()
        page.wait_for_timeout(700)
        drag(0.05, 0.45)
        page.click('.bk-bar button[data-act="hl"]')
        page.wait_for_timeout(500)
        check("a note reaches both stores", count() == 1 and idb_notes() == 1,
              f"server={count()} idb={idb_notes()}")

        wipe_server()
        page.reload()
        page.wait_for_timeout(900)
        check("IndexedDB restores a lost server file",
              painted() == 1 and count() == 1,
              f"painted={painted()} server={count()}")

        clear_idb()
        page.reload()
        page.wait_for_timeout(900)
        check("the server file restores a cleared IndexedDB",
              painted() == 1 and idb_notes() == 1,
              f"painted={painted()} idb={idb_notes()}")

        pbox = page.locator("main p").first.bounding_box()
        page.mouse.click(pbox["x"] + pbox["width"] * 0.15, pbox["y"] + 8)
        page.wait_for_timeout(350)
        page.click('.bk-pop button[data-act="del"]')
        page.wait_for_timeout(500)
        check("a delete is recorded as a tombstone",
              painted() == 0 and count() == 0 and tombstones() == 1,
              f"painted={painted()} live={count()} tombs={tombstones()}")
        clear_idb()
        page.reload()
        page.wait_for_timeout(900)
        check("a deleted note is not resurrected by the other store",
              painted() == 0, f"painted={painted()}")

        # each book keeps its own rows, keyed by the id built into the page
        check("annotations are scoped to a book id",
              page.evaluate("""async()=>{
                const d=await new Promise(r=>{const q=indexedDB.open('bookify');
                  q.onsuccess=()=>r(q.result);q.onerror=()=>r(null)});
                if(!d)return false;
                const rows=await new Promise(r=>{const tx=d.transaction(['annotations'],'readonly');
                  const rq=tx.objectStore('annotations').getAll();
                  rq.onsuccess=()=>r(rq.result||[]);rq.onerror=()=>r([])});
                const books=await new Promise(r=>{const tx=d.transaction(['books'],'readonly');
                  const rq=tx.objectStore('books').getAll();
                  rq.onsuccess=()=>r(rq.result||[]);rq.onerror=()=>r([])});
                d.close();
                const id=document.body.dataset.book;
                return rows.every(a=>a.bookId===id) && books.some(b=>b.id===id)}"""))

        # an Ask AI thread is stored per passage and comes back
        clear_idb()
        page.reload()
        page.wait_for_timeout(700)
        drag(0.05, 0.5)
        page.click('.bk-bar button[data-act="ask"]')
        page.wait_for_timeout(400)
        page.wait_for_function(
            "()=>document.querySelector('#askpanel [data-act=\"stop\"]').disabled",
            timeout=30000)
        page.fill("#askpanel textarea", "and what about retries?")
        page.keyboard.press("Enter")
        page.wait_for_function(
            "()=>document.querySelector('#askpanel [data-act=\"stop\"]').disabled",
            timeout=30000)
        check("the chat is written to IndexedDB",
              page.evaluate("""async()=>{
                const d=await new Promise(r=>{const q=indexedDB.open('bookify');
                  q.onsuccess=()=>r(q.result);q.onerror=()=>r(null)});
                if(!d)return 0;
                const rows=await new Promise(r=>{const tx=d.transaction(['chats'],'readonly');
                  const rq=tx.objectStore('chats').getAll();
                  rq.onsuccess=()=>r(rq.result||[]);rq.onerror=()=>r([])});
                d.close();
                return rows.length===1 && rows[0].msgs.length===4}"""))
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        page.reload()
        page.wait_for_timeout(900)
        drag(0.05, 0.5)
        page.click('.bk-bar button[data-act="ask"]')
        page.wait_for_timeout(900)
        check("reopening the passage restores the thread",
              page.evaluate("()=>document.querySelectorAll('#askpanel .ap-msg').length")== 4
              and "restored" in page.evaluate("()=>document.querySelector('#askpanel .sp').textContent"))
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)

        # book map: dragging a node must not navigate
        page.goto(s.url + "/__map__.html")
        page.wait_for_timeout(1800)
        node = None
        y = 90
        while y < 700 and node is None:
            x = 90
            while x < 1300 and node is None:
                page.mouse.move(x, y)
                if page.evaluate("()=>document.getElementById('c').style.cursor") == "pointer":
                    node = (x, y)
                x += 26
            y += 26
        if node:
            page.mouse.move(*node)
            page.mouse.down()
            page.mouse.move(node[0] + 170, node[1] + 130, steps=14)
            page.mouse.up()
            page.wait_for_timeout(600)
            check("dragging a map node does not navigate",
                  page.url.endswith("/__map__.html"), page.url)
        else:
            check("map node found for drag test", False, "no node under the cursor")

        # full-text search
        page.goto(s.url + "/README.html")
        page.wait_for_timeout(400)
        page.keyboard.press("Control+k")
        page.wait_for_selector("#searchbox:not([hidden]) input", timeout=5000)
        page.keyboard.type("marker_alt")
        page.wait_for_function(
            "()=>document.querySelectorAll('#searchbox li[data-i]').length>0", timeout=5000)
        check("search finds text on another page",
              page.evaluate("()=>document.querySelector('#searchbox li[data-i]')"
                            ".textContent.includes('Alt Page')"))
        check("search marks the matched words",
              page.evaluate("()=>!!document.querySelector('#searchbox li[data-i] mark')"))
        page.keyboard.press("Enter")
        page.wait_for_url("**/guide/alt.html*", timeout=5000)
        page.wait_for_timeout(500)
        check("a result opens its page with the terms highlighted",
              page.evaluate("()=>CSS.highlights.has('bk-search')"
                            "&&CSS.highlights.get('bk-search').size>0"))
        page.keyboard.press("Control+k")
        page.wait_for_timeout(250)
        check("search remembers the last query",
              page.evaluate("()=>document.querySelector('#searchbox input').value==='marker_alt'"))
        page.keyboard.press("Escape")
        page.wait_for_timeout(250)
        check("escape closes search", page.evaluate("()=>document.getElementById('searchbox').hidden"))

        # a large book's shared sidebar is filled in by script, current page marked
        with Serve(src, tmp / "out-ui-shared", "--no-ask",
                   env={"BOOKIFY_INLINE_NAV_LIMIT": "1"}) as shared:
            page.goto(shared.url + "/guide/page.html")
            page.wait_for_timeout(500)
            check("shared sidebar is filled in",
                  page.evaluate("()=>document.querySelectorAll('nav.sidebar .tree a').length>=3"))
            check("shared sidebar marks the current page and opens its folder",
                  page.evaluate("()=>{const a=document.querySelector('nav.sidebar .tree a.active');"
                                "return !!a && a.getAttribute('href')==='../guide/page.html'"
                                "&& a.closest('details').open}"))
            check("shared sidebar links resolve from a subfolder",
                  page.evaluate("()=>[...document.querySelectorAll('nav.sidebar .tree a')]"
                                ".some(a=>a.href.endsWith('/README.html'))"))

        check("no uncaught page errors", not errors, "; ".join(errors[:3]))
        browser.close()


# ---------------------------------------------------------------- main

def main() -> int:
    global VERBOSE
    ap = argparse.ArgumentParser(description="bookify regression tests")
    ap.add_argument("--browser", action="store_true",
                    help="also run the Playwright UI suite")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    VERBOSE = args.verbose

    if not BOOKIFY.is_file():
        print(f"bookify not found at {BOOKIFY}", file=sys.stderr)
        return 2
    if not shutil.which("uv"):
        print("uv is required to run bookify (https://docs.astral.sh/uv/)",
              file=sys.stderr)
        return 2

    started = time.time()
    with tempfile.TemporaryDirectory(prefix="bookify-tests-") as td:
        tmp = Path(td)
        write_fixture(tmp / "src")
        test_build(tmp)
        test_docs_systems(tmp)
        test_navigation(tmp)
        test_wikilinks(tmp)
        test_templates(tmp)
        test_annotations(tmp)
        test_ask(tmp)
        if args.browser:
            test_browser(tmp)

    took = time.time() - started
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed in {took:.1f}s")
    for f in FAIL:
        print(f"  - {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
