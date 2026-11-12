import asyncio, re, sys, time
from pathlib import Path
from aiohttp import web
from playwright.async_api import async_playwright

PROFILE = Path.home() / ".code_master_profile"
OUT = Path("output")
TIMEOUT, STABLE = 180, 5
SITES = {
    "ChatGPT": "https://chatgpt.com/", "Grok": "https://grok.com/",
    "Claude": "https://claude.ai/new", "Gemini": "https://gemini.google.com/app",
    "DeepSeek": "https://chat.deepseek.com/", "Perplexity": "https://www.perplexity.ai/",
    "Copilot": "https://copilot.microsoft.com/", "Mistral": "https://chat.mistral.ai/chat",
}
INPUT = "textarea, div[contenteditable='true'], div[role='textbox']"
LOCK = asyncio.Lock()
CTX = None
STATUS = {"state": "idle", "ais": {}, "answers": {}, "final": "", "merger": ""}


def slug(t): return re.sub(r"[^a-z0-9]+", "_", t.lower()).strip("_")[:40] or "task"
def best(b): return max(b, key=len).strip() if b else ""


async def read_blocks(page):
    try:
        b = await page.evaluate("[...document.querySelectorAll('pre')].map(p=>p.innerText)")
        return b or [await page.evaluate(
            "(document.querySelector('main')||document.body).innerText.slice(-8000)")]
    except Exception:
        return []


async def ask(name, prompt, track=True):
    t0 = time.time()
    if track: STATUS["ais"][name] = "working..."
    page = await CTX.new_page()
    try:
        await page.goto(SITES[name], wait_until="domcontentloaded", timeout=60000)
        box = page.locator(INPUT).last
        await box.wait_for(state="visible", timeout=45000)
        async with LOCK:
            await page.bring_to_front()
            await box.click()
            await page.keyboard.insert_text(prompt)
            await asyncio.sleep(0.4)
            await page.keyboard.press("Enter")
            await asyncio.sleep(0.8)
            btn = page.locator("button[aria-label*='Send' i]").last
            if await btn.count() and await btn.is_enabled():
                try: await btn.click(timeout=1500)
                except Exception: pass
        last, same = None, 0
        while time.time() - t0 < TIMEOUT:
            await asyncio.sleep(1)
            cur = "\n".join(await read_blocks(page))
            if cur and cur == last:
                same += 1
                if same >= STABLE: break
            else:
                last, same = cur, 0
        res = best(await read_blocks(page))
        if track: STATUS["ais"][name] = f"done {time.time()-t0:.0f}s"
        return res
    except Exception as e:
        if track: STATUS["ais"][name] = f"failed: {type(e).__name__}"
        return ""


async def run_task(task, names, merger):
    STATUS.update(state="running", ais={}, answers={}, final="", merger=merger)
    out = OUT / slug(task); out.mkdir(parents=True, exist_ok=True)
    res = await asyncio.gather(*(ask(n, task) for n in names))
    answers = {n: r for n, r in zip(names, res) if r}
    STATUS["answers"] = answers
    for n, c in answers.items(): (out / f"{n}.txt").write_text(c, encoding="utf-8")
    if not answers:
        STATUS["state"] = "done"; return
    m = merger if merger in SITES else next(iter(answers))
    STATUS["ais"]["Merge"] = f"merging with {m}..."
    cands = "\n\n".join(f"===== CANDIDATE FROM {n} =====\n{c}" for n, c in answers.items())
    mp = (f"Task: {task}\n\nBelow are {len(answers)} independent solutions from different AIs. "
          "Study them all, fix bugs, keep the best ideas and features from each, and write ONE "
          "final complete working solution. Output only the final code in a single code block.\n\n" + cands)
    final = await ask(m, mp, track=False) or "\n\n".join(answers.values())
    STATUS["ais"]["Merge"] = "final code ready"
    STATUS["final"] = final
    (out / "final_code.txt").write_text(final, encoding="utf-8")
    STATUS["state"] = "done"


@web.middleware
async def cors(request, handler):
    r = web.Response() if request.method == "OPTIONS" else await handler(request)
    r.headers.update({"Access-Control-Allow-Origin": "*",
                      "Access-Control-Allow-Headers": "*",
                      "Access-Control-Allow-Methods": "GET,POST,OPTIONS"})
    return r


async def h_status(_): return web.json_response({**STATUS, "sites": list(SITES)})


async def h_run(req):
    if STATUS["state"] == "running":
        return web.json_response({"error": "busy"}, status=409)
    d = await req.json()
    names = [n for n in d.get("names", []) if n in SITES]
    if not d.get("task") or not names:
        return web.json_response({"error": "bad request"}, status=400)
    asyncio.create_task(run_task(d["task"], names, d.get("merger", "Claude")))
    return web.json_response({"ok": True})


async def start(app):
    global CTX
    app["pw"] = await async_playwright().start()
    CTX = await app["pw"].chromium.launch_persistent_context(
        str(PROFILE), channel="chrome", headless=False,
        viewport={"width": 1280, "height": 800},
        args=["--disable-blink-features=AutomationControlled"])


async def stop(app):
    await CTX.close(); await app["pw"].stop()


async def login():
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(str(PROFILE), channel="chrome", headless=False)
        for u in SITES.values():
            pg = await ctx.new_page(); await pg.goto(u)
        await asyncio.to_thread(input, "Log in to every AI, then press Enter here...")
        await ctx.close()


if __name__ == "__main__":
    if "--login" in sys.argv:
        asyncio.run(login())
    else:
        app = web.Application(middlewares=[cors])
        app.add_routes([web.get("/status", h_status), web.post("/run", h_run),
                        web.route("OPTIONS", "/{p:.*}", lambda r: web.Response())])
        app.on_startup.append(start); app.on_cleanup.append(stop)
        print("Code Master agent running on http://localhost:8765 - open code-master.html")
        web.run_app(app, host="127.0.0.1", port=8765, print=None)
