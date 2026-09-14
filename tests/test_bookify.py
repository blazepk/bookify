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


def run_build(src: Path, out: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*BOOKIFY_CMD, str(src), "--no-serve", "-o", str(out), *extra],
        capture_output=True, text=True, cwd=ROOT, timeout=600, env=child_env())


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
    names = ("chrome-headless-shell", "headless_shell", "chrome", "Chromium")
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
