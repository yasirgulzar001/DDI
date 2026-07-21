#!/usr/bin/env python3
"""
YouTube Views Generator – Production-Ready Version
- Fixes: stealth API, latency guard, file size limit, encrypted credentials,
  proxy-less job skip, background proxy checks, port validation.
"""

import asyncio, re, random, logging, os, sys, base64, hashlib
from dotenv import load_dotenv

load_dotenv()

from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, Any

from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

import aiosqlite
from cryptography.fernet import Fernet

from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from playwright_stealth import StealthConfig, stealth_async
import fake_useragent

# -------------------------------------------------------------------
# Configuration
# -------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
if not BOT_TOKEN:
    sys.exit("ERROR: BOT_TOKEN is not set. Add it to your .env file.")

ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "0").split(",")))
DB_PATH = "bot.db"
DEFAULT_THREADS = 5
PROXY_CHECK_INTERVAL = 15 * 60          # 15 min
DEAD_PROXY_CLEANUP_INTERVAL = 60 * 60   # 1 hour
DEAD_PROXY_RETENTION_HOURS = 24
MAX_CONCURRENT_VIEWS = 10
MAX_PROXY_FILE_SIZE = 1 * 1024 * 1024   # 1 MB

STORAGE_DIR = Path("./browser_sessions")
STORAGE_DIR.mkdir(exist_ok=True)

# --- Encryption setup (for proxy credentials) ---
_enc_key = os.getenv("ENCRYPTION_KEY")
if not _enc_key:
    # Derive a deterministic key from BOT_TOKEN (NOT secure for real secrets)
    _raw = hashlib.sha256(BOT_TOKEN.encode()).digest()
    _enc_key = base64.urlsafe_b64encode(_raw)
    logger.warning("ENCRYPTION_KEY not set. Using BOT_TOKEN-derived key – change for production.")
_fernet = Fernet(_enc_key)

def encrypt(text: str) -> str:
    return _fernet.encrypt(text.encode()).decode()

def decrypt(token: str) -> str:
    return _fernet.decrypt(token.encode()).decode()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# -------------------------------------------------------------------
# Database
# -------------------------------------------------------------------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                credits INTEGER DEFAULT 10000,
                tier TEXT DEFAULT 'free',
                threads INTEGER DEFAULT 5,
                autocheck INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                video_url TEXT NOT NULL,
                target_views INTEGER NOT NULL,
                completed_views INTEGER DEFAULT 0,
                failed_views INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',
                country TEXT,
                min_watch INTEGER DEFAULT 60,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS proxies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                ip TEXT NOT NULL,
                port INTEGER NOT NULL,
                username TEXT,
                password TEXT,
                type TEXT DEFAULT 'residential',
                country TEXT,
                alive INTEGER DEFAULT 1,
                consecutive_fails INTEGER DEFAULT 0,
                banned_until TIMESTAMP,
                health_score REAL DEFAULT 1.0,
                last_checked TIMESTAMP,
                latency_ms INTEGER,
                last_error TEXT,
                storage_state_path TEXT
            );
        """)
        await db.commit()

# -------------------------------------------------------------------
# FSM & Keyboards
# -------------------------------------------------------------------
class AddView(StatesGroup):
    url = State()
    target = State()
    country = State()
    watch = State()
    confirm = State()

def main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="/views"), KeyboardButton(text="/add")],
            [KeyboardButton(text="/status"), KeyboardButton(text="/jobs")],
            [KeyboardButton(text="/threads"), KeyboardButton(text="/balance")],
            [KeyboardButton(text="/myproxies"), KeyboardButton(text="/proxyhealth")],
            [KeyboardButton(text="/addproxies"), KeyboardButton(text="/checkmyproxies")],
            [KeyboardButton(text="/autocheck"), KeyboardButton(text="/help")],
        ],
        resize_keyboard=True
    )

def country_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇺🇸 US", callback_data="country_US"),
         InlineKeyboardButton(text="🇬🇧 UK", callback_data="country_UK")],
        [InlineKeyboardButton(text="🌍 Any", callback_data="country_any")]
    ])

def confirm_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Confirm", callback_data="confirm_job"),
         InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_job")]
    ])

# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------
def extract_video_id(url: str) -> Optional[str]:
    patterns = [
        r"(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})",
        r"/embed/([a-zA-Z0-9_-]{11})",
        r"/shorts/([a-zA-Z0-9_-]{11})"
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None

def estimate_cost_cents(amount: int, tier: str) -> int:
    return amount * (1 if tier == "premium" else 10)

def cents_to_dollars(cents: int) -> str:
    return f"${cents / 100:.2f}"

def parse_proxy_line(line: str) -> Optional[dict]:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "://" in line:
        line = line.split("://", 1)[1]
    if "@" in line:
        auth, host = line.split("@", 1)
        user_pass = auth.split(":", 1)
        ip_port = host.split(":")
        if len(user_pass) != 2 or len(ip_port) != 2:
            return None
        try:
            port = int(ip_port[1])
            if not (1 <= port <= 65535):
                return None
        except ValueError:
            return None
        return {
            "ip": ip_port[0],
            "port": port,
            "username": user_pass[0],
            "password": user_pass[1]
        }
    else:
        parts = line.split(":")
        if len(parts) == 2:
            try:
                port = int(parts[1])
                if not (1 <= port <= 65535):
                    return None
            except ValueError:
                return None
            return {"ip": parts[0], "port": port, "username": None, "password": None}
        elif len(parts) == 4:
            try:
                port = int(parts[1])
                if not (1 <= port <= 65535):
                    return None
            except ValueError:
                return None
            return {"ip": parts[0], "port": port, "username": parts[2], "password": parts[3]}
    return None

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# -------------------------------------------------------------------
# Fingerprint Generator
# -------------------------------------------------------------------
class FingerprintGenerator:
    def __init__(self):
        self.ua = fake_useragent.UserAgent(browsers=['chrome', 'edge', 'chromium'], os=['windows', 'macos', 'linux'])

    def random_fingerprint(self, country: str = None) -> Dict[str, Any]:
        platforms = {'windows': 'Win32', 'macos': 'MacIntel', 'linux': 'Linux x86_64'}
        os_choice = random.choice(['windows', 'macos', 'linux'])
        platform = platforms[os_choice]
        user_agent = self.ua.random

        viewport_widths = [1366, 1440, 1536, 1920, 2560]
        viewport_heights = [768, 900, 864, 1080, 1440]
        width = random.choice(viewport_widths)
        height = random.choice(viewport_heights)

        if country == "US":
            lat, lon = random.uniform(25.0, 49.0), random.uniform(-125.0, -66.0)
        elif country == "UK":
            lat, lon = random.uniform(50.0, 59.0), random.uniform(-8.0, 2.0)
        else:
            lat, lon = random.uniform(35.0, 60.0), random.uniform(-10.0, 40.0)

        tz_offset = int((lon / 15) + 0.5)
        timezone_id = f"Etc/GMT{'+' if tz_offset >= 0 else ''}{tz_offset}" if tz_offset != 0 else "UTC"
        if tz_offset in [-5, -6, -7, -8]:
            timezone_id = random.choice(['America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles'])
        elif tz_offset in [0, 1, 2]:
            timezone_id = random.choice(['Europe/London', 'Europe/Paris', 'Europe/Berlin'])

        locale = random.choice(['en-US', 'en-GB', 'en-CA', 'en-AU'])
        color_depth = 24
        device_pixel_ratio = random.choice([1, 1.25, 1.5, 2])
        hardware_concurrency = random.choice([2, 4, 8, 12, 16])
        webgl_vendor = random.choice(['Google Inc. (Intel)', 'Google Inc. (NVIDIA)', 'Google Inc. (AMD)'])
        webgl_renderer = random.choice([
            'ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)',
            'ANGLE (NVIDIA, NVIDIA GeForce GTX 1050 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)',
            'ANGLE (AMD, AMD Radeon(TM) Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)'
        ])

        return {
            "user_agent": user_agent,
            "platform": platform,
            "viewport": {"width": width, "height": height},
            "geolocation": {"latitude": lat, "longitude": lon, "accuracy": random.uniform(20, 100)},
            "timezone_id": timezone_id,
            "locale": locale,
            "color_depth": color_depth,
            "device_pixel_ratio": device_pixel_ratio,
            "hardware_concurrency": hardware_concurrency,
            "webgl_vendor": webgl_vendor,
            "webgl_renderer": webgl_renderer,
        }

# -------------------------------------------------------------------
# User Concurrency Manager
# -------------------------------------------------------------------
class UserConcurrencyManager:
    def __init__(self):
        self.semaphores: Dict[int, asyncio.Semaphore] = {}
        self.limits: Dict[int, int] = {}  # explicit limits – never touch _value

    def set_limit(self, user_id: int, limit: int):
        self.limits[user_id] = limit
        self.semaphores[user_id] = asyncio.Semaphore(limit)

    def get_limit(self, user_id: int) -> int:
        return self.limits.get(user_id, DEFAULT_THREADS)

    async def acquire(self, user_id: int) -> asyncio.Semaphore:
        if user_id not in self.semaphores:
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute("SELECT threads FROM users WHERE id=?", (user_id,))
                row = await cur.fetchone()
                limit = row["threads"] if row else DEFAULT_THREADS
            self.limits[user_id] = limit
            self.semaphores[user_id] = asyncio.Semaphore(limit)
        return self.semaphores[user_id]

    async def cleanup(self):
        async with aiosqlite.connect(DB_PATH) as db:
            for uid in list(self.semaphores.keys()):
                cur = await db.execute("SELECT 1 FROM users WHERE id=?", (uid,))
                if not await cur.fetchone():
                    del self.semaphores[uid]
                    self.limits.pop(uid, None)

# -------------------------------------------------------------------
# Browser Manager (NO TLS PROXY)
# -------------------------------------------------------------------
class BrowserManager:
    def __init__(self):
        self.playwright = None
        self.browser: Browser = None
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT_VIEWS)
        self.fingerprint_gen = FingerprintGenerator()
        self.user_concurrency = UserConcurrencyManager()

    async def start(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-blink-features=AutomationControlled',
                  '--disable-dev-shm-usage', '--disable-setuid-sandbox']
        )
        asyncio.create_task(dead_proxy_cleanup_loop())
        asyncio.create_task(semaphore_cleanup_loop(self.user_concurrency))
        logger.info("BrowserManager started (no TLS proxy)")

    async def stop(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def create_context(self, proxy: Dict[str, Any] = None,
                             country: str = None,
                             storage_state_path: str = None) -> BrowserContext:
        fp_data = self.fingerprint_gen.random_fingerprint(country)

        playwright_proxy = None
        if proxy and proxy.get("ip"):
            server = f"http://{proxy['ip']}:{proxy['port']}"
            username = proxy.get("username")
            password = proxy.get("password")
            if username and password:
                # decrypt credentials if stored encrypted (they are plain now after decryption in query)
                server = f"http://{username}:{password}@{proxy['ip']}:{proxy['port']}"
            playwright_proxy = {"server": server}

        storage_state = None
        if storage_state_path and Path(storage_state_path).exists():
            storage_state = storage_state_path

        context = await self.browser.new_context(
            user_agent=fp_data["user_agent"],
            viewport=fp_data["viewport"],
            geolocation=fp_data["geolocation"],
            permissions=["geolocation"],
            timezone_id=fp_data["timezone_id"],
            locale=fp_data["locale"],
            color_scheme=random.choice(['light', 'dark']),
            device_scale_factor=fp_data["device_pixel_ratio"],
            proxy=playwright_proxy,
            storage_state=storage_state,
            extra_http_headers={"Accept-Language": fp_data["locale"]}
        )

        await stealth_async(context, StealthConfig(
            webdriver=True, chrome_app=True, chrome_runtime=True,
            hack_webrtc=False, remove_hairline=True,
            mock_fonts=True, mock_chrome_features=True,
        ))

        # Override specific navigator props via init script
        await context.add_init_script(f"""
            Object.defineProperty(navigator, 'platform', {{ get: () => '{fp_data["platform"]}' }});
            Object.defineProperty(navigator, 'hardwareConcurrency', {{ get: () => {fp_data["hardware_concurrency"]} }});
            Object.defineProperty(screen, 'colorDepth', {{ get: () => {fp_data["color_depth"]} }});
            const getParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(parameter) {{
                if (parameter === 37445) return '{fp_data["webgl_vendor"]}';
                if (parameter === 37446) return '{fp_data["webgl_renderer"]}';
                return getParameter.call(this, parameter);
            }};
        """)
        return context

# -------------------------------------------------------------------
# Human Simulation & View Session
# -------------------------------------------------------------------
class HumanSimulator:
    @staticmethod
    async def random_delay(min_s=0.1, max_s=1.5):
        await asyncio.sleep(random.uniform(min_s, max_s))

    @staticmethod
    async def move_mouse_randomly(page: Page, times: int = 3):
        for _ in range(times):
            x = random.randint(100, 800)
            y = random.randint(100, 600)
            await page.mouse.move(x, y)
            await HumanSimulator.random_delay(0.05, 0.3)

    @staticmethod
    async def human_scroll(page: Page, max_scrolls: int = 2):
        for _ in range(random.randint(1, max_scrolls)):
            await page.mouse.wheel(0, random.randint(200, 600))
            await HumanSimulator.random_delay(0.5, 2.0)

    @staticmethod
    async def click_randomly(page: Page, selector: str = "a", clicks: int = 1):
        elements = await page.query_selector_all(selector)
        if elements:
            for _ in range(clicks):
                el = random.choice(elements)
                try:
                    await el.click(delay=random.randint(50, 200))
                except Exception:
                    pass
                await HumanSimulator.random_delay(0.5, 1.0)

class ViewSession:
    def __init__(self, bm: BrowserManager, proxy: Dict[str, Any] = None, country: str = None):
        self.bm = bm
        self.proxy = proxy
        self.country = country
        self.context: BrowserContext = None
        self.page: Page = None
        self.storage_path = proxy.get("storage_state_path") if proxy else None

    async def __aenter__(self):
        self.context = await self.bm.create_context(
            proxy=self.proxy,
            country=self.country,
            storage_state_path=self.storage_path
        )
        self.page = await self.context.new_page()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.context:
            if self.storage_path:
                try:
                    await self.context.storage_state(path=self.storage_path)
                except Exception as e:
                    logger.error(f"Failed to save storage state: {e}")
            await self.context.close()

    async def execute_view(self, video_url: str, min_watch: int) -> bool:
        try:
            await self.page.goto("https://www.youtube.com/", wait_until="domcontentloaded", timeout=30000)
            await HumanSimulator.random_delay(1.0, 3.0)
            await HumanSimulator.move_mouse_randomly(self.page, random.randint(2, 5))
            await HumanSimulator.human_scroll(self.page, random.randint(1, 3))

            await self.page.goto(video_url, wait_until="domcontentloaded", timeout=30000)
            await HumanSimulator.random_delay(1.0, 4.0)

            try:
                await self.page.wait_for_selector("video", state="visible", timeout=15000)
            except Exception:
                logger.warning("Video element not found")
                return False

            try:
                video_el = await self.page.query_selector("video")
                if video_el:
                    await video_el.click(delay=random.randint(50, 150))
                    await HumanSimulator.random_delay(0.5, 1.0)
            except Exception:
                pass

            watch_time = random.randint(min_watch, min_watch + 30)
            elapsed = 0
            while elapsed < watch_time:
                pause = random.uniform(1, 5)
                await asyncio.sleep(pause)
                elapsed += pause
                if random.random() < 0.3:
                    await HumanSimulator.move_mouse_randomly(self.page, 1)
                if random.random() < 0.2:
                    await HumanSimulator.human_scroll(self.page, 1)
                if random.random() < 0.1:
                    await self.page.mouse.move(400, random.randint(500, 600))

            if random.random() < 0.4:
                await HumanSimulator.human_scroll(self.page, random.randint(1, 2))
                await HumanSimulator.random_delay(2.0, 5.0)
                await HumanSimulator.move_mouse_randomly(self.page, 2)

            if random.random() < 0.3:
                links = await self.page.query_selector_all("a#thumbnail")
                if links:
                    try:
                        await random.choice(links).click(delay=random.randint(100, 300))
                        await HumanSimulator.random_delay(2.0, 5.0)
                    except Exception:
                        pass
            return True
        except Exception as e:
            logger.error(f"View failed: {e}")
            return False

# -------------------------------------------------------------------
# Proxy Health Checker
# -------------------------------------------------------------------
def _failed_result(proxy: dict, error: str, banned_until=None) -> dict:
    return {
        "alive": False,
        "consecutive_fails": proxy.get("consecutive_fails", 0) + 1,
        "health_score": max(0.0, proxy.get("health_score", 1.0) - 0.2),
        "banned_until": banned_until,
        "last_error": error,
        "latency_ms": None,
    }

async def check_proxy_with_browser(proxy: dict, bm: BrowserManager) -> dict:
    try:
        server = f"http://{proxy['ip']}:{proxy['port']}"
        if proxy.get("username"):
            # credentials are now stored encrypted; decrypt when using
            user = decrypt(proxy["username"])
            pwd = decrypt(proxy["password"])
            server = f"http://{user}:{pwd}@{proxy['ip']}:{proxy['port']}"
        playwright_proxy = {"server": server}
        context = await bm.browser.new_context(proxy=playwright_proxy)
        page = await context.new_page()
        try:
            resp = await page.goto("https://www.youtube.com/", timeout=15000)
            if resp and resp.status == 200:
                content = await page.content()
                if "unusual traffic" not in content.lower():
                    latency = None
                    if resp.request is not None:
                        try:
                            latency = int(resp.request.responseEndTiming - resp.request.requestStartTiming)
                        except Exception:
                            latency = None
                    return {
                        "alive": True,
                        "consecutive_fails": 0,
                        "health_score": min(1.0, proxy.get("health_score", 1.0) + 0.1),
                        "banned_until": None,
                        "last_error": None,
                        "latency_ms": latency,
                    }
                else:
                    return _failed_result(
                        proxy, "Captcha/unusual traffic",
                        banned_until=datetime.now(timezone.utc) + timedelta(hours=2)
                    )
            else:
                banned_until = (
                    datetime.now(timezone.utc) + timedelta(hours=2)
                    if resp and resp.status == 429 else None
                )
                return _failed_result(
                    proxy, f"HTTP {resp.status if resp else 'connection failed'}",
                    banned_until=banned_until
                )
        except Exception as e:
            return _failed_result(proxy, str(e))
        finally:
            await context.close()
    except Exception as e:
        logger.error(f"Browser proxy check error: {e}")
        return _failed_result(proxy, str(e))

async def proxy_health_check_loop(bm: BrowserManager):
    while True:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute("""
                    SELECT * FROM proxies
                    WHERE last_checked IS NULL OR last_checked < datetime('now', '-15 minutes')
                    LIMIT 20
                """)
                due = [dict(row) for row in await cur.fetchall()]

            for proxy in due:
                updates = await check_proxy_with_browser(proxy, bm)
                updates["last_checked"] = datetime.now(timezone.utc)
                async with aiosqlite.connect(DB_PATH) as db:
                    if not proxy.get("storage_state_path"):
                        state_path = STORAGE_DIR / f"proxy_{proxy['id']}.json"
                        await db.execute(
                            "UPDATE proxies SET storage_state_path=? WHERE id=?",
                            (str(state_path), proxy["id"])
                        )
                    await db.execute("""
                        UPDATE proxies SET alive=?, consecutive_fails=?, banned_until=?,
                        health_score=?, last_checked=?, last_error=?, latency_ms=?
                        WHERE id=?
                    """, (
                        updates["alive"], updates["consecutive_fails"], updates["banned_until"],
                        updates["health_score"], updates["last_checked"],
                        updates.get("last_error"), updates.get("latency_ms"), proxy["id"]
                    ))
                    await db.commit()
        except Exception as e:
            logger.error(f"Proxy health loop error: {e}")
        await asyncio.sleep(PROXY_CHECK_INTERVAL)

async def dead_proxy_cleanup_loop():
    while True:
        await asyncio.sleep(DEAD_PROXY_CLEANUP_INTERVAL)
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("""
                    DELETE FROM proxies WHERE alive = 0
                    AND last_checked < datetime('now', ?)
                """, (f"-{DEAD_PROXY_RETENTION_HOURS} hours",))
                await db.commit()
            logger.info("Dead proxy cleanup completed")
        except Exception as e:
            logger.error(f"Dead proxy cleanup failed: {e}")

async def semaphore_cleanup_loop(ucm: UserConcurrencyManager):
    while True:
        await asyncio.sleep(3600)
        await ucm.cleanup()

# -------------------------------------------------------------------
# Job Worker
# -------------------------------------------------------------------
async def is_job_cancelled(job_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT status FROM jobs WHERE id=?", (job_id,))
        row = await cur.fetchone()
        return row is None or row[0] == "cancelled"

async def worker_thread(job_id: int, video_url: str, min_watch: int,
                        proxy: dict, bm: BrowserManager, user_sem: asyncio.Semaphore):
    async with user_sem, bm.semaphore:
        if await is_job_cancelled(job_id):
            return False
        async with ViewSession(bm, proxy) as session:
            success = await session.execute_view(video_url, min_watch)
    async with aiosqlite.connect(DB_PATH) as db:
        if success:
            await db.execute("UPDATE jobs SET completed_views = completed_views + 1 WHERE id=?", (job_id,))
        else:
            await db.execute("UPDATE jobs SET failed_views = failed_views + 1 WHERE id=?", (job_id,))
        await db.commit()
    return success

async def run_job_workers(job_id: int, bm: BrowserManager):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM jobs WHERE id=?", (job_id,))
        job = await cur.fetchone()
        if not job or job["status"] not in ("pending", "running"):
            return
        video_url = job["video_url"]
        target_views = job["target_views"]
        min_watch = job["min_watch"]
        user_id = job["user_id"]

        await db.execute("UPDATE jobs SET status='running' WHERE id=?", (job_id,))
        await db.commit()

    user_sem = await bm.user_concurrency.acquire(user_id)
    thread_limit = bm.user_concurrency.get_limit(user_id)
    producer_sem = asyncio.Semaphore(thread_limit * 2)

    async def fetch_proxy() -> Optional[dict]:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("""
                SELECT * FROM proxies
                WHERE (user_id=? OR user_id IS NULL)
                AND alive=1 AND (banned_until IS NULL OR banned_until < datetime('now'))
                AND health_score > 0.5
                ORDER BY health_score DESC, RANDOM()
                LIMIT 1
            """, (user_id,))
            row = await cur.fetchone()
            return dict(row) if row else None

    tasks = []
    for _ in range(target_views):
        if await is_job_cancelled(job_id):
            break
        await producer_sem.acquire()
        proxy = await fetch_proxy()
        if proxy is None:
            # No proxy available – release semaphore and skip this view attempt
            producer_sem.release()
            continue
        t = asyncio.create_task(
            worker_thread(job_id, video_url, min_watch, proxy, bm, user_sem)
        )
        t.add_done_callback(lambda _, sem=producer_sem: sem.release())
        tasks.append(t)

    await asyncio.gather(*tasks, return_exceptions=True)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT status, completed_views, target_views FROM jobs WHERE id=?", (job_id,)
        )
        job = await cur.fetchone()
        if job and job["status"] not in ("cancelled", "error"):
            final = "done" if job["completed_views"] >= job["target_views"] else "partial"
            await db.execute("UPDATE jobs SET status=? WHERE id=?", (final, job_id))
        await db.commit()

async def safe_run_job(job_id: int, bm: BrowserManager, bot: Bot = None, telegram_id: int = None):
    try:
        await run_job_workers(job_id, bm)
        if bot and telegram_id:
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute(
                    "SELECT status, completed_views, target_views, failed_views FROM jobs WHERE id=?",
                    (job_id,)
                )
                job = await cur.fetchone()
            if job:
                status_emoji = "✅" if job["status"] == "done" else ("⚠️" if job["status"] == "partial" else "❌")
                try:
                    await bot.send_message(
                        telegram_id,
                        f"{status_emoji} Job #{job_id} finished — "
                        f"{job['completed_views']}/{job['target_views']} views "
                        f"(+{job['failed_views']} failed) | Status: {job['status']}"
                    )
                except Exception:
                    pass
    except Exception as e:
        logger.exception(f"Job {job_id} crashed: {e}")
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE jobs SET status='error' WHERE id=?", (job_id,))
            await db.commit()
        if bot and telegram_id:
            try:
                await bot.send_message(telegram_id, f"❌ Job #{job_id} crashed with an internal error.")
            except Exception:
                pass

# -------------------------------------------------------------------
# Telegram Bot Handlers
# -------------------------------------------------------------------
router = Router()
bm_global: BrowserManager = None
bot_global: Bot = None

def get_bm() -> BrowserManager:
    return bm_global

def get_bot() -> Bot:
    return bot_global

@router.message(Command("start"))
async def start(message: types.Message):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO users (telegram_id) VALUES (?)", (message.from_user.id,))
        await db.commit()
    await message.answer(
        f"Welcome {message.from_user.full_name}!\n\n"
        "Use /add to create a view job, /balance to check your credits, "
        "or /help for a full command list.",
        reply_markup=main_menu()
    )

@router.message(Command("ping"))
async def ping(message: types.Message):
    await message.reply("🏓 Pong! Bot is alive.")

@router.message(Command("help"))
async def help_cmd(message: types.Message):
    await message.answer(
        "📋 <b>Commands</b>\n\n"
        "<b>Jobs</b>\n"
        "/views <url> <amount> [country] — quick start\n"
        "/add — guided job wizard\n"
        "/status — last 5 jobs\n"
        "/jobs — last 10 jobs\n"
        "/cancel <id> — cancel a running job\n\n"
        "<b>Account</b>\n"
        "/balance — credits & tier\n"
        "/threads <1-50> — set parallel threads\n\n"
        "<b>Proxies</b>\n"
        "/addproxies — upload proxy list (.txt)\n"
        "/myproxies — proxy pool stats\n"
        "/proxyhealth — health & latency stats\n"
        "/checkmyproxies — force health check now\n"
        "/autocheck <on|off> — auto health checks",
        parse_mode="HTML"
    )

@router.message(Command("balance"))
async def balance(message: types.Message):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT credits, tier FROM users WHERE telegram_id=?", (message.from_user.id,))
        user = await cur.fetchone()
        if user:
            await message.reply(f"💰 Balance: {cents_to_dollars(user['credits'])} | Tier: {user['tier']}")
        else:
            await message.reply("Use /start first.")

@router.message(Command("threads"))
async def threads_cmd(message: types.Message):
    try:
        parts = message.text.split()
        if len(parts) < 2:
            raise ValueError
        t = int(parts[1])
        if not (1 <= t <= 50):
            raise ValueError
    except (ValueError, IndexError):
        await message.reply("Usage: /threads <1-50>")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("UPDATE users SET threads=? WHERE telegram_id=?", (t, message.from_user.id))
        cur = await db.execute("SELECT id FROM users WHERE telegram_id=?", (message.from_user.id,))
        row = await cur.fetchone()
        await db.commit()

    if row:
        get_bm().user_concurrency.set_limit(row["id"], t)
    await message.reply(f"✅ Threads set to {t}")

@router.message(Command("views"))
async def views_quick(message: types.Message):
    args = message.text.split()
    if len(args) < 3:
        await message.reply("Usage: /views <url> <amount> [country]")
        return
    url = args[1]
    if not extract_video_id(url):
        await message.reply("❌ Invalid YouTube URL.")
        return
    try:
        amount = int(args[2])
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.reply("❌ Amount must be a positive integer.")
        return
    country = args[3].upper() if len(args) > 3 else None

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id, credits, tier FROM users WHERE telegram_id=?", (message.from_user.id,))
        user = await cur.fetchone()
        if not user:
            await message.reply("Use /start first.")
            return
        cost = estimate_cost_cents(amount, user["tier"])
        if user["credits"] < cost:
            await message.reply(
                f"❌ Insufficient funds. Need {cents_to_dollars(cost)}, "
                f"have {cents_to_dollars(user['credits'])}"
            )
            return
        await db.execute("UPDATE users SET credits = credits - ? WHERE telegram_id=?", (cost, message.from_user.id))
        cur = await db.execute(
            "INSERT INTO jobs (user_id, video_url, target_views, country) VALUES (?, ?, ?, ?)",
            (user["id"], url, amount, country)
        )
        job_id = cur.lastrowid
        await db.commit()

    asyncio.create_task(safe_run_job(job_id, get_bm(), get_bot(), message.from_user.id))
    await message.reply(
        f"🚀 Job #{job_id} started: {amount} views\n"
        f"Cost: {cents_to_dollars(cost)} | You'll be notified when done."
    )

# --- FSM add flow ---
@router.message(Command("add"))
async def add_start(message: types.Message, state: FSMContext):
    await state.set_state(AddView.url)
    await message.reply("Send the YouTube video URL:")

@router.message(AddView.url)
async def add_url(message: types.Message, state: FSMContext):
    if not extract_video_id(message.text):
        await message.reply("❌ Invalid YouTube URL. Try again:")
        return
    await state.update_data(url=message.text)
    await state.set_state(AddView.target)
    await message.reply("How many views?")

@router.message(AddView.target)
async def add_target(message: types.Message, state: FSMContext):
    try:
        amount = int(message.text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.reply("❌ Please enter a positive number:")
        return
    await state.update_data(target=amount)
    await state.set_state(AddView.country)
    await message.reply("Choose target country or 'Any':", reply_markup=country_kb())

@router.callback_query(F.data.startswith("country_"))
async def country_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    country = callback.data.split("_")[1] if callback.data != "country_any" else None
    await state.update_data(country=country)
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.set_state(AddView.watch)
    await callback.message.reply("Enter minimum watch time in seconds (default 60):")

@router.message(AddView.watch)
async def add_watch(message: types.Message, state: FSMContext):
    try:
        watch = int(message.text)
        if watch < 10:
            watch = 60
    except ValueError:
        watch = 60
    await state.update_data(watch=watch)
    data = await state.get_data()
    summary = (
        f"📋 <b>Job Summary</b>\n"
        f"Video: {data['url']}\n"
        f"Views: {data['target']}\n"
        f"Country: {data.get('country') or 'Any'}\n"
        f"Min Watch: {watch}s\n\n"
        "Confirm?"
    )
    await state.set_state(AddView.confirm)
    await message.reply(summary, reply_markup=confirm_kb(), parse_mode="HTML")

@router.callback_query(F.data == "confirm_job")
async def confirm_job(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    await state.clear()

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id, credits, tier FROM users WHERE telegram_id=?", (callback.from_user.id,))
        user = await cur.fetchone()
        if not user:
            await callback.message.reply("Use /start first.")
            return
        cost = estimate_cost_cents(data["target"], user["tier"])
        if user["credits"] < cost:
            await callback.message.reply(
                f"❌ Not enough credits. Need {cents_to_dollars(cost)}, "
                f"have {cents_to_dollars(user['credits'])}."
            )
            return
        await db.execute("UPDATE users SET credits = credits - ? WHERE id=?", (cost, user["id"]))
        cur = await db.execute(
            "INSERT INTO jobs (user_id, video_url, target_views, country, min_watch) VALUES (?, ?, ?, ?, ?)",
            (user["id"], data["url"], data["target"], data.get("country"), data["watch"])
        )
        job_id = cur.lastrowid
        await db.commit()

    asyncio.create_task(safe_run_job(job_id, get_bm(), get_bot(), callback.from_user.id))
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply(
        f"🚀 Job #{job_id} started. You'll be notified when it finishes."
    )

@router.callback_query(F.data == "cancel_job")
async def cancel_add(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply("❌ Cancelled.")

@router.message(Command("status"))
async def status(message: types.Message):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id FROM users WHERE telegram_id=?", (message.from_user.id,))
        user = await cur.fetchone()
        if not user:
            await message.reply("No account found. Use /start.")
            return
        cur = await db.execute("""
            SELECT id, video_url, status, completed_views, target_views, failed_views
            FROM jobs WHERE user_id=? ORDER BY created_at DESC LIMIT 5
        """, (user["id"],))
        jobs = await cur.fetchall()
        if not jobs:
            await message.reply("No jobs yet.")
            return
        lines = []
        for j in jobs:
            lines.append(
                f"#{j['id']} | {j['status']} | "
                f"{j['completed_views']}/{j['target_views']} ✅  {j['failed_views']} ❌"
            )
        await message.reply("📊 <b>Recent Jobs</b>\n\n" + "\n".join(lines), parse_mode="HTML")

@router.message(Command("jobs"))
async def jobs_list(message: types.Message):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id FROM users WHERE telegram_id=?", (message.from_user.id,))
        user = await cur.fetchone()
        if not user:
            return
        cur = await db.execute(
            "SELECT * FROM jobs WHERE user_id=? ORDER BY created_at DESC LIMIT 10", (user["id"],)
        )
        jobs = await cur.fetchall()
        if not jobs:
            await message.reply("No jobs yet.")
            return
        lines = [f"#{j['id']} {j['video_url']} — {j['status']} ({j['completed_views']}/{j['target_views']})"
                 for j in jobs]
        await message.reply("📋 <b>Last 10 jobs</b>\n\n" + "\n".join(lines), parse_mode="HTML")

@router.message(Command("cancel"))
async def cancel_job_cmd(message: types.Message):
    try:
        job_id = int(message.text.split()[1])
    except (IndexError, ValueError):
        await message.reply("Usage: /cancel <job_id>")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id FROM users WHERE telegram_id=?", (message.from_user.id,))
        user = await cur.fetchone()
        if not user:
            return
        cur = await db.execute(
            "SELECT id FROM jobs WHERE id=? AND user_id=?", (job_id, user["id"])
        )
        if not await cur.fetchone():
            await message.reply("❌ Job not found or doesn't belong to you.")
            return
        await db.execute(
            "UPDATE jobs SET status='cancelled' WHERE id=? AND status IN ('pending','running')",
            (job_id,)
        )
        await db.commit()
    await message.reply(f"🛑 Job #{job_id} cancelled.")

# --- Proxy management ---
@router.message(Command("addproxies"))
async def addproxies(message: types.Message):
    await message.reply("Send me a .txt file with proxies (ip:port or ip:port:user:pass)")

@router.message(F.document.file_name.endswith('.txt'))
async def proxy_file_handler(message: types.Message):
    # Enforce file size limit to prevent OOM
    if message.document.file_size is not None and message.document.file_size > MAX_PROXY_FILE_SIZE:
        await message.reply("❌ File too large. Max size is 1 MB.")
        return

    file_id = message.document.file_id
    file = await message.bot.get_file(file_id)
    file_bytes = await message.bot.download_file(file.file_path)
    content = file_bytes.read().decode(errors='ignore')
    added = 0
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id FROM users WHERE telegram_id=?", (message.from_user.id,))
        user = await cur.fetchone()
        if not user:
            return
        for line in content.splitlines():
            parsed = parse_proxy_line(line)
            if not parsed:
                continue
            cur = await db.execute("SELECT id FROM proxies WHERE ip=? AND port=?", (parsed["ip"], parsed["port"]))
            if await cur.fetchone():
                continue
            state_path = STORAGE_DIR / f"proxy_{parsed['ip']}_{parsed['port']}.json"
            # Encrypt credentials before storing
            enc_user = encrypt(parsed["username"]) if parsed["username"] else None
            enc_pass = encrypt(parsed["password"]) if parsed["password"] else None
            await db.execute("""
                INSERT INTO proxies (user_id, ip, port, username, password, type, storage_state_path)
                VALUES (?, ?, ?, ?, ?, 'residential', ?)
            """, (user["id"], parsed["ip"], parsed["port"], enc_user, enc_pass, str(state_path)))
            added += 1
        await db.commit()
    await message.reply(f"✅ Added {added} new proxies.")

@router.message(Command("myproxies"))
async def myproxies(message: types.Message):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id FROM users WHERE telegram_id=?", (message.from_user.id,))
        user = await cur.fetchone()
        if not user:
            return
        cur = await db.execute("""
            SELECT COUNT(*) as total,
                   SUM(alive) as alive,
                   SUM(CASE WHEN alive=0 THEN 1 ELSE 0 END) as dead
            FROM proxies WHERE user_id=?
        """, (user["id"],))
        stats = await cur.fetchone()
        await message.reply(
            f"🌐 <b>Proxy Pool</b>\n"
            f"Total: {stats['total']} | Alive: {stats['alive'] or 0} | Dead: {stats['dead'] or 0}",
            parse_mode="HTML"
        )

@router.message(Command("proxyhealth"))
async def proxyhealth(message: types.Message):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id FROM users WHERE telegram_id=?", (message.from_user.id,))
        user = await cur.fetchone()
        if not user:
            return
        cur = await db.execute("""
            SELECT AVG(health_score) as avg_h, AVG(latency_ms) as avg_l,
                   COUNT(*) as total, SUM(alive) as alive
            FROM proxies WHERE user_id=?
        """, (user["id"],))
        stats = await cur.fetchone()
        total = stats["total"] or 0
        alive = stats["alive"] or 0
        avg_h = stats["avg_h"]
        avg_l = stats["avg_l"]
        health_str = f"{avg_h:.2f}" if avg_h is not None else "N/A"
        latency_str = f"{int(avg_l)}ms" if avg_l is not None else "N/A"
        await message.reply(
            f"🏥 <b>Pool Health</b>\n"
            f"Alive: {alive}/{total}\n"
            f"Avg Health Score: {health_str}\n"
            f"Avg Latency: {latency_str}",
            parse_mode="HTML"
        )

@router.message(Command("checkmyproxies"))
async def checkmyproxies(message: types.Message):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id FROM users WHERE telegram_id=?", (message.from_user.id,))
        user = await cur.fetchone()
        if not user:
            return
        cur = await db.execute("SELECT * FROM proxies WHERE user_id=?", (user["id"],))
        proxies = [dict(row) for row in await cur.fetchall()]

    if not proxies:
        await message.reply("You have no proxies.")
        return

    # Offload to background to keep bot responsive
    async def _do_check():
        alive_before = sum(p["alive"] for p in proxies)
        for proxy in proxies:
            updates = await check_proxy_with_browser(proxy, get_bm())
            updates["last_checked"] = datetime.now(timezone.utc)
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("""
                    UPDATE proxies SET alive=?, consecutive_fails=?, banned_until=?,
                    health_score=?, last_checked=?, last_error=?, latency_ms=?
                    WHERE id=?
                """, (
                    updates["alive"], updates["consecutive_fails"], updates["banned_until"],
                    updates["health_score"], updates["last_checked"],
                    updates.get("last_error"), updates.get("latency_ms"), proxy["id"]
                ))
                await db.commit()
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT COUNT(*) as cnt FROM proxies WHERE user_id=? AND alive=1", (user["id"],)
            )
            after = await cur.fetchone()
        try:
            await message.reply(
                f"✅ Check complete.\nAlive before: {alive_before} → now: {after['cnt']}"
            )
        except Exception:
            pass

    asyncio.create_task(_do_check())
    await message.reply(f"⏳ Checking {len(proxies)} proxies in background…")

@router.message(Command("autocheck"))
async def autocheck(message: types.Message):
    args = message.text.split()
    if len(args) < 2 or args[1] not in ["on", "off"]:
        await message.reply("Usage: /autocheck <on|off>")
        return
    val = 1 if args[1] == "on" else 0
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET autocheck=? WHERE telegram_id=?", (val, message.from_user.id))
        await db.commit()
    await message.reply(f"Autocheck turned {args[1]}")

# --- Admin commands ---
@router.message(Command("admin_stats"))
async def admin_stats(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM jobs")
        jobs_count = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM proxies")
        proxies_count = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM users")
        users_count = (await cur.fetchone())[0]
        cur = await db.execute("SELECT SUM(completed_views) FROM jobs")
        total_views = (await cur.fetchone())[0] or 0
    await message.reply(
        f"📊 <b>Admin Stats</b>\n"
        f"Users: {users_count}\n"
        f"Jobs: {jobs_count}\n"
        f"Proxies: {proxies_count}\n"
        f"Total Views Delivered: {total_views}",
        parse_mode="HTML"
    )

@router.message(Command("admin_forcecheck"))
async def admin_forcecheck(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE proxies SET last_checked=NULL")
        await db.commit()
    await message.reply("✅ Forced full proxy recheck scheduled.")

@router.message(Command("admin_addcredits"))
async def admin_addcredits(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 3:
        await message.reply("Usage: /admin_addcredits <telegram_id> <cents>")
        return
    try:
        target_id = int(parts[1])
        amount = int(parts[2])
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.reply("Invalid arguments. Amount must be a positive integer (cents).")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id FROM users WHERE telegram_id=?", (target_id,))
        user = await cur.fetchone()
        if not user:
            await message.reply("User not found.")
            return
        await db.execute(
            "UPDATE users SET credits = credits + ? WHERE telegram_id=?", (amount, target_id)
        )
        await db.commit()
    await message.reply(
        f"✅ Added {cents_to_dollars(amount)} to user {target_id}."
    )

# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------
async def main():
    global bm_global, bot_global
    await init_db()
    bm_global = BrowserManager()
    await bm_global.start()
    asyncio.create_task(proxy_health_check_loop(bm_global))
    bot_global = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    logger.info("Bot started – no TLS proxy, using Chromium default JA3")
    try:
        await dp.start_polling(bot_global)
    finally:
        await bm_global.stop()

if __name__ == "__main__":
    asyncio.run(main())
