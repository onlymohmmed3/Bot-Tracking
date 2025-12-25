import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiohttp
import json
from datetime import datetime, timedelta, timezone
import asyncio
import os
from typing import Dict, Any, List, Optional
from dotenv import load_dotenv
import re

try:
    import qrcode
except ImportError:
    qrcode = None


playwright_browser = None

# ─────────────────────────────
# Logging (Rich + Emojis – بدون تكرار)
# ─────────────────────────────
import logging
import os
from rich.logging import RichHandler

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    # نخلي الرسالة فقط، و Rich يضيف الوقت + level
    format="%(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[RichHandler(
        rich_tracebacks=True,
        show_path=False,   # ما يكتب bot.py:line
    )],
)

# أسماء المستويات مع إيموجي
logging.addLevelName(logging.DEBUG,   "DEBUG 🧪")
logging.addLevelName(logging.INFO,    "INFO  ℹ️ ")
logging.addLevelName(logging.WARNING, "WARN  ⚠️")
logging.addLevelName(logging.ERROR,   "ERROR ❌")
logging.addLevelName(logging.CRITICAL,"FATAL 💥")

logger = logging.getLogger("parcelsbot")

# نخفف الضجة من مكتبات ثانية
for name in ("discord", "discord.client", "discord.gateway", "aiohttp"):
    logging.getLogger(name).setLevel(logging.WARNING)


# ─────────────────────────────
# Environment & setup
# ─────────────────────────────
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
TRACKINGMORE_API_KEY = os.getenv("TRACKINGMORE_API_KEY")

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing in .env")

TRACKINGMORE_BASE_URL = "https://api.trackingmore.com/v4"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "tracking_data.json")
GUILD_CONFIG_FILE = os.path.join(BASE_DIR, "guild_config.json")


def _flag(name: str, default: str = "true") -> bool:
    val = os.getenv(name, default)
    if val is None:
        val = default
    return str(val).lower() in ("1", "true", "yes", "on")


# Feature flags من .env
ENABLE_TRACK_ORDER = _flag("ENABLE_TRACK_ORDER", "true")
ENABLE_MY_ORDERS = _flag("ENABLE_MY_ORDERS", "true")
ENABLE_ORDER_DETAILS = _flag("ENABLE_ORDER_DETAILS", "true")
ENABLE_DELETE_ORDER = _flag("ENABLE_DELETE_ORDER", "true")
ENABLE_SET_REFRESH_INTERVAL = _flag("ENABLE_SET_REFRESH_INTERVAL", "true")
ENABLE_ETA_STATS = _flag("ENABLE_ETA_STATS", "true")
ENABLE_AUTO_UPDATES = _flag("ENABLE_AUTO_UPDATES", "true")

# فلاغات إضافية
ENABLE_SHARE_ORDER = _flag("ENABLE_SHARE_ORDER", "true")
ENABLE_UNSHARE_ORDER = _flag("ENABLE_UNSHARE_ORDER", "true")
ENABLE_TRACKING_SCREENSHOT_CMD = _flag("ENABLE_TRACKING_SCREENSHOT_CMD", "true")
ENABLE_SHARE_ALL_ORDERS = _flag("ENABLE_SHARE_ALL_ORDERS", "true")
ENABLE_UNSHARE_ALL_ORDERS = _flag("ENABLE_UNSHARE_ALL_ORDERS", "true")
ENABLE_DELETE_DMS = _flag("ENABLE_DELETE_DMS", "true")

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

tracking_data: Dict[str, List[Dict[str, Any]]] = {}
guild_config: Dict[str, Dict[str, Any]] = {}

DEFAULT_REFRESH_INTERVAL_MINUTES = 0.016  # حوالي ثانية واحدة (1/60 دقيقة) لتحديث فوري
ORDER_DELIVERY_DAYS = 8

BAHRAIN_TZ = timezone(timedelta(hours=3))
UTC = timezone.utc


def utcnow() -> datetime:
    """Timezone-aware UTC now."""
    return datetime.now(UTC)


# ─────────────────────────────
# Status model
# ─────────────────────────────
STAGES = [
    "processing",
    "warehouse",
    "transit",
    "customs",
    "warehouse_dest",
    "out_for_delivery",
    "delivered",
]

STATUS_TEXT = {
    "processing":       "Processing",
    "warehouse":        "In warehouse",
    "transit":          "In transit",
    "customs":          "Customs",  # سيتم تحديثه ديناميكياً مع اسم الدولة
    "warehouse_dest":   "Warehouse",  # سيتم تحديثه ديناميكياً مع اسم الدولة
    "out_for_delivery": "Out for delivery",
    "delivered":        "Delivered",
}

STATUS_COLORS = {
    "processing":       discord.Color.yellow(),
    "warehouse":        discord.Color.orange(),
    "transit":          discord.Color.blue(),
    "customs":          discord.Color.purple(),
    "warehouse_dest":   discord.Color.teal(),
    "out_for_delivery": discord.Color.blurple(),
    "delivered":        discord.Color.green(),
}

STAGE_EMOJIS = {
    "processing": "⏳",
    "warehouse": "📦",
    "transit": "✈️",
    "customs": "🛃",
    "warehouse_dest": "🏢",
    "out_for_delivery": "🚚",
    "delivered": "✅",
}


STAGE_ICONS = {
    "processing": "🟡",
    "warehouse": "🟠",
    "transit": "🔵",
    "customs": "🟣",
    "warehouse_dest": "🟤",
    "out_for_delivery": "🟢",
    "delivered": "🟩",
}

COMPANY_COLORS: Dict[str, discord.Color] = {
    "aliexpress": discord.Color.orange(),
    "cainiao":    discord.Color.blue(),
    "temu":       discord.Color.green(),
    "shein":      discord.Color.purple(),
    "dhl":        discord.Color.gold(),
    "aramex":     discord.Color.red(),
}

COMPANY_LOGOS: Dict[str, str] = {
    "aliexpress": "https://your-cdn.com/logos/aliexpress.png",
    "cainiao":    "https://your-cdn.com/logos/cainiao.png",
    "temu":       "https://your-cdn.com/logos/temu.png",
    "shein":      "https://your-cdn.com/logos/shein.png",
    "dhl":        "https://your-cdn.com/logos/dhl.png",
    "aramex":     "https://your-cdn.com/logos/aramex.png",
}

ESTIMATED_DAYS_SIM: Dict[str, int] = {
    "aliexpress": 14,
    "cainiao": 14,
    "temu": 10,
    "shein": 9,
    "dhl": 5,
    "aramex": 7,
}

COMPANY_TO_COURIER_CODE: Dict[str, str] = {
    "aliexpress": "cainiao",
    "cainiao": "cainiao",
    "global_cainiao": "cainiao",
    "temu": "cainiao",
    "shein": "cainiao",
    "dhl": "dhl",
    "aramex": "aramex",
}

COMPANY_DISPLAY: Dict[str, str] = {
    "aliexpress": "AliExpress",
    "cainiao":    "Cainiao",
    "temu":       "Temu",
    "shein":      "SHEIN",
    "dhl":        "DHL",
    "aramex":     "Aramex",
}

DEFAULT_LOCATIONS: List[str] = [
    "Order received",
    "Main warehouse",
    "On the way",
    "Local distribution center",
    "Delivered",
]

COMPANY_LOCATIONS: Dict[str, List[str]] = {
    "aliexpress": [
        "Seller warehouse - China",
        "International sorting center",
        "In air to destination country",
        "Local distribution center",
        "Delivered to customer",
    ],
    "cainiao": [
        "Cainiao origin hub",
        "Linehaul warehouse",
        "In transit to destination country",
        "Destination sorting center",
        "Delivered",
    ],
    "temu": [
        "Temu warehouse - China",
        "Order preparation center",
        "In transit to carrier",
        "Local distribution center",
        "Delivered",
    ],
    "shein": [
        "SHEIN warehouse - China",
        "International sorting center",
        "On the way to your country",
        "Local SHEIN warehouse",
        "Delivered",
    ],
    "dhl": [
        "Shipment picked up",
        "DHL origin facility",
        "In air to destination",
        "DHL destination facility",
        "Delivered",
    ],
    "aramex": [
        "Shipment picked up",
        "Aramex main hub",
        "On the way to your city",
        "Aramex local facility",
        "Delivered",
    ],
}

COUNTRY_NAME_BY_ISO2: Dict[str, str] = {
    "CN": "China",
    "BH": "Bahrain",
    "SA": "Saudi Arabia",
    "AE": "United Arab Emirates",
    "US": "United States",
    "GB": "United Kingdom",
    "DE": "Germany",
    "FR": "France",
    "ES": "Spain",
    "IT": "Italy",
    "NL": "Netherlands",
    "TR": "Turkey",
    "QA": "Qatar",
    "KW": "Kuwait",
    "OM": "Oman",
    "JO": "Jordan",
    "EG": "Egypt",
}

COUNTRY_FLAG_BY_NAME: Dict[str, str] = {
    "China": "🇨🇳",
    "Bahrain": "🇧🇭",
    "Saudi Arabia": "🇸🇦",
    "United Arab Emirates": "🇦🇪",
    "United States": "🇺🇸",
    "United Kingdom": "🇬🇧",
    "Germany": "🇩🇪",
    "France": "🇫🇷",
    "Spain": "🇪🇸",
    "Italy": "🇮🇹",
    "Netherlands": "🇳🇱",
    "Turkey": "🇹🇷",
    "Qatar": "🇶🇦",
    "Kuwait": "🇰🇼",
    "Oman": "🇴🇲",
    "Jordan": "🇯🇴",
    "Egypt": "🇪🇬",
}


def country_to_flag(name: Optional[str]) -> str:
    if not name:
        return "🌍"
    return COUNTRY_FLAG_BY_NAME.get(name, "🌍")


def iso2_to_country_name(code: Optional[str]) -> Optional[str]:
    if not code:
        return None
    code = code.upper()
    return COUNTRY_NAME_BY_ISO2.get(code, code)


def convert_to_bahrain_datetime_str(dt_raw: str) -> str:
    if not dt_raw:
        return dt_raw

    s = dt_raw.strip()

    try:
        iso_candidate = s.replace(" ", "T").replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso_candidate)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        dt_bh = dt.astimezone(BAHRAIN_TZ)
        return dt_bh.strftime("%d-%m-%Y %I:%M %p")
    except Exception:
        pass

    m = re.match(
        r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})\s+GMT([+-])(\d{2})(\d{2})?",
        s,
    )
    if m:
        date_part, time_part, sign, hh, mm = m.groups()
        mm = mm or "00"
        hours = int(hh)
        minutes = int(mm)
        offset = timedelta(hours=hours, minutes=minutes)
        if sign == "-":
            offset = -offset
        src_tz = timezone(offset)

        dt = datetime.fromisoformat(f"{date_part}T{time_part}")
        dt = dt.replace(tzinfo=src_tz)
        dt_bh = dt.astimezone(BAHRAIN_TZ)
        return dt_bh.strftime("%d-%m-%Y %I:%M %p")

    try:
        dt = datetime.fromisoformat(s.replace(" ", "T"))
        dt = dt.replace(tzinfo=UTC)
        dt_bh = dt.astimezone(BAHRAIN_TZ)
        return dt_bh.strftime("%d-%m-%Y %I:%M %p")
    except Exception:
        return s


TRACK_LINK_TEMPLATES: Dict[str, str] = {
    "aliexpress": "https://global.cainiao.com/detail.htm?mailNoList={tn}",
    "cainiao": "https://global.cainiao.com/detail.htm?mailNoList={tn}",
    "temu": "https://global.cainiao.com/detail.htm?mailNoList={tn}",
    "shein": "https://global.cainiao.com/detail.htm?mailNoList={tn}",
    "dhl": "https://www.dhl.com/global-en/home/tracking.html?tracking-id={tn}",
    "aramex": "https://www.aramex.com/track/shipments/{tn}",
}


def make_tracking_link(company: str, tracking_number: str) -> Optional[str]:
    comp = (company or "").lower()
    tpl = TRACK_LINK_TEMPLATES.get(comp)
    if not tpl:
        return None
    return tpl.format(tn=tracking_number)



def generate_tracking_qr(company: str, tracking_number: str) -> Optional[str]:
    """
    توليد QR Code للرابط الرسمي للتتبع (اختياري).
    يرجع مسار الصورة لو نجح، أو None لو فشل/المكتبة غير موجودة.
    """
    if qrcode is None:
        return None

    url = make_tracking_link(company, tracking_number)
    if not url:
        return None

    qr_dir = os.path.join(BASE_DIR, "qrs")
    os.makedirs(qr_dir, exist_ok=True)

    filename = f"{tracking_number}.png"
    path = os.path.join(qr_dir, filename)

    try:
        img = qrcode.make(url)
        img.save(path)
        return path
    except Exception:
        return None


# ─────────────────────────────
# Screenshot helper (Playwright)
# ─────────────────────────────
async def capture_tracking_screenshot(url: str, filename: str) -> Optional[str]:
    global playwright_browser
    if playwright_browser is None:
        return None

    screenshots_dir = os.path.join(BASE_DIR, "screenshots")
    os.makedirs(screenshots_dir, exist_ok=True)
    full_path = os.path.join(screenshots_dir, filename)

    try:
        page = await playwright_browser.new_page(viewport={"width": 1280, "height": 720})

        # افتح صفحة التتبع
        await page.goto(url, wait_until="networkidle", timeout=60000)

        # نحاول نضغط زر الكوكيز قبل ما نصوّر
        try:
            # نعطي الصفحة ثانيتين عشان يحمل البانر
            await asyncio.sleep(2)

            # 1) نحاول عن طريق role + الاسم
            try:
                await page.get_by_role(
                    "button",
                    name=re.compile(r"accept cookies", re.IGNORECASE),
                ).click(timeout=5000)
            except Exception:
                # 2) نحاول عن طريق نص الزر
                try:
                    await page.locator("text=Accept Cookies").first.click(timeout=5000)
                except Exception:
                    # 3) محاولة أخيرة لأي زر فيه كلمة Accept
                    await page.locator("button:has-text('Accept')").first.click(timeout=5000)
        except Exception:
            # لو ما لقيناه نتجاهل الخطأ ونكمّل
            pass

        # نعطي الصفحة لحظات بعد الضغط عشان يختفي البانر
        await asyncio.sleep(1)

        # الآن نلتقط السكرين شوت بدون البانر
        await page.screenshot(path=full_path, full_page=True)
        await page.close()

        return full_path
    except Exception as e:
        logger.error(f"Screenshot error: {e}")
        return None


# ─────────────────────────────
# Storage helpers
# ─────────────────────────────
def load_tracking_data() -> None:
    global tracking_data
    if not os.path.exists(DATA_FILE):
        tracking_data = {}
        return

    try:
        # لو الملف فاضي تمامًا، نعتبره فارغ بدون ما نرمي خطأ
        if os.path.getsize(DATA_FILE) == 0:
            tracking_data = {}
            return

        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        tracking_data = {str(k): v for k, v in data.items()}
    except json.JSONDecodeError:
        # JSON خربان → نعيد ضبطه
        logger.warning("tracking_data.json is invalid; resetting file.")
        tracking_data = {}
        save_tracking_data()
    except Exception as e:
        logger.error(f"Failed to load tracking data: {e}")
        tracking_data = {}


def save_tracking_data() -> None:
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(tracking_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to save tracking data: {e}")


def load_guild_config() -> None:
    global guild_config
    if not os.path.exists(GUILD_CONFIG_FILE):
        guild_config = {}
        return
    try:
        with open(GUILD_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        guild_config = {str(k): v for k, v in data.items()}
    except Exception as e:
        logger.error(f"Failed to load guild config: {e}")
        guild_config = {}


def save_guild_config() -> None:
    try:
        with open(GUILD_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(guild_config, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to save guild config: {e}")


# ─────────────────────────────
# Misc helpers
# ─────────────────────────────
def get_stage_index(stage: str) -> int:
    try:
        return STAGES.index(stage)
    except ValueError:
        return 0


def get_status_text_with_country(stage: str, pkg: Dict[str, Any] = None) -> str:
    """الحصول على نص الحالة مع اسم الدولة إذا كانت customs أو warehouse_dest"""
    base_text = STATUS_TEXT.get(stage, stage)
    
    if stage in ("customs", "warehouse_dest") and pkg:
        # محاولة الحصول على اسم دولة الوجهة
        dest_country = (
            pkg.get("destination_country_name") or 
            pkg.get("location") or 
            ""
        )
        
        # إذا كانت location تحتوي على اسم دولة، استخدمها
        if dest_country and dest_country.lower() not in ["unknown country", "not set", ""]:
            # تنظيف اسم الدولة (إزالة أي معلومات إضافية)
            country_name = dest_country.split(",")[0].strip()
            if stage == "customs":
                return f"Customs {country_name}"
            elif stage == "warehouse_dest":
                return f"Warehouse {country_name}"
    
    return base_text


def get_default_locations(company: str) -> List[str]:
    return COMPANY_LOCATIONS.get(company.lower(), DEFAULT_LOCATIONS)


def build_progress_bar_by_stage(stage: str, pkg: Dict[str, Any] = None) -> str:
    idx = get_stage_index(stage)
    total = max(len(STAGES) - 1, 1)
    ratio = idx / total
    blocks = 12
    filled = int(round(ratio * blocks))
    
    # Use different block characters for better visual
    if stage == "delivered":
        bar = "█" * blocks
    else:
        bar = "█" * filled + "░" * (blocks - filled)
    
    percent = int(round(ratio * 100))
    emoji = STAGE_EMOJIS.get(stage, "📦")
    status_text = get_status_text_with_country(stage, pkg) if pkg else STATUS_TEXT.get(stage, stage)
    
    # Add stage number indicator
    stage_num = f"{idx + 1}/{len(STAGES)}"
    
    return f"[{bar}] **{percent}%** ({stage_num}) – {emoji} {status_text}"


def build_stage_line(stage: str, pkg: Dict[str, Any] = None) -> str:
    """Build a clean, professional stage display with vertical layout"""
    lines = []
    current_idx = get_stage_index(stage)
    
    for idx, s in enumerate(STAGES):
        emoji = STAGE_EMOJIS.get(s, "•")
        label = STATUS_TEXT.get(s, s)
        
        # Get status text with country if available
        if pkg and s in ("customs", "warehouse_dest"):
            label = get_status_text_with_country(s, pkg)
        
        # Determine stage status and format
        if idx < current_idx:
            # Completed stages - show as done
            lines.append(f"`✓` {emoji} ~~{label}~~")
        elif idx == current_idx:
            # Current stage - highlight with indicator
            lines.append(f"`▶` {emoji} **{label}**")
        else:
            # Future stages - show as pending
            lines.append(f"`○` {emoji} {label}")
    
    return "\n".join(lines)


def build_route_line(pkg: Dict[str, Any]) -> str:
    hops: List[str] = pkg.get("hop_countries") or []
    stage = pkg.get("stage", "processing")
    current_location = pkg.get("location") or ""

    if not hops:
        origin = pkg.get("origin_country_name") or "Origin"
        dest = pkg.get("destination_country_name") or "Destination"
        
        # إذا كانت الحالة customs أو warehouse_dest، الموقع الحالي هو دولة الوجهة
        if stage in ("customs", "warehouse_dest", "out_for_delivery", "delivered"):
            if current_location and current_location != "Unknown country":
                # استخدم الموقع الحالي إذا كان محدداً
                return f"{country_to_flag(origin)} {origin} → {country_to_flag(current_location)} __**{current_location}**__"
            else:
                return f"{country_to_flag(origin)} {origin} → {country_to_flag(dest)} __**{dest}**__"
        else:
            return f"{country_to_flag(origin)} {origin} → {country_to_flag(dest)} {dest}"

    stage_idx = get_stage_index(stage)
    total_stage = max(len(STAGES) - 1, 1)
    stage_ratio = stage_idx / total_stage

    # إذا كانت الحالة customs أو warehouse_dest، الموقع الحالي هو آخر دولة في hops (عادة الوجهة)
    if stage in ("customs", "warehouse_dest", "out_for_delivery", "delivered"):
        if current_location and current_location != "Unknown country" and current_location in hops:
            current_idx = hops.index(current_location)
        else:
            # استخدم آخر دولة في hops (عادة الوجهة)
            current_idx = len(hops) - 1
    else:
        if len(hops) == 1:
            current_idx = 0
        else:
            current_idx = int(round(stage_ratio * (len(hops) - 1)))

    parts = []
    for i, c in enumerate(hops):
        flag = country_to_flag(c)
        name = c
        if i == current_idx:
            parts.append(f"__**{flag} {name}**__")
        else:
            parts.append(f"{flag} {name}")
    return " → ".join(parts)

async def ensure_order_categories(guild: discord.Guild) -> Dict[str, int]:
    guild_id = str(guild.id)
    conf = guild_config.get(guild_id, {})

    in_cat = guild.get_channel(conf.get("in_progress_category_id")) if conf.get("in_progress_category_id") else None
    done_cat = guild.get_channel(conf.get("done_category_id")) if conf.get("done_category_id") else None

    if not in_cat:
        in_cat = discord.utils.get(guild.categories, name="Orders - In Progress")
    if not done_cat:
        done_cat = discord.utils.get(guild.categories, name="Orders - Completed")

    if not in_cat:
        in_cat = await guild.create_category("Orders - In Progress")
    if not done_cat:
        done_cat = await guild.create_category("Orders - Completed")

    guild_config[guild_id] = {
        "in_progress_category_id": in_cat.id,
        "done_category_id": done_cat.id,
    }
    save_guild_config()
    return guild_config[guild_id]


def make_channel_base_name(company: str, tracking_number: str, nickname: str = "") -> str:
    if nickname:
        base = nickname.lower().strip()
        base = base.replace(" ", "-")
        base = re.sub(r"[^a-z0-9\-]", "-", base)
        base = re.sub(r"-+", "-", base).strip("-")
        if not base:
            base = f"{company}-{tracking_number}".lower()
    else:
        base = f"{company}-{tracking_number}".lower()

    base = re.sub(r"[^a-z0-9\-]", "-", base)
    base = re.sub(r"-+", "-", base).strip("-")
    if not base:
        base = "order"
    return f"order-{base}"[:90]


def make_channel_display_name(pkg: Dict[str, Any]) -> str:
    stage = pkg.get("stage", "processing")
    emoji = STAGE_EMOJIS.get(stage, "📦")
    company = pkg.get("company", "order")
    tracking_number = pkg.get("tracking_number", "unknown")
    nickname = pkg.get("nickname") or ""
    base = make_channel_base_name(company, tracking_number, nickname)
    name = f"{emoji} {base}"
    return name[:100]


async def rename_channel_for_stage(pkg: Dict[str, Any]) -> None:
    guild_id = pkg.get("guild_id")
    channel_id = pkg.get("channel_id")
    if not guild_id or not channel_id:
        return

    guild = bot.get_guild(int(guild_id))
    if not guild:
        return
    channel = guild.get_channel(int(channel_id))
    if not isinstance(channel, discord.TextChannel):
        return

    new_name = make_channel_display_name(pkg)
    if channel.name != new_name:
        try:
            await channel.edit(name=new_name)
        except Exception as e:
            print(f"⚠️ Failed to rename channel {channel.id}: {e}")


async def move_channel_if_delivered(pkg: Dict[str, Any]) -> None:
    if pkg.get("stage") != "delivered":
        return

    guild_id = pkg.get("guild_id")
    channel_id = pkg.get("channel_id")
    if not guild_id or not channel_id:
        return

    guild = bot.get_guild(int(guild_id))
    if not guild:
        return

    conf = await ensure_order_categories(guild)
    done_cat = guild.get_channel(conf["done_category_id"])
    channel = guild.get_channel(int(channel_id))

    if channel and isinstance(done_cat, discord.CategoryChannel):
        if channel.category_id != done_cat.id:
            await channel.edit(category=done_cat)


# ─────────────────────────────
# Latest Tracking Number extractor
# ─────────────────────────────
def extract_latest_tracking_number(tracking: Any, original: str) -> Optional[str]:
    orig_digits = "".join(ch for ch in original if ch.isdigit())
    candidates: set[str] = set()

    def add_from_string(text: str):
        lower = text.lower()
        if "latest tracking number" not in lower and "latest tracking#" not in lower and "latest tracking" not in lower:
            return
        for m in re.findall(r"\d{8,}", text):
            if m != orig_digits:
                candidates.add(m)

    def walk(obj: Any, depth: int = 0):
        if depth > 6:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                kl = str(k).lower()
                if isinstance(v, str):
                    if ("latest" in kl and "tracking" in kl) or ("new" in kl and "tracking" in kl):
                        for m in re.findall(r"\d{8,}", v):
                            if m != orig_digits:
                                candidates.add(m)
                    else:
                        add_from_string(v)
                if isinstance(v, (dict, list)):
                    walk(v, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                walk(item, depth + 1)

    walk(tracking)

    if not candidates:
        return None

    return sorted(candidates, key=lambda x: (len(x), x))[-1]


# ─────────────────────────────
# TrackingMore API
# ─────────────────────────────
async def fetch_tracking_info_from_api(tracking_number: str, company: str) -> Optional[Dict[str, Any]]:
    api_key = TRACKINGMORE_API_KEY
    if not api_key:
        return None

    headers = {
        "Content-Type": "application/json",
        "Tracking-Api-Key": api_key,
    }

    comp = (company or "cainiao").lower()
    courier_code = COMPANY_TO_COURIER_CODE.get(comp, comp)

    async with aiohttp.ClientSession() as session:
        params = {
            "tracking_numbers": tracking_number,
            "items_amount": 1,
            "lang": "en",
        }
        if courier_code:
            params["courier_code"] = courier_code

        url_get = f"{TRACKINGMORE_BASE_URL}/trackings/get"
        try:
            async with session.get(url_get, headers=headers, params=params, timeout=20) as resp:
                data = await resp.json()
        except Exception as e:
            logger.error(f"TrackingMore GET failed: {e}")
            return None

        meta = data.get("meta", {})
        trackings = data.get("data") or []

        if meta.get("code") != 200 or not trackings:
            payload = {
                "tracking_number": tracking_number,
                "language": "en",
            }
            if courier_code:
                payload["courier_code"] = courier_code

            url_create = f"{TRACKINGMORE_BASE_URL}/trackings/create"
            try:
                async with session.post(url_create, headers=headers, json=payload, timeout=20) as resp:
                    create_data = await resp.json()
            except Exception as e:
                logger.error(f"TrackingMore CREATE failed: {e}")
                return None

            if create_data.get("meta", {}).get("code") != 200:
                print(f"⚠️ TrackingMore CREATE error: {create_data.get('meta')}")
                return None

            tracking = create_data.get("data") or {}
        else:
            tracking = trackings[0]

    orig_digits = "".join(ch for ch in tracking_number if ch.isdigit())
    latest_tn = extract_latest_tracking_number(tracking, tracking_number)

    delivery_status = (tracking.get("delivery_status") or "").lower()

    # تحديد الحالة الأولية من delivery_status (لكن سيتم تحديثها من timeline events لاحقاً)
    if delivery_status == "delivered":
        stage = "delivered"
    elif delivery_status in ("outfordelivery", "pickup"):
        stage = "out_for_delivery"
    elif delivery_status in ("transit", "intransit"):
        stage = "transit"
    elif delivery_status in ("info_received", "inforeceived", "package_arrived_at_sort_facility"):
        stage = "warehouse"
    elif delivery_status in ("pending", "notfound"):
        stage = "processing"
    else:
        stage = "processing"
    
    # ملاحظة: سيتم تحديث stage من timeline events لاحقاً (الأولوية لـ timeline events)

    origin_info = tracking.get("origin_info") or {}
    dest_info = tracking.get("destination_info") or {}

    events_raw: List[Any] = []
    for ev in origin_info.get("trackinfo") or []:
        events_raw.append(("origin", ev))
    for ev in dest_info.get("trackinfo") or []:
        events_raw.append(("destination", ev))

    def _event_key(item: Any) -> str:
        _src, ev = item
        return (
            ev.get("checkpoint_date")
            or ev.get("Date")
            or ev.get("date")
            or ""
        )

    events_raw = sorted(events_raw, key=_event_key)

    event_lines: List[str] = []
    last_source: Optional[str] = None
    last_event_country_str: Optional[str] = None
    last_event_location_raw: Optional[str] = None
    first_event_dt_raw: Optional[str] = None
    hop_countries: List[str] = []

    for src, ev in events_raw:
        detail = (
            ev.get("tracking_detail")
            or ev.get("StatusDescription")
            or ev.get("status_description")
            or ev.get("Details")
            or ev.get("details")
            or ev.get("raw_status")
            or ""
        )

        event_country = (
            ev.get("country_name")
            or ev.get("CountryName")
            or ""
        )

        loc = (
            ev.get("location")
            or ev.get("city")
            or ev.get("checkpoint_location")
            or ""
        )

        dt = (
            ev.get("checkpoint_date")
            or ev.get("Date")
            or ev.get("date")
            or ""
        )

        carrier_note = ev.get("carrier_note") or ev.get("description") or ""

        new_tn = (
            ev.get("tracking_number")
            or ev.get("TrackingNumber")
            or ev.get("tracking_no")
            or ev.get("new_tracking_number")
        )
        if new_tn:
            d = "".join(ch for ch in new_tn if ch.isdigit())
            if d and d != orig_digits:
                latest_tn = d

        if dt and not first_event_dt_raw:
            first_event_dt_raw = dt

        display_dt = convert_to_bahrain_datetime_str(dt) if dt else ""

        detail = detail.strip()
        main_text = detail if detail else (loc if loc else "Status update")
        if display_dt:
            main = f"{display_dt} - {main_text}"
        else:
            main = main_text

        if event_country:
            last_event_country_str = event_country

        if carrier_note:
            cn_lower = carrier_note.lower()
            for cname in COUNTRY_NAME_BY_ISO2.values():
                if cname and cname.lower() in cn_lower:
                    last_event_country_str = cname
                    break

        if loc:
            last_event_location_raw = loc

        if carrier_note:
            line = f"{main}\nCarrier note: {carrier_note.strip()}"
        else:
            line = main

        line = line.strip()
        if not line:
            continue

        cname = event_country or last_event_country_str
        if cname:
            cname_clean = cname.strip()
            if not hop_countries or hop_countries[-1] != cname_clean:
                hop_countries.append(cname_clean)

        event_lines.append(line)
        last_source = src

    origin_iso = tracking.get("origin_country_iso2") or tracking.get("origin_country")
    dest_iso = tracking.get("destination_country_iso2") or tracking.get("destination_country")

    origin_name = iso2_to_country_name(origin_iso)
    dest_name = iso2_to_country_name(dest_iso)

    location_country: Optional[str] = None

    if stage in ("out_for_delivery", "delivered"):
        location_country = (
            dest_name
            or last_event_country_str
            or last_event_location_raw
            or origin_name
        )
    else:
        if last_event_country_str:
            location_country = last_event_country_str
        elif last_source == "destination":
            location_country = last_event_location_raw or dest_name or origin_name
        elif last_source == "origin":
            location_country = origin_name or dest_name or last_event_location_raw
        else:
            location_country = dest_name or origin_name or last_event_location_raw

    if not location_country:
        location_country = "Unknown country"

    eta = (
        tracking.get("scheduled_delivery_date")
        or tracking.get("order_promised_delivery_date")
        or ""
    )

    if not event_lines:
        event_lines.append("No detailed events from carrier yet.")

    pickup_raw = tracking.get("pickup_date") or ""
    if not pickup_raw:
        pickup_raw = first_event_dt_raw or ""

    pickup_date = ""
    if pickup_raw:
        pickup_bh_str = convert_to_bahrain_datetime_str(pickup_raw)
        pickup_date = pickup_bh_str.split(" ", 1)[0]

    last_detail_text = ""
    if event_lines:
        last_detail_text = event_lines[-1].lower()
    
    # تعريف الكلمات المفتاحية (يتم استخدامها في عدة أماكن)
    delivered_keywords = [
        "package delivered",
        "delivered",
        "delivery completed",
        "successfully delivered",
        "delivered to customer",
        "delivered to recipient",
        "delivery successful",
        "تم التسليم",  # عربي
        "已送达",  # صيني
    ]
    
    customs_keywords = [
        "import customs clearance complete",
        "import customs clearance started",
        "import clearance start",
        "arrived at linehaul office",
        "arrived at linehual office",  # typo في API
        "customs clearance",
        "customs",
    ]
    
    warehouse_dest_keywords = [
        "arrived in transit country/region",
        "arrive at transit country or district",
        "received by local delivery company",
        "bahrain station 4】arrived",
        "bahrain station 3】arrived",
        "bahrain station 2】arrived",
        "bahrain station 1】arrived",
        "station 4】arrived",
        "station 3】arrived",
        "station 2】arrived",
        "station 1】arrived",
        "arrived at destination country/region sorting center",
    ]
    
    out_for_delivery_keywords = [
        "out for delivery",
        "outfordelivery",
        "on the way to recipient",
    ]
    
    # تحقق ذكي من الحالة من أحدث التحديثات
    # للبحث عن الحالات المختلفة في التحديثات حتى لو API لم يحدث delivery_status
    if event_lines:
        # فحص آخر حدث أولاً (الأهم)
        last_event_text = event_lines[-1].lower() if event_lines else ""
        
        # تحقق من آخر 3 تحديثات للبحث عن الحالات المختلفة
        recent_events_text = " ".join(event_lines[-3:]).lower()
        
        # فحص جميع الأحداث (للحالات المهمة جداً مثل delivered)
        all_events_text = " ".join(event_lines).lower()
        
        # تحديد دولة الوجهة من dest_name
        dest_country_name = dest_name or ""
        
        # تحقق من الحالات بالترتيب (من الأحدث إلى الأقدم)
        # 1. Delivered (أعلى أولوية) - فحص شامل
        # فحص آخر حدث أولاً (الأهم) - هذا الأسرع
        if any(keyword in last_event_text for keyword in delivered_keywords):
            stage = "delivered"
            logger.debug(f"🚨 Detected delivered from LAST event: {last_event_text[:100]}")
        # فحص آخر 3 أحداث
        elif any(keyword in recent_events_text for keyword in delivered_keywords):
            stage = "delivered"
            logger.debug(f"Detected delivered from recent events")
        # فحص جميع الأحداث (للحالات المهمة جداً)
        elif any(keyword in all_events_text for keyword in ["package delivered", "delivered"]):
            stage = "delivered"
            logger.debug(f"Detected delivered from all timeline")
        # 2. Out for delivery (فقط إذا لم يكن delivered)
        elif stage != "delivered" and any(keyword in recent_events_text for keyword in out_for_delivery_keywords):
            stage = "out_for_delivery"
        # 3. Warehouse (في دولة الوجهة)
        elif stage != "delivered" and any(keyword in recent_events_text for keyword in warehouse_dest_keywords):
            stage = "warehouse_dest"
        # 4. Customs
        elif stage != "delivered" and any(keyword in recent_events_text for keyword in customs_keywords):
            stage = "customs"
        # 5. إذا كان في transit ولكن يوجد إشارة إلى warehouse في دولة الوجهة
        elif stage == "transit" and any(keyword in recent_events_text for keyword in warehouse_dest_keywords):
            stage = "warehouse_dest"
        # 6. إذا كان في transit ولكن يوجد إشارة إلى customs
        elif stage == "transit" and any(keyword in recent_events_text for keyword in customs_keywords):
            stage = "customs"
    
    # تحقق إضافي من last_detail_text
    if stage == "transit" and last_detail_text:
        if any(keyword in last_detail_text for keyword in customs_keywords):
            stage = "customs"
        elif any(keyword in last_detail_text for keyword in warehouse_dest_keywords):
            stage = "warehouse_dest"
        elif "warehouse" in last_detail_text and not any(
            w in last_detail_text for w in ["in transit", "in air", "linehaul", "departed", "left country"]
        ):
            stage = "warehouse"

    # إذا كانت الحالة customs أو warehouse_dest، الموقع الحالي يجب أن يكون دولة الوجهة
    if stage in ("customs", "warehouse_dest") and dest_name:
        location_country = dest_name
    elif stage in ("customs", "warehouse_dest") and not location_country:
        # إذا لم تكن هناك دولة وجهة محددة، استخدم last_event_country_str أو dest_name
        location_country = last_event_country_str or dest_name or location_country

    if origin_name and (not hop_countries or hop_countries[0] != origin_name):
        hop_countries.insert(0, origin_name)
    if dest_name and (not hop_countries or hop_countries[-1] != dest_name):
        hop_countries.append(dest_name)

    return {
        "stage": stage,
        "location": location_country,
        "estimated_delivery": eta,
        "events": event_lines,
        "pickup_date": pickup_date,
        "latest_tracking": latest_tn,
        "delivery_status_raw": delivery_status,
        "hop_countries": hop_countries,
        "origin_country_name": origin_name,
        "destination_country_name": dest_name,
    }


# ─────────────────────────────
# Fallback simulation
# ─────────────────────────────
def simulate_tracking_info(pkg: Dict[str, Any]) -> Dict[str, Any]:
    added_at_str = pkg.get("added_at")
    if added_at_str:
        try:
            added_at = datetime.fromisoformat(added_at_str)
            if added_at.tzinfo is None:
                added_at = added_at.replace(tzinfo=UTC)
        except Exception:
            added_at = utcnow()
    else:
        added_at = utcnow()

    company = pkg.get("company", "").lower()
    est_days = ESTIMATED_DAYS_SIM.get(company, 10)
    total_hours = max(24.0, float(est_days * 24))
    elapsed_hours = (utcnow() - added_at).total_seconds() / 3600.0
    if elapsed_hours < 0:
        elapsed_hours = 0.0

    ratio = min(elapsed_hours / total_hours, 1.0)
    if ratio < 0.15:
        stage = "processing"
    elif ratio < 0.3:
        stage = "warehouse"
    elif ratio < 0.5:
        stage = "transit"
    elif ratio < 0.65:
        stage = "customs"
    elif ratio < 0.8:
        stage = "warehouse_dest"
    elif ratio < 0.95:
        stage = "out_for_delivery"
    else:
        stage = "delivered"

    locations = get_default_locations(company)
    idx = get_stage_index(stage)
    location = locations[idx] if idx < len(locations) else locations[-1]

    now_str = datetime.now(BAHRAIN_TZ).strftime("%d-%m-%Y %I:%M %p")
    detail = STATUS_TEXT.get(stage, stage)
    event_text = f"{now_str} - Simulated update: {detail}"

    eta = pkg.get("estimated_delivery")
    if not eta:
        eta = (added_at + timedelta(days=est_days)).date().isoformat()

    return {
        "stage": stage,
        "location": location,
        "estimated_delivery": eta,
        "events": [event_text],
    }


# ─────────────────────────────
# Update one package
# ─────────────────────────────
def _extract_date_from_last_timeline(pkg: Dict[str, Any]) -> Optional[str]:
    timeline = pkg.get("timeline") or []
    if not timeline:
        return None
    last_line = timeline[-1]
    m = re.search(r"(\d{4}-\d{2}-\d{2})", last_line)
    if not m:
        return None
    return m.group(1)


async def update_package_status(pkg: Dict[str, Any]) -> bool:
    if not pkg.get("active", True) and pkg.get("stage") == "delivered":
        pkg["_stage_changed"] = False
        pkg["_last_update_changed"] = False
        return False

    old_stage = pkg.get("stage", "processing")
    old_timeline = list(pkg.get("timeline") or [])
    old_last_event = old_timeline[-1] if old_timeline else None

    # ⚡ فحص سريع من timeline events الموجودة أولاً (قبل استدعاء API)
    # هذا يساعد في اكتشاف التغييرات بسرعة أكبر
    existing_timeline = pkg.get("timeline", [])
    if existing_timeline and old_stage != "delivered":
        # تعريف الكلمات المفتاحية (شامل جداً)
        delivered_keywords = [
            "package delivered",
            "delivered",
            "delivery completed",
            "successfully delivered",
            "delivered to customer",
            "delivered to recipient",
            "delivery successful",
            "تم التسليم",
            "已送达",
        ]
        
        # فحص آخر حدث أولاً (الأهم)
        last_event_text = existing_timeline[-1].lower() if existing_timeline else ""
        recent_events_text = " ".join(existing_timeline[-5:]).lower()  # فحص آخر 5 أحداث
        all_events_text = " ".join(existing_timeline).lower()
        
        # فحص شامل - حتى لو كان جزء من النص
        # مثال: "Package delivered. Carrier note: ..." أو "delivered" في أي مكان
        if any(keyword in last_event_text for keyword in delivered_keywords):
            pkg["stage"] = "delivered"
            pkg["status_index"] = get_stage_index("delivered")
            if old_stage != "delivered":
                logger.info(f"🚨 FAST CHECK: Detected delivered from LAST event (before API call) for {pkg['tracking_number']}")
                logger.info(f"   Last event: {last_event_text[:150]}")
                pkg["_stage_changed"] = True
                # استخراج تاريخ التسليم
                if not pkg.get("delivered_date"):
                    d_str = _extract_date_from_last_timeline(pkg)
                    if d_str:
                        pkg["delivered_date"] = d_str
                pkg["active"] = False
                save_tracking_data()
                return True
        elif any(keyword in recent_events_text for keyword in delivered_keywords):
            pkg["stage"] = "delivered"
            pkg["status_index"] = get_stage_index("delivered")
            if old_stage != "delivered":
                logger.info(f"🚨 FAST CHECK: Detected delivered from recent events (before API call) for {pkg['tracking_number']}")
                pkg["_stage_changed"] = True
                if not pkg.get("delivered_date"):
                    d_str = _extract_date_from_last_timeline(pkg)
                    if d_str:
                        pkg["delivered_date"] = d_str
                pkg["active"] = False
                save_tracking_data()
                return True
        elif any(keyword in all_events_text for keyword in ["package delivered", "delivered"]):
            pkg["stage"] = "delivered"
            pkg["status_index"] = get_stage_index("delivered")
            if old_stage != "delivered":
                logger.info(f"🚨 FAST CHECK: Detected delivered from all timeline (before API call) for {pkg['tracking_number']}")
                pkg["_stage_changed"] = True
                if not pkg.get("delivered_date"):
                    d_str = _extract_date_from_last_timeline(pkg)
                    if d_str:
                        pkg["delivered_date"] = d_str
                pkg["active"] = False
                save_tracking_data()
                return True

    # استدعاء API للحصول على التحديثات الجديدة
    # ملاحظة: قد لا يعيد API التحديثات الجديدة بسرعة، لذا نعتمد على timeline events أيضاً
    api_info = await fetch_tracking_info_from_api(
        pkg["tracking_number"], pkg.get("company", "")
    )
    using_api = api_info is not None
    
    # إذا كان API لا يعيد معلومات، نحاول مرة أخرى بعد قليل
    # لكن أولاً نفحص timeline events من API إذا كانت موجودة

    if using_api:
        info = api_info
    else:
        info = simulate_tracking_info(pkg)

    api_delivery_status = ""
    if using_api:
        api_delivery_status = (info.get("delivery_status_raw") or "").lower()

    new_stage = info.get("stage", old_stage)

    # التأكد من الحفاظ على delivered إذا كان كذلك
    if old_stage == "delivered":
        new_stage = "delivered"
    # التأكد من تحديث الحالة إلى delivered إذا كان API يقول ذلك
    if api_delivery_status == "delivered":
        new_stage = "delivered"
    
    # تحقق إضافي من التحديثات الجديدة والموجودة للبحث عن جميع المراحل
    new_events = info.get("events", [])
    
    # تعريف الكلمات المفتاحية (نفس المستخدمة في fetch_tracking_info_from_api)
    delivered_keywords = [
        "package delivered",
        "delivered",
        "delivery completed",
        "successfully delivered",
        "delivered to customer",
        "delivered to recipient",
        "delivery successful",
        "【bahrain station 4】 delivered",
        "station 4】 delivered",
        "bahrain station 4】 delivered",
        "تم التسليم",
        "已送达",
    ]
    
    customs_keywords = [
        "import customs clearance complete",
        "import customs clearance started",
        "import clearance start",
        "arrived at linehaul office",
        "arrived at linehual office",
        "customs clearance",
        "customs",
    ]
    
    warehouse_dest_keywords = [
        "arrived in transit country/region",
        "arrive at transit country or district",
        "received by local delivery company",
        "bahrain station 4】arrived",
        "bahrain station 3】arrived",
        "bahrain station 2】arrived",
        "bahrain station 1】arrived",
        "station 4】arrived",
        "station 3】arrived",
        "station 2】arrived",
        "station 1】arrived",
        "arrived at destination country/region sorting center",
    ]
    
    out_for_delivery_keywords = [
        "out for delivery",
        "outfordelivery",
        "on the way to recipient",
    ]
    
    # فحص التحديثات الموجودة والجديدة معاً
    all_events_to_check = existing_timeline + new_events
    
    # فحص أقوى للكشف عن delivered - فحص آخر حدث أولاً (الأهم)
    if all_events_to_check:
        # فحص آخر حدث (الأحدث) - هذا الأهم
        last_event_text = all_events_to_check[-1].lower() if all_events_to_check else ""
        
        # فحص آخر 3 أحداث (للحالات المهمة)
        recent_events_text = " ".join(all_events_to_check[-3:]).lower()
        
        # فحص جميع الأحداث (للحالات المهمة جداً مثل delivered)
        all_events_text = " ".join(all_events_to_check).lower()
        
        # 1. Delivered (أعلى أولوية) - فحص شامل
        # فحص آخر حدث أولاً (الأهم)
        if any(keyword in last_event_text for keyword in delivered_keywords):
            new_stage = "delivered"
            logger.info(f"🚨 URGENT: Detected delivered status from LAST event for {pkg['tracking_number']}")
        # فحص آخر 3 أحداث
        elif any(keyword in recent_events_text for keyword in delivered_keywords):
            new_stage = "delivered"
            logger.info(f"Detected delivered status from recent events for {pkg['tracking_number']}")
        # فحص جميع الأحداث (للحالات المهمة جداً)
        elif any(keyword in all_events_text for keyword in ["package delivered", "delivered"]):
            new_stage = "delivered"
            logger.info(f"Detected delivered status from all timeline for {pkg['tracking_number']}")
        # 2. Out for delivery (فقط إذا لم يكن delivered)
        elif new_stage != "delivered" and any(keyword in recent_events_text for keyword in out_for_delivery_keywords):
            new_stage = "out_for_delivery"
            logger.info(f"Detected out_for_delivery status from timeline for {pkg['tracking_number']}")
        # 3. Warehouse (في دولة الوجهة)
        elif new_stage != "delivered" and any(keyword in recent_events_text for keyword in warehouse_dest_keywords):
            new_stage = "warehouse_dest"
        # 4. Customs
        elif new_stage != "delivered" and any(keyword in recent_events_text for keyword in customs_keywords):
            new_stage = "customs"
            logger.debug(f"Detected customs status from timeline for {pkg['tracking_number']}")

    pkg["stage"] = new_stage
    pkg["status_index"] = get_stage_index(new_stage)

    if using_api and info.get("latest_tracking"):
        pkg["latest_tracking_number"] = info["latest_tracking"]

    if using_api:
        pkg["hop_countries"] = info.get("hop_countries") or pkg.get("hop_countries", [])
        pkg["origin_country_name"] = info.get("origin_country_name") or pkg.get("origin_country_name")
        pkg["destination_country_name"] = info.get("destination_country_name") or pkg.get("destination_country_name")

    if using_api:
        pickup_date = info.get("pickup_date")
        if pickup_date and not pkg.get("order_date"):
            pkg["order_date"] = pickup_date
            try:
                d = datetime.fromisoformat(pickup_date).date()
            except Exception:
                d = None
            if d:
                pkg["estimated_delivery"] = (d + timedelta(days=ORDER_DELIVERY_DAYS)).isoformat()

    if info.get("location"):
        pkg["location"] = info["location"]

    # نتجاهل estimated_delivery القادمة من API ونستخدم منطقنا الخاص:
    # - في البداية: Order Date + ORDER_DELIVERY_DAYS
    # - عند الوصول لـ warehouse_dest / out_for_delivery: نجعلها غداً (بتوقيت البحرين)

    pkg.setdefault("timeline", [])
    
    # تحديث timeline events من API
    new_events_added = False
    for ev in info.get("events", []):
        if ev not in pkg["timeline"]:
            pkg["timeline"].append(ev)
            new_events_added = True
            logger.debug(f"Added new event to timeline: {ev[:100]}")

    if using_api:
        pkg["timeline"] = [
            line for line in pkg["timeline"]
            if "Simulated update" not in line
        ]
    
    # إذا تم إضافة أحداث جديدة، قم بفحصها فوراً للكشف عن delivered
    if new_events_added and new_stage != "delivered":
        updated_timeline = pkg.get("timeline", [])
        if updated_timeline:
            # فحص آخر حدث جديد
            last_event_text = updated_timeline[-1].lower() if updated_timeline else ""
            recent_events_text = " ".join(updated_timeline[-5:]).lower()  # فحص آخر 5 أحداث
            all_timeline_text = " ".join(updated_timeline).lower()
            
            delivered_keywords = [
                "package delivered",
                "delivered",
                "delivery completed",
                "successfully delivered",
                "delivered to customer",
                "delivered to recipient",
                "delivery successful",
            ]
            
            # فحص شامل
            if any(keyword in last_event_text for keyword in delivered_keywords):
                new_stage = "delivered"
                logger.info(f"🚨 NEW EVENT: Detected delivered from LAST newly added event for {pkg['tracking_number']}")
                logger.info(f"   Event: {last_event_text[:150]}")
            elif any(keyword in recent_events_text for keyword in delivered_keywords):
                new_stage = "delivered"
                logger.info(f"🚨 NEW EVENT: Detected delivered from newly added recent events for {pkg['tracking_number']}")
            elif any(keyword in all_timeline_text for keyword in ["package delivered", "delivered"]):
                new_stage = "delivered"
                logger.info(f"🚨 NEW EVENT: Detected delivered from all newly added timeline for {pkg['tracking_number']}")
    
    # فحص إضافي: إذا كان آخر حدث "Out for delivery" وكان قد مر عليه وقت كافٍ (أكثر من 24 ساعة)
    # قد يعني أن الشحنة تم تسليمها لكن API لم يحدث الحالة
    if new_stage == "out_for_delivery" and existing_timeline:
        last_event = existing_timeline[-1] if existing_timeline else ""
        if "out for delivery" in last_event.lower():
            # محاولة استخراج التاريخ من آخر حدث
            try:
                # البحث عن تاريخ في آخر حدث (صيغة: YYYY-MM-DD HH:MM:SS)
                date_match = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}):(\d{2}):(\d{2})", last_event)
                if date_match:
                    event_date_str = f"{date_match.group(1)} {date_match.group(2)}:{date_match.group(3)}:{date_match.group(4)}"
                    try:
                        event_dt = datetime.strptime(event_date_str, "%Y-%m-%d %H:%M:%S")
                        # تحويل إلى UTC
                        if event_dt.tzinfo is None:
                            event_dt = event_dt.replace(tzinfo=UTC)
                        else:
                            event_dt = event_dt.astimezone(UTC)
                        
                        # حساب الوقت المنقضي
                        time_diff = utcnow() - event_dt
                        hours_passed = time_diff.total_seconds() / 3600
                        
                        # إذا مر أكثر من 24 ساعة على "Out for delivery"، قد تكون الشحنة تم تسليمها
                        if hours_passed > 24:
                            logger.warning(f"⚠️ Out for delivery for {hours_passed:.1f} hours - might be delivered but API not updated for {pkg['tracking_number']}")
                            # لا نغير الحالة تلقائياً، لكن نسجل تحذير
                    except Exception as e:
                        logger.debug(f"Could not parse date from event: {e}")
            except Exception:
                pass

    new_timeline = pkg["timeline"]
    new_last_event = new_timeline[-1] if new_timeline else None

    # فحص نهائي شامل من timeline المحدث للبحث عن جميع المراحل
    # هذا الفحص الأهم - يفحص جميع الأحداث للتأكد من عدم تفويت أي حالة
    if new_timeline:
        # فحص آخر حدث أولاً (الأهم)
        last_event_text = new_timeline[-1].lower() if new_timeline else ""
        
        # فحص آخر 5 أحداث (للتأكد من عدم تفويت أي شيء)
        final_timeline_text = " ".join(new_timeline[-5:]).lower()
        
        # فحص جميع الأحداث (للحالات المهمة جداً)
        all_timeline_text = " ".join(new_timeline).lower()
        
        # تحقق من الحالات بالترتيب (من الأحدث إلى الأقدم)
        # 1. Delivered (أعلى أولوية) - فحص شامل جداً
        # فحص آخر حدث أولاً (الأهم) - هذا الأسرع
        if new_stage != "delivered":
            if any(kw in last_event_text for kw in delivered_keywords):
                new_stage = "delivered"
                pkg["stage"] = "delivered"
                pkg["status_index"] = get_stage_index("delivered")
                logger.info(f"🚨🚨🚨 FINAL CHECK: Set delivered from LAST event for {pkg['tracking_number']}")
                logger.info(f"   Last event: {last_event_text[:200]}")
            # فحص آخر 5 أحداث
            elif any(kw in final_timeline_text for kw in delivered_keywords):
                new_stage = "delivered"
                pkg["stage"] = "delivered"
                pkg["status_index"] = get_stage_index("delivered")
                logger.info(f"🚨🚨 FINAL CHECK: Set delivered from recent 5 events for {pkg['tracking_number']}")
            # فحص جميع الأحداث (للحالات المهمة جداً) - هذا الأهم للتأكد
            elif any(kw in all_timeline_text for kw in ["package delivered", "delivered"]):
                new_stage = "delivered"
                pkg["stage"] = "delivered"
                pkg["status_index"] = get_stage_index("delivered")
                logger.info(f"🚨 FINAL CHECK: Set delivered from ALL timeline for {pkg['tracking_number']}")
                logger.info(f"   Timeline length: {len(new_timeline)} events")
        
        # 2. Out for delivery (فقط إذا لم يكن delivered)
        if new_stage != "delivered" and any(kw in final_timeline_text for kw in out_for_delivery_keywords):
            new_stage = "out_for_delivery"
            pkg["stage"] = "out_for_delivery"
            pkg["status_index"] = get_stage_index("out_for_delivery")
            logger.info(f"Final check: Set out_for_delivery status for {pkg['tracking_number']} from timeline")
        # 3. Warehouse (في دولة الوجهة)
        elif new_stage != "delivered" and any(kw in final_timeline_text for kw in warehouse_dest_keywords):
            new_stage = "warehouse_dest"
            pkg["stage"] = "warehouse_dest"
            pkg["status_index"] = get_stage_index("warehouse_dest")
        # 4. Customs
        elif new_stage != "delivered" and any(kw in final_timeline_text for kw in customs_keywords):
            new_stage = "customs"
            pkg["stage"] = "customs"
            pkg["status_index"] = get_stage_index("customs")
            logger.debug(f"Final check: Set customs status for {pkg['tracking_number']} from timeline")

    if new_stage == "delivered" and not pkg.get("delivered_date"):
        d_str = _extract_date_from_last_timeline(pkg)
        if d_str:
            pkg["delivered_date"] = d_str

    stage_changed = new_stage != old_stage
    last_update_changed = new_last_event != old_last_event

    pkg["_stage_changed"] = stage_changed
    pkg["_last_update_changed"] = last_update_changed

    if new_stage == "delivered":
        pkg["active"] = False

    # تأكيد وجود Order Date وتعيين ETA مبدئي (Order Date + ORDER_DELIVERY_DAYS)
    if not pkg.get("order_date"):
        added_at_str = pkg.get("added_at")
        if added_at_str:
            try:
                added_at_dt = datetime.fromisoformat(added_at_str)
                pkg["order_date"] = added_at_dt.date().isoformat()
            except Exception:
                added_at_dt = None
        else:
            added_at_dt = None
    else:
        try:
            added_at_dt = datetime.fromisoformat(pkg["order_date"])
        except Exception:
            added_at_dt = None

    # نحسب ETA الأساسية دائماً من Order Date + ORDER_DELIVERY_DAYS
    base_eta = None
    if added_at_dt:
        try:
            base_eta = (added_at_dt.date() + timedelta(days=ORDER_DELIVERY_DAYS)).isoformat()
        except Exception:
            base_eta = None

    # 🔁 منطق خاص: لما توصل الشحنة لمخزن البحرين / شركة التوصيل المحلية
    # إذا stage في warehouse_dest أو out_for_delivery → نخلي ETA = بكرة
    # غير كذا → نخليها على القاعدة الأساسية (Order Date + 8 أيام)
    try:
        eta_value = base_eta
        if new_stage in ("warehouse_dest", "out_for_delivery") and new_stage != "delivered":
            today_bh = datetime.now(BAHRAIN_TZ).date()
            tomorrow_bh = today_bh + timedelta(days=1)
            eta_value = tomorrow_bh.isoformat()

        if eta_value:
            pkg["estimated_delivery"] = eta_value
    except Exception:
        # أي خطأ هنا ما يوقف التحديث، بس نتجاهله
        pass

    return stage_changed or (new_timeline != old_timeline)


async def refresh_user_packages(user_id: str) -> None:
    changed = False
    for pkg in tracking_data.get(user_id, []):
        if pkg.get("active", True):
            c = await update_package_status(pkg)
            pkg.pop("_stage_changed", None)
            pkg.pop("_last_update_changed", None)
            if c:
                changed = True
    if changed:
        save_tracking_data()


# ─────────────────────────────
# Embeds
# ─────────────────────────────

class TrackingEmbed:
    @staticmethod
    def from_package(pkg: Dict[str, Any], compact: bool = False) -> discord.Embed:
        tracking_num = pkg["tracking_number"]
        nickname = pkg.get("nickname") or ""
        company = pkg.get("company", "").lower()
        stage = pkg.get("stage", "processing")
        status_text = get_status_text_with_country(stage, pkg)
        idx = get_stage_index(stage)

        locations = get_default_locations(company)
        default_location = locations[idx] if 0 <= idx < len(locations) else locations[-1]

        location = pkg.get("location") or default_location
        eta = pkg.get("estimated_delivery")
        
        # تنظيف قيمة ETA إذا كانت None أو "None"
        if not eta or str(eta).lower() == "none":
            eta = None
        else:
            # Improve ETA format لو عندنا قيمة مخزّنة
            try:
                eta_dt = datetime.fromisoformat(str(eta))
                if isinstance(eta_dt, datetime):
                    eta = eta_dt.astimezone(BAHRAIN_TZ).strftime("%d-%m-%Y")
                else:
                    eta = eta_dt.strftime("%d-%m-%Y")
            except Exception:
                # لو التاريخ مو بصيغة ISO نخليه كنص كما هو
                eta = str(eta)

        # لو ما زالت ETA فاضية، نحسبها من Order Date أو added_at (عرض فقط)
        if not eta:
            fallback_src = pkg.get("order_date") or pkg.get("added_at")
            if fallback_src:
                try:
                    # نحاول أولاً كـ ISO، ولو فشل نجرب dd-mm-YYYY
                    try:
                        base_dt = datetime.fromisoformat(fallback_src)
                    except Exception:
                        base_dt = datetime.strptime(fallback_src, "%d-%m-%Y")
                    eta_date = (base_dt.date() + timedelta(days=ORDER_DELIVERY_DAYS))
                    eta = eta_date.strftime("%d-%m-%Y")
                except Exception:
                    eta = "Not set"
            else:
                eta = "Not set"
        timeline: List[str] = pkg.get("timeline", [])
        active_flag = pkg.get("active", True)
        dm_enabled = pkg.get("dm_enabled", True)
        channel_notifications = pkg.get("channel_notifications", True)

        # Order date fallback logic - improve format
        order_date_str = pkg.get("order_date")
        if not order_date_str:
            added_at_str = pkg.get("added_at")
            if added_at_str:
                try:
                    added_dt = datetime.fromisoformat(added_at_str)
                    order_date_str = added_dt.astimezone(BAHRAIN_TZ).strftime("%d-%m-%Y")
                except Exception:
                    order_date_str = "Not set"
            else:
                order_date_str = "Not set"
        else:
            try:
                # Convert date format to Bahrain timezone
                order_dt = datetime.fromisoformat(order_date_str)
                if isinstance(order_dt, datetime):
                    order_date_str = order_dt.astimezone(BAHRAIN_TZ).strftime("%d-%m-%Y")
                else:
                    # If it's date only
                    order_date_str = order_dt.strftime("%d-%m-%Y")
            except Exception:
                pass

        # Title with better formatting
        stage_emoji = STAGE_EMOJIS.get(stage, "📦")
        title = f"{stage_emoji} Order Status"
        if nickname:
            title += f" • {nickname}"

        # استخدام لون الحالة (stage) مباشرة - كل حالة لها لونها الخاص
        color = STATUS_COLORS.get(stage, discord.Color.blurple())

        # Description with tracking numbers and time info
        desc = f"**Tracking Number:** `{tracking_num}`"
        latest_tn = pkg.get("latest_tracking_number")
        if latest_tn and latest_tn != tracking_num:
            desc += f"\n**Latest Tracking:** `{latest_tn}`"
        
        # Add elapsed time since order
        try:
            order_date_for_elapsed = pkg.get("order_date") or pkg.get("added_at")
            if order_date_for_elapsed:
                order_dt = datetime.fromisoformat(order_date_for_elapsed)
                if isinstance(order_dt, datetime):
                    order_dt = order_dt.replace(tzinfo=UTC) if order_dt.tzinfo is None else order_dt
                else:
                    order_dt = datetime.combine(order_dt, datetime.min.time()).replace(tzinfo=UTC)
                
                elapsed = (utcnow() - order_dt).days
                if elapsed >= 0:
                    desc += f"\n**⏱️ Days elapsed:** **{elapsed}** day{'s' if elapsed != 1 else ''}"
        except Exception:
            pass

        embed = discord.Embed(
            title=title,
            description=desc,
            color=color,
        )

        # Company thumbnail
        company_logo = COMPANY_LOGOS.get(company)
        if company_logo and company_logo.startswith("http"):
            embed.set_thumbnail(url=company_logo)

        display_company = COMPANY_DISPLAY.get(company, company.upper() or "Unknown")

        # ─────────────────────────────
        # Section: Order information
        # ─────────────────────────────
        embed.add_field(
            name="━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            value="**📦 Order Information**",
            inline=False,
        )

        embed.add_field(name="🏢 Company", value=f"**{display_company}**", inline=True)
        embed.add_field(
            name="📍 Current Status",
            value=f"{STAGE_EMOJIS.get(stage, '📦')} **{status_text}**",
            inline=True,
        )
        
        # Add days in current stage
        if not compact:
            try:
                timeline = pkg.get("timeline", [])
                if timeline:
                    # Find when current stage started
                    stage_started = None
                    for event in reversed(timeline):
                        event_lower = event.lower()
                        stage_keywords = {
                            "processing": ["processing", "order received", "seller"],
                            "warehouse": ["warehouse", "sorting center"],
                            "transit": ["transit", "in transit", "on the way", "departed"],
                            "customs": ["customs", "clearance"],
                            "warehouse_dest": ["warehouse", "arrived", "destination"],
                            "out_for_delivery": ["out for delivery", "delivery"],
                            "delivered": ["delivered"]
                        }
                        keywords = stage_keywords.get(stage, [])
                        if any(kw in event_lower for kw in keywords):
                            # Extract date from event
                            if " - " in event:
                                date_str = event.split(" - ")[0].strip()
                                try:
                                    if len(date_str) > 10:
                                        stage_started = datetime.strptime(date_str[:16], "%Y-%m-%d %H:%M")
                                    else:
                                        stage_started = datetime.strptime(date_str, "%Y-%m-%d")
                                    break
                                except:
                                    pass
                    if stage_started:
                        days_in_stage = (datetime.now(BAHRAIN_TZ) - stage_started.replace(tzinfo=UTC).astimezone(BAHRAIN_TZ)).days
                        if days_in_stage >= 0:
                            stage_duration = f"**{days_in_stage}** day{'s' if days_in_stage != 1 else ''}"
                            embed.add_field(name="⏱️ In current stage", value=stage_duration, inline=True)
            except Exception:
                pass

        if compact:
            embed.add_field(name="🌍 Location", value=f"**{location}**", inline=True)
            embed.add_field(name="📅 ETA", value=f"**{eta}**", inline=True)
        else:
            embed.add_field(name="🌍 Current Location", value=f"**{location}**", inline=False)
            embed.add_field(name="🗓️ Order Date", value=f"`{order_date_str}`", inline=True)
            embed.add_field(name="📅 Estimated Delivery", value=f"`{eta}`", inline=True)

        # ─────────────────────────────
        # Section: Tracking progress
        # ─────────────────────────────
        embed.add_field(
            name="━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            value="**📊 Tracking Progress**",
            inline=False,
        )

        embed.add_field(
            name="📊 Progress Status",
            value=build_progress_bar_by_stage(stage, pkg),
            inline=False,
        )

        if not compact:
            embed.add_field(
                name="📍 Stages",
                value=build_stage_line(stage, pkg),
                inline=False,
            )

            embed.add_field(
                name="🗺️ Route",
                value=build_route_line(pkg),
                inline=False,
            )

        # ─────────────────────────────
        # Timeline as a nice feed
        # ─────────────────────────────
        if timeline:
            recent_updates = timeline[-3:] if not compact else timeline[-1:]
            timeline_text = ""
            emojis = ["1️⃣", "2️⃣", "3️⃣"]

            for i, update_line in enumerate(reversed(recent_updates), 1):
                timestamp = ""
                location_part = ""
                note = ""

                # Extract timestamp + main text + carrier note
                if " - " in update_line:
                    parts = update_line.split(" - ", 1)
                    if len(parts) == 2:
                        timestamp = parts[0].strip()
                        rest = parts[1]
                        if "Carrier note:" in rest:
                            main, note_part = rest.split("Carrier note:", 1)
                            location_part = main.strip()
                            note = note_part.strip()
                        else:
                            location_part = rest.strip()
                else:
                    location_part = update_line.strip()

                # Detect update type and assign appropriate emoji
                update_lower = location_part.lower()
                update_emoji = "📦"  # Default (بداية الطلب / تحديثات عامة)
                
                # Check for different update types
                if any(kw in update_lower for kw in ["delivered", "تم التسليم", "已送达"]):
                    update_emoji = "✅"
                elif any(kw in update_lower for kw in ["customs", "clearance", "linehaul"]):
                    update_emoji = "🛃"
                elif any(kw in update_lower for kw in ["warehouse", "station", "sorting center"]):
                    # نفرق بين مخزن المصدر ومخزن دولة الوجهة
                    dest_keywords = [
                        "bahrain",
                        "saudi",
                        "saudi arabia",
                        "kuwait",
                        "uae",
                        "united arab emirates",
                        "qatar",
                        "oman",
                        "jordan",
                        "egypt",
                        "received by local delivery company",
                        "arrived at destination country/region sorting center",
                        "station 1】arrived",
                        "station 2】arrived",
                        "station 3】arrived",
                        "station 4】arrived",
                    ]
                    if any(kw in update_lower for kw in dest_keywords):
                        # مستودع في دولة الوجهة → 🏢
                        update_emoji = "🏢"
                    else:
                        # مستودع في بلد الإرسال → نخليه 📦 (افتراضي)
                        update_emoji = "📦"
                elif any(kw in update_lower for kw in ["out for delivery", "on the way to recipient"]):
                    update_emoji = "🚚"
                elif any(kw in update_lower for kw in ["departed", "in transit", "in air", "left"]):
                    update_emoji = "✈️"
                elif any(kw in update_lower for kw in ["processing", "received", "picked up"]):
                    update_emoji = "⏳"
                elif any(kw in update_lower for kw in ["arrived", "arrive"]):
                    update_emoji = "📍"

                emoji_num = emojis[i - 1] if i <= len(emojis) else "•"

                # Build the line with improved formatting
                # Add badge for important updates
                badge = ""
                if any(kw in update_lower for kw in ["delivered", "تم التسليم", "已送达"]):
                    badge = " 🎉"
                elif any(kw in update_lower for kw in ["customs", "clearance"]):
                    badge = " ⚠️"
                
                line = f"{emoji_num} {update_emoji}{badge}"
                
                if timestamp:
                    # Improve timestamp display - use new format
                    try:
                        # Try to convert timestamp to Bahrain timezone if not already
                        dt_parsed = None
                        if len(timestamp) > 10:
                            try:
                                dt_parsed = datetime.strptime(timestamp[:16], "%Y-%m-%d %H:%M")
                            except:
                                try:
                                    dt_parsed = datetime.strptime(timestamp[:10], "%Y-%m-%d")
                                except:
                                    pass
                        else:
                            try:
                                dt_parsed = datetime.strptime(timestamp, "%Y-%m-%d")
                            except:
                                pass
                        
                        if dt_parsed:
                            dt_bh = dt_parsed.replace(tzinfo=UTC).astimezone(BAHRAIN_TZ) if dt_parsed.tzinfo is None else dt_parsed.astimezone(BAHRAIN_TZ)
                            timestamp_formatted = dt_bh.strftime("%d-%m-%Y %I:%M %p")
                            
                            # Add relative time with better formatting
                            now_bh = datetime.now(BAHRAIN_TZ)
                            time_diff = now_bh - dt_bh
                            if time_diff.days > 0:
                                relative = f" *({time_diff.days}d ago)*"
                            elif time_diff.seconds >= 3600:
                                hours = time_diff.seconds // 3600
                                relative = f" *({hours}h ago)*"
                            elif time_diff.seconds >= 60:
                                minutes = time_diff.seconds // 60
                                relative = f" *({minutes}m ago)*"
                            else:
                                relative = " *(just now)*"
                            timestamp_formatted += relative
                        else:
                            timestamp_formatted = timestamp
                    except Exception:
                        timestamp_formatted = timestamp
                    line += f" `{timestamp_formatted}`"
                
                if location_part:
                    # Extract country name if present
                    country_emoji = ""
                    location_display = location_part[:70]
                    
                    # Try to extract and highlight country names
                    country_mapping = {
                        "bahrain": "Bahrain",
                        "china": "China",
                        "uae": "United Arab Emirates",
                        "united arab emirates": "United Arab Emirates",
                        "saudi": "Saudi Arabia",
                        "saudi arabia": "Saudi Arabia",
                        "kuwait": "Kuwait",
                        "qatar": "Qatar",
                        "oman": "Oman",
                        "jordan": "Jordan",
                        "egypt": "Egypt",
                    }
                    
                    location_lower = location_display.lower()
                    for keyword, country_name in country_mapping.items():
                        if keyword in location_lower:
                            flag = country_to_flag(country_name)
                            if flag != "🌍":  # Only add if we found a flag
                                country_emoji = f" {flag}"
                            break
                    
                    line += f" **{location_display}**{country_emoji}"
                
                if note:
                    # Improved carrier note formatting
                    note_clean = note[:80].strip()
                    line += f"\n   └─ 📝 *{note_clean}*"

                # Add separator between updates (except for the last one)
                if i < len(recent_updates):
                    timeline_text += line + "\n" + "─" * 40 + "\n\n"
                else:
                    timeline_text += line + "\n\n"

            field_name = "⏱️ Recent Updates" if len(recent_updates) > 1 else "⏱️ Last Update"
            embed.add_field(
                name=field_name,
                value=timeline_text.strip()[:1024],
                inline=False,
            )

        # ─────────────────────────────
        # Section: notification settings
        # ─────────────────────────────
        if not compact:
            embed.add_field(
                name="━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
                value="**⚙️ Settings & Controls**",
                inline=False,
            )

            active_text = "**Active** ✅" if active_flag else "**Paused** ⏸️"
            embed.add_field(name="⚙️ Tracking State", value=active_text, inline=True)

            dm_text = "**Enabled** 🔔" if dm_enabled else "**Disabled** 🔕"
            embed.add_field(name="🔔 DM Notifications", value=dm_text, inline=True)

            channel_text = "**Enabled** 📢" if channel_notifications else "**Disabled** 🔕"
            embed.add_field(name="📢 Channel Alerts", value=channel_text, inline=True)

        # ─────────────────────────────
        # Smart footer
        # ─────────────────────────────
        refresh_interval = pkg.get("refresh_interval_minutes", DEFAULT_REFRESH_INTERVAL_MINUTES)
        last_checked = pkg.get("last_checked_at")

        footer_parts: List[str] = []
        
        # Add last checked time with actual time
        if last_checked:
            try:
                last_dt = datetime.fromisoformat(last_checked.replace("Z", "+00:00"))
                # Convert to Bahrain time
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=UTC)
                last_dt_bh = last_dt.astimezone(BAHRAIN_TZ)
                # Format: DD-MM-YYYY HH:MM AM/PM
                time_str = last_dt_bh.strftime("%d-%m-%Y %I:%M %p")
                footer_parts.append(f"🕐 Updated: {time_str}")
            except Exception:
                footer_parts.append("🕐 Recently updated")
        else:
            footer_parts.append("🕐 Not updated yet")

        # Add refresh interval info
        footer_parts.append(f"🔄 Auto-refresh: {refresh_interval}m")

        footer_text = " • ".join(footer_parts)
        footer_text += " • 🇧🇭 Bahrain Time"
        
        embed.set_footer(text=footer_text, icon_url=None)
        
        # Add delivery probability if not delivered
        if stage != "delivered" and not compact:
            try:
                order_date_str = pkg.get("order_date") or pkg.get("added_at")
                if order_date_str and eta and eta != "Not set":
                    order_dt = datetime.fromisoformat(order_date_str)
                    if isinstance(order_dt, datetime):
                        order_dt = order_dt.replace(tzinfo=UTC) if order_dt.tzinfo is None else order_dt
                    else:
                        order_dt = datetime.combine(order_dt, datetime.min.time()).replace(tzinfo=UTC)
                    
                    try:
                        eta_date = datetime.fromisoformat(eta).date()
                        total_days = (eta_date - order_dt.date()).days
                        elapsed_days = (datetime.now(BAHRAIN_TZ).date() - order_dt.date()).days
                        
                        if total_days > 0 and elapsed_days >= 0:
                            progress_percent = min(100, int((elapsed_days / total_days) * 100))
                            if progress_percent < 100:
                                embed.add_field(
                                    name="📈 Delivery Progress",
                                    value=f"**{progress_percent}%** of estimated time elapsed",
                                    inline=False,
                                )
                    except Exception:
                        pass
            except Exception:
                pass

        # Shared users
        shared_users = pkg.get("shared_users") or []
        if shared_users and not compact:
            shared_mentions = ", ".join(f"<@{uid}>" for uid in shared_users[:5])
            if len(shared_users) > 5:
                shared_mentions += f" and {len(shared_users) - 5} more"
            embed.add_field(
                name="👥 Shared with",
                value=shared_mentions,
                inline=False,
            )

        return embed

class OrderView(discord.ui.View):
    def __init__(self, owner_id: int, tracking_number: str, company: str, guild_id: int, compact: bool = False):
        super().__init__(timeout=None)
        self.owner_id = owner_id
        self.tracking_number = tracking_number
        self.company = company
        self.guild_id = guild_id
        self.compact = compact
        

    def _find_package(self) -> Optional[Dict[str, Any]]:
        for _user_id, pkgs in tracking_data.items():
            for pkg in pkgs:
                if (
                    pkg.get("tracking_number") == self.tracking_number
                    and pkg.get("guild_id") == self.guild_id
                ):
                    return pkg
        return None

    async def _ensure_owner(self, interaction: discord.Interaction) -> bool:
        pkg = self._find_package()
        if not pkg:
            await interaction.response.send_message(
                "❌ Order not found.",
                ephemeral=True,
            )
            return False

        owner_id = pkg.get("owner_id", self.owner_id)
        shared_users = pkg.get("shared_users") or []
        allowed_ids = {owner_id} | set(shared_users)

        if interaction.user.id not in allowed_ids:
            await interaction.response.send_message(
                "❌ You are not allowed to control this order.",
                ephemeral=True,
            )
            return False
        return True

    # ─────────────────────────────
    # Row 1: Primary Actions
    # ─────────────────────────────
    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.primary, row=0)
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_owner(interaction):
            return

        pkg = self._find_package()
        if not pkg:
            await interaction.response.send_message(
                "❌ Order not found.",
                ephemeral=True,
            )
            return

        await update_package_status(pkg)
        pkg["last_checked_at"] = utcnow().isoformat()
        pkg.pop("_stage_changed", None)
        pkg.pop("_last_update_changed", None)
        save_tracking_data()
        await move_channel_if_delivered(pkg)
        await rename_channel_for_stage(pkg)

        embed = TrackingEmbed.from_package(pkg, compact=self.compact)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="🔗 Tracking page", style=discord.ButtonStyle.secondary, row=0)
    async def tracking_page_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_owner(interaction):
            return

        pkg = self._find_package()
        if not pkg:
            await interaction.response.send_message(
                "❌ Order not found.",
                ephemeral=True,
            )
            return

        url = make_tracking_link(pkg.get("company", ""), pkg["tracking_number"])
        if url:
            await interaction.response.send_message(
                f"🔗 Official tracking page:\n{url}",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "Could not build tracking URL. Please use the tracking number on the carrier's website.",
                ephemeral=True,
            )

    @discord.ui.button(label="📸 Screenshot", style=discord.ButtonStyle.secondary, row=0)
    async def screenshot_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_owner(interaction):
            return

        pkg = self._find_package()
        if not pkg:
            await interaction.response.send_message(
                "❌ Order not found.",
                ephemeral=True,
            )
            return

        url = make_tracking_link(pkg.get("company", ""), pkg["tracking_number"])
        if not url:
            await interaction.response.send_message(
                "❌ Could not build tracking URL for this company.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        filename = f"{pkg['tracking_number']}.png"
        path = await capture_tracking_screenshot(url, filename)
        if not path:
            await interaction.followup.send(
                "⚠ Failed to capture screenshot (Playwright not installed or error occurred).",
                ephemeral=True,
            )
            return

        file = discord.File(path, filename=filename)
        await interaction.followup.send(
            content="📸 Live snapshot of the official tracking page:",
            file=file,
            ephemeral=True,
        )

    # ─────────────────────────────
    # Row 2: Settings & Controls
    # ─────────────────────────────
    @discord.ui.button(label="🔔 Toggle DM", style=discord.ButtonStyle.secondary, row=1)
    async def toggle_dm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_owner(interaction):
            return

        pkg = self._find_package()
        if not pkg:
            await interaction.response.send_message(
                "❌ Order not found.",
                ephemeral=True,
            )
            return

        current = pkg.get("dm_enabled", True)
        new_state = not current
        pkg["dm_enabled"] = new_state
        save_tracking_data()

        embed = TrackingEmbed.from_package(pkg, compact=self.compact)
        await interaction.response.edit_message(embed=embed, view=self)

        state_text = "enabled ✅ (you will receive DM updates)" if new_state else "disabled ❌ (no DM updates)"
        await interaction.followup.send(
            f"🔔 DM notifications for this order are now **{state_text}**.",
            ephemeral=True,
        )

    @discord.ui.button(label="📢 Toggle Alerts", style=discord.ButtonStyle.secondary, row=1)
    async def toggle_alerts_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_owner(interaction):
            return

        pkg = self._find_package()
        if not pkg:
            await interaction.response.send_message(
                "❌ Order not found.",
                ephemeral=True,
            )
            return

        current = pkg.get("channel_notifications", True)
        new_state = not current
        pkg["channel_notifications"] = new_state
        save_tracking_data()

        embed = TrackingEmbed.from_package(pkg, compact=self.compact)
        await interaction.response.edit_message(embed=embed, view=self)

        state_text = "enabled ✅ (channel alerts on)" if new_state else "disabled 🔕 (no channel alerts)"
        await interaction.followup.send(
            f"📢 Channel alerts for this order are now **{state_text}**.",
            ephemeral=True,
        )

    @discord.ui.button(label="⏸ Toggle tracking", style=discord.ButtonStyle.danger, row=1)
    async def toggle_tracking_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_owner(interaction):
            return

        pkg = self._find_package()
        if not pkg:
            await interaction.response.send_message(
                "❌ Order not found.",
                ephemeral=True,
            )
            return

        current = pkg.get("active", True)
        if pkg.get("stage") == "delivered":
            pkg["active"] = False
            save_tracking_data()
            embed = TrackingEmbed.from_package(pkg, compact=self.compact)
            await interaction.response.edit_message(embed=embed, view=self)
            await interaction.followup.send(
                "✅ This order is delivered and tracking is locked as completed.",
                ephemeral=True,
            )
            return

        new_state = not current
        pkg["active"] = new_state
        save_tracking_data()

        embed = TrackingEmbed.from_package(pkg, compact=self.compact)
        await interaction.response.edit_message(embed=embed, view=self)

        state_text = "enabled ✅ (auto updates on)" if new_state else "disabled ⏸ (no auto updates)"
        await interaction.followup.send(
            f"⚙ Tracking for this order is now **{state_text}**.",
            ephemeral=True,
        )

    @discord.ui.button(label="⏱ Set Interval", style=discord.ButtonStyle.secondary, row=1)
    async def set_interval_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_owner(interaction):
            return

        pkg = self._find_package()
        if not pkg:
            await interaction.response.send_message(
                "❌ Order not found.",
                ephemeral=True,
            )
            return

        intervals = [5, 10, 15, 30, 60, 120]
        options = [
            discord.SelectOption(
                label=f"{m} minutes",
                value=str(m),
                description=f"Check every {m} minutes" if m > 1 else f"Check every minute",
                default=(pkg.get("refresh_interval_minutes", DEFAULT_REFRESH_INTERVAL_MINUTES) == m),
            )
            for m in intervals
        ]

        view = SetIntervalQuickView(self.owner_id, self.tracking_number, options)
        await interaction.response.send_message(
            f"Select refresh interval for `{self.tracking_number}`:",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="✏️ Edit Name", style=discord.ButtonStyle.secondary, row=1)
    async def edit_name_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_owner(interaction):
            return

        pkg = self._find_package()
        if not pkg:
            await interaction.response.send_message(
                "❌ Order not found.",
                ephemeral=True,
            )
            return

        modal = EditNicknameModal(self.owner_id, self.tracking_number, pkg.get("nickname", ""))
        await interaction.response.send_modal(modal)

    # ─────────────────────────────
    # Row 3: Share & Delete
    # ─────────────────────────────
    @discord.ui.button(label="👥 Share", style=discord.ButtonStyle.secondary, row=2)
    async def share_order_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_owner(interaction):
            return

        pkg = self._find_package()
        if not pkg:
            await interaction.response.send_message(
                "❌ Order not found.",
                ephemeral=True,
            )
            return

        shared_users = pkg.get("shared_users") or []
        shared_text = ""
        if shared_users:
            if not interaction.guild:
                shared_text = f"\n\n👥 **Currently shared with:** {len(shared_users)} user(s)"
            else:
                shared_names = []
                for uid in shared_users[:5]:
                    member = interaction.guild.get_member(uid)
                    if member:
                        shared_names.append(member.mention)
                    else:
                        shared_names.append(f"<@{uid}>")
                if shared_names:
                    shared_text = f"\n\n👥 **Currently shared with:** {', '.join(shared_names)}"
                if len(shared_users) > 5:
                    shared_text += f" and {len(shared_users) - 5} more"

        await interaction.response.send_message(
            f"👥 **Share Order**\n\n"
            f"To share order `{self.tracking_number}` with another user, use:\n"
            f"`/share_order user:@username`\n\n"
            f"Or use `/unshare_order` to remove access.{shared_text}",
            ephemeral=True,
        )

    @discord.ui.button(label="🗑️ Delete order", style=discord.ButtonStyle.danger, row=2)
    async def delete_order_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_owner(interaction):
            return

        await interaction.response.send_message(
            content=(
                f"⚠ You are about to delete order `{self.tracking_number}`.\n"
                "This will also delete its channel. Are you sure?"
            ),
            view=ConfirmDeleteView(self.owner_id, self.tracking_number),
            ephemeral=True,
        )


# ─────────────────────────────
# Edit Nickname Modal
# ─────────────────────────────
class EditNicknameModal(discord.ui.Modal, title="Edit Order Nickname"):
    nickname_input = discord.ui.TextInput(
        label="Order Nickname",
        placeholder="Enter a nickname for this order (leave empty to remove)",
        max_length=100,
        required=False,
    )

    def __init__(self, owner_id: int, tracking_number: str, current_nickname: str):
        super().__init__()
        self.owner_id = owner_id
        self.tracking_number = tracking_number
        if current_nickname:
            self.nickname_input.default = current_nickname

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "❌ This form belongs to another user.",
                ephemeral=True,
            )
            return

        new_nickname = self.nickname_input.value.strip() if self.nickname_input.value else ""
        
        # البحث عن الطلب
        found = False
        for user_id_str, packages in tracking_data.items():
            for pkg in packages:
                if pkg.get("tracking_number") == self.tracking_number:
                    if new_nickname:
                        pkg["nickname"] = new_nickname
                        result_text = f"✅ Nickname updated to: **{new_nickname}**"
                    else:
                        pkg.pop("nickname", None)
                        result_text = "✅ Nickname removed"
                    found = True
                    save_tracking_data()
                    break
            if found:
                break

        if found:
            await interaction.response.send_message(
                result_text,
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"❌ Order `{self.tracking_number}` not found.",
                ephemeral=True,
            )


# ─────────────────────────────
# Quick interval selector for OrderView
# ─────────────────────────────
class SetIntervalQuickView(discord.ui.View):
    def __init__(self, owner_id: int, tracking_number: str, options: List[discord.SelectOption]):
        super().__init__(timeout=60)
        self.owner_id = owner_id
        self.tracking_number = tracking_number
        self.select = discord.ui.Select(
            placeholder="Select refresh interval…",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.select.callback = self._on_select  # type: ignore
        self.add_item(self.select)

    async def _on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "❌ This belongs to another user.",
                ephemeral=True,
            )
            return

        interval = int(self.select.values[0])
        
        # البحث عن الطلب في جميع المستخدمين
        found = False
        for user_id_str, packages in tracking_data.items():
            for pkg in packages:
                if pkg.get("tracking_number") == self.tracking_number:
                    pkg["refresh_interval_minutes"] = interval
                    found = True
                    break
            if found:
                break

        if found:
            save_tracking_data()
            await interaction.response.edit_message(
                content=f"✅ Refresh interval set to **{interval} minutes** for `{self.tracking_number}`.",
                view=None,
            )
        else:
            await interaction.response.edit_message(
                content=f"❌ Order `{self.tracking_number}` not found.",
                view=None,
            )


# ─────────────────────────────
# Delete views
# ─────────────────────────────
class ConfirmDeleteView(discord.ui.View):
    def __init__(self, owner_id: int, tracking_number: str):
        super().__init__(timeout=60)
        self.owner_id = owner_id
        self.tracking_number = tracking_number

    async def _ensure_owner(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "❌ This confirmation belongs to another user.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.danger)
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_owner(interaction):
            return

        user_id_str = str(self.owner_id)
        user_packages = tracking_data.get(user_id_str, [])

        removed_pkg: Optional[Dict[str, Any]] = None
        remaining: List[Dict[str, Any]] = []
        for p in user_packages:
            if p["tracking_number"] == self.tracking_number and removed_pkg is None:
                removed_pkg = p
                continue
            remaining.append(p)

        tracking_data[user_id_str] = remaining
        save_tracking_data()

        if removed_pkg:
            channel_id = removed_pkg.get("channel_id")
            guild_id = removed_pkg.get("guild_id")
            if guild_id and channel_id:
                guild = bot.get_guild(int(guild_id))
                if guild:
                    channel = guild.get_channel(int(channel_id))
                    if channel:
                        try:
                            await channel.delete(
                                reason=f"Order {self.tracking_number} removed by {interaction.user}"
                            )
                        except Exception as e:
                            print(f"⚠️ Failed to delete channel {channel_id}: {e}")

            msg_text = f"✅ Order `{self.tracking_number}` removed and its channel deleted."
            try:
                await interaction.response.edit_message(
                    content=msg_text,
                    view=None,
                )
            except discord.NotFound:
                if not interaction.response.is_done():
                    try:
                        await interaction.response.send_message(msg_text, ephemeral=True)
                    except Exception:
                        pass
                else:
                    try:
                        await interaction.followup.send(msg_text, ephemeral=True)
                    except Exception:
                        try:
                            if interaction.channel:
                                await interaction.channel.send(msg_text)
                        except Exception:
                            pass
        else:
            try:
                await interaction.response.edit_message(
                    content=f"❌ No order found with tracking number `{self.tracking_number}`.",
                    view=None,
                )
            except discord.NotFound:
                await interaction.followup.send(
                    f"❌ No order found with tracking number `{self.tracking_number}`.",
                    ephemeral=True,
                )

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_owner(interaction):
            return

        try:
            await interaction.response.edit_message(
                content="❌ Deletion cancelled.",
                view=None,
            )
        except discord.NotFound:
            await interaction.followup.send(
                "❌ Deletion cancelled.",
                ephemeral=True,
            )


class DeleteOrderView(discord.ui.View):
    def __init__(self, owner_id: int, options: List[discord.SelectOption]):
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.select = discord.ui.Select(
            placeholder="Select an order to delete…",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.select.callback = self._on_select  # type: ignore
        self.add_item(self.select)

    async def _on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "❌ This list belongs to another user.",
                ephemeral=True,
            )
            return

        tracking_number = self.select.values[0]
        await interaction.response.edit_message(
            content=(
                f"⚠ You selected order `{tracking_number}`.\n"
                "Are you sure you want to delete it and its channel?"
            ),
            view=ConfirmDeleteView(self.owner_id, tracking_number),
        )


# ─────────────────────────────
# Select views for other commands
# ─────────────────────────────
class OrderDetailsSelectView(discord.ui.View):
    def __init__(self, user_id: int, options: List[discord.SelectOption]):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.select = discord.ui.Select(
            placeholder="Select an order to view details…",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.select.callback = self._on_select  # type: ignore
        self.add_item(self.select)

    async def _on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "❌ This list belongs to another user.",
                ephemeral=True,
            )
            return

        tn = self.select.values[0]

        target_pkg = None
        for owner_uid, packages in tracking_data.items():
            for pkg in packages:
                owner_id = pkg.get("owner_id", int(owner_uid))
                shared_users = pkg.get("shared_users") or []
                if pkg.get("tracking_number") == tn and (
                    interaction.user.id == owner_id or interaction.user.id in shared_users
                ):
                    target_pkg = pkg
                    break
            if target_pkg:
                break

        if not target_pkg:
            await interaction.response.edit_message(
                content=f"❌ No order found with tracking number `{tn}` that you can access.",
                view=None,
            )
            return

        if target_pkg.get("active", True):
            await update_package_status(target_pkg)
            target_pkg["last_checked_at"] = utcnow().isoformat()
            target_pkg.pop("_stage_changed", None)
            target_pkg.pop("_last_update_changed", None)
            save_tracking_data()
            await move_channel_if_delivered(target_pkg)
            await rename_channel_for_stage(target_pkg)

        embed = TrackingEmbed.from_package(target_pkg, compact=False)
        await interaction.response.edit_message(
            content=f"Details for `{tn}`:",
            embed=embed,
            view=None,
        )


class SetIntervalSelectView(discord.ui.View):
    def __init__(self, owner_id: int, interval_minutes: int, options: List[discord.SelectOption]):
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.interval_minutes = interval_minutes
        self.select = discord.ui.Select(
            placeholder="Select an order to change its refresh interval…",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.select.callback = self._on_select  # type: ignore
        self.add_item(self.select)

    async def _on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "❌ This list belongs to another user.",
                ephemeral=True,
            )
            return

        tn = self.select.values[0]
        user_id_str = str(self.owner_id)
        user_packages = tracking_data.get(user_id_str, [])

        for pkg in user_packages:
            if pkg["tracking_number"] == tn:
                pkg["refresh_interval_minutes"] = int(self.interval_minutes)
                save_tracking_data()
                await interaction.response.edit_message(
                    content=(
                        f"✅ Auto refresh interval for `{tn}` set to "
                        f"**{self.interval_minutes} minutes**."
                    ),
                    view=None,
                )
                return

        await interaction.response.edit_message(
            content=f"❌ No order found with tracking number `{tn}`.",
            view=None,
        )


class ShareOrderSelectView(discord.ui.View):
    def __init__(self, owner_id: int, target_user: discord.Member, options: List[discord.SelectOption]):
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.target_user = target_user
        self.select = discord.ui.Select(
            placeholder="Select an order to share…",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.select.callback = self._on_select  # type: ignore
        self.add_item(self.select)

    async def _on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "❌ This list belongs to another user.",
                ephemeral=True,
            )
            return

        tn = self.select.values[0]
        owner_id_str = str(self.owner_id)
        user_packages = tracking_data.get(owner_id_str, [])

        target_pkg = None
        for pkg in user_packages:
            if pkg.get("tracking_number") == tn:
                target_pkg = pkg
                break

        if not target_pkg:
            await interaction.response.edit_message(
                content=f"❌ No order found with tracking number `{tn}` under your account.",
                view=None,
            )
            return

        shared = target_pkg.get("shared_users")
        if shared is None:
            shared = []
            target_pkg["shared_users"] = shared

        if self.target_user.id == self.owner_id:
            await interaction.response.edit_message(
                content="⚠ You are already the owner of this order.",
                view=None,
            )
            return

        if self.target_user.id in shared:
            await interaction.response.edit_message(
                content=f"ℹ {self.target_user.mention} is already allowed to control this order.",
                view=None,
            )
            return

        shared.append(self.target_user.id)
        target_pkg["shared_users"] = shared
        save_tracking_data()

        await interaction.response.edit_message(
            content=f"✅ {self.target_user.mention} can now control order `{tn}`.",
            view=None,
        )

        try:
            await self.target_user.send(
                f"📦 You have been granted control over order `{tn}` "
                f"by {interaction.user.mention} in server **{interaction.guild.name}**."
            )
        except Exception:
            pass


class UnshareOrderSelectView(discord.ui.View):
    def __init__(self, owner_id: int, target_user: discord.Member, options: List[discord.SelectOption]):
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.target_user = target_user
        self.select = discord.ui.Select(
            placeholder="Select an order to unshare…",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.select.callback = self._on_select  # type: ignore
        self.add_item(self.select)

    async def _on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "❌ This list belongs to another user.",
                ephemeral=True,
            )
            return

        tn = self.select.values[0]
        owner_id_str = str(self.owner_id)
        user_packages = tracking_data.get(owner_id_str, [])

        target_pkg = None
        for pkg in user_packages:
            if pkg.get("tracking_number") == tn:
                target_pkg = pkg
                break

        if not target_pkg:
            await interaction.response.edit_message(
                content=f"❌ No order found with tracking number `{tn}` under your account.",
                view=None,
            )
            return

        shared = target_pkg.get("shared_users") or []

        if self.target_user.id not in shared:
            await interaction.response.edit_message(
                content=f"ℹ {self.target_user.mention} does not currently have access to this order.",
                view=None,
            )
            return

        shared.remove(self.target_user.id)
        target_pkg["shared_users"] = shared
        save_tracking_data()

        await interaction.response.edit_message(
            content=f"✅ {self.target_user.mention} no longer has access to order `{tn}`.",
            view=None,
        )

        try:
            await self.target_user.send(
                f"📦 Your access to order `{tn}` in server **{interaction.guild.name}** "
                f"has been revoked by {interaction.user.mention}."
            )
        except Exception:
            pass


class ScreenshotSelectView(discord.ui.View):
    def __init__(self, user_id: int, options: List[discord.SelectOption]):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.select = discord.ui.Select(
            placeholder="Select an order to capture screenshot…",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.select.callback = self._on_select  # type: ignore
        self.add_item(self.select)

    async def _on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "❌ This list belongs to another user.",
                ephemeral=True,
            )
            return

        tn = self.select.values[0]

        target_pkg = None
        for owner_uid, packages in tracking_data.items():
            for pkg in packages:
                owner_id = pkg.get("owner_id", int(owner_uid))
                shared_users = pkg.get("shared_users") or []
                if pkg.get("tracking_number") == tn and (
                    interaction.user.id == owner_id or interaction.user.id in shared_users
                ):
                    target_pkg = pkg
                    break
            if target_pkg:
                break

        if not target_pkg:
            await interaction.response.edit_message(
                content=f"❌ No order found with tracking number `{tn}` that you can access.",
                view=None,
            )
            return

        url = make_tracking_link(target_pkg.get("company", ""), target_pkg["tracking_number"])
        if not url:
            await interaction.response.edit_message(
                content="❌ Could not build tracking URL for this company.",
                view=None,
            )
            return

        await interaction.response.edit_message(
            content=f"⏳ Capturing screenshot for `{tn}`...",
            view=None,
        )

        filename = f"{target_pkg['tracking_number']}.png"
        path = await capture_tracking_screenshot(url, filename)
        if not path:
            await interaction.followup.send(
                "⚠ Failed to capture screenshot (Playwright not installed or error occurred).",
                ephemeral=True,
            )
            return

        file = discord.File(path, filename=filename)
        await interaction.followup.send(
            content=f"📸 Screenshot for `{tn}`:",
            file=file,
            ephemeral=True,
        )


# ─────────────────────────────
# Slash command choices
# ─────────────────────────────
COMPANY_CHOICES = [
    app_commands.Choice(name="AliExpress (Cainiao)", value="aliexpress"),
    app_commands.Choice(name="Cainiao Global", value="cainiao"),
    app_commands.Choice(name="Temu (Cainiao)", value="temu"),
    app_commands.Choice(name="SHEIN (Cainiao)", value="shein"),
    app_commands.Choice(name="DHL", value="dhl"),
    app_commands.Choice(name="Aramex", value="aramex"),
]


# ─────────────────────────────
# Bot events
# ─────────────────────────────
@bot.event
async def on_ready():
    global playwright_browser

    if playwright_browser is None:
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        playwright_browser = await pw.chromium.launch(headless=True)

    load_tracking_data()
    load_guild_config()
    logger.info(f"Bot ready: {bot.user} ({bot.user.id})")

    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"❌ Slash sync error: {e}")

    if ENABLE_AUTO_UPDATES:
        if not check_tracking_updates.is_running():
            check_tracking_updates.start()
    else:
        logger.info("⏹ Auto updates disabled via ENABLE_AUTO_UPDATES=false")


# ─────────────────────────────
# Slash commands
# ─────────────────────────────
if ENABLE_TRACK_ORDER:
    @bot.tree.command(
        name="track_order",
        description="Add a new tracked order (AliExpress/Cainiao/Temu/etc.)",
    )
    @app_commands.describe(
        tracking_number="Shipment tracking number",
        company="Platform/carrier",
        nickname="Optional label for this order (used as channel name)",
        refresh_interval_minutes="Auto-refresh interval in minutes (default 15)",
    )
    @app_commands.choices(company=COMPANY_CHOICES)
    async def track_order(
        interaction: discord.Interaction,
        tracking_number: str,
        company: app_commands.Choice[str],
        nickname: Optional[str] = None,
        refresh_interval_minutes: Optional[int] = None,
    ):
        await interaction.response.defer(thinking=True, ephemeral=True)

        if not interaction.guild:
            await interaction.followup.send(
                "❌ This command only works inside a server.",
                ephemeral=True,
            )
            return

        user_id = str(interaction.user.id)
        tracking_data.setdefault(user_id, [])

        for pkg in tracking_data[user_id]:
            if pkg["tracking_number"] == tracking_number:
                await interaction.followup.send(
                    "⚠ This tracking number is already being tracked under your account.",
                    ephemeral=True,
                )
                return

        guild_conf = await ensure_order_categories(interaction.guild)
        in_cat = interaction.guild.get_channel(guild_conf["in_progress_category_id"])

        if not isinstance(in_cat, discord.CategoryChannel):
            await interaction.followup.send(
                "❌ Could not access 'Orders - In Progress' category.",
                ephemeral=True,
            )
            return

        now = utcnow()
        comp_lower = company.value.lower()
        interval = refresh_interval_minutes if refresh_interval_minutes and refresh_interval_minutes > 0 else DEFAULT_REFRESH_INTERVAL_MINUTES

        pkg: Dict[str, Any] = {
            "tracking_number": tracking_number,
            "company": comp_lower,
            "nickname": nickname or "",
            "channel_id": None,
            "guild_id": interaction.guild.id,
            "added_at": now.isoformat(),
            "order_date": None,
            "stage": "processing",
            "status_index": get_stage_index("processing"),
            "estimated_delivery": None,
            "timeline": [
                f"{datetime.now(BAHRAIN_TZ).strftime('%d-%m-%Y %I:%M %p')} - Order registered in tracking bot.",
            ],
            "active": True,
            "refresh_interval_minutes": int(interval),
            "last_checked_at": None,
            "message_id": None,
            "delivered_date": None,
            "dm_enabled": True,
            "latest_tracking_number": None,
            "hop_countries": [],
            "origin_country_name": None,
            "destination_country_name": None,
            "owner_id": interaction.user.id,
            "shared_users": [],
            "channel_notifications": True,
        }

        channel_name = make_channel_display_name(pkg)

        topic_parts = [f"Tracking {tracking_number} ({company.value}) for {interaction.user}"]
        if nickname:
            topic_parts.append(f"Nickname: {nickname}")
        topic_parts.append("Order Date: pending from carrier")
        topic = " | ".join(topic_parts)

        order_channel = await interaction.guild.create_text_channel(
            name=channel_name,
            category=in_cat,
            topic=topic,
        )
        pkg["channel_id"] = order_channel.id

        tracking_data[user_id].append(pkg)
        save_tracking_data()

        await update_package_status(pkg)
        pkg["last_checked_at"] = utcnow().isoformat()
        pkg.pop("_stage_changed", None)
        pkg.pop("_last_update_changed", None)
        save_tracking_data()
        await move_channel_if_delivered(pkg)
        await rename_channel_for_stage(pkg)

        embed = TrackingEmbed.from_package(pkg, compact=False)
        view = OrderView(interaction.user.id, tracking_number, comp_lower, interaction.guild.id, compact=False)

        # محاولة توليد QR للرابط الرسمي للتتبع (اختياري)
        qr_file = None
        qr_path = generate_tracking_qr(comp_lower, tracking_number)
        if qr_path:
            try:
                embed.set_image(url=f"attachment://{tracking_number}.png")
                qr_file = discord.File(qr_path, filename=f"{tracking_number}.png")
            except Exception:
                qr_file = None

        if qr_file:
            first_msg = await order_channel.send(
                content=(
                    f"{interaction.user.mention} A dedicated channel has been created for this order.\n"
                    "Use the buttons below or slash commands to manage this tracking."
                ),
                embed=embed,
                view=view,
                file=qr_file,
            )
        else:
            first_msg = await order_channel.send(
                content=(
                    f"{interaction.user.mention} A dedicated channel has been created for this order.\n"
                    "Use the buttons below or slash commands to manage this tracking."
                ),
                embed=embed,
                view=view,
            )

        pkg["message_id"] = first_msg.id
        save_tracking_data()

        order_date_str = pkg.get("order_date") or "Not set"
        eta_str = pkg.get("estimated_delivery") or "Not set"

        await interaction.followup.send(
            f"✅ Order added and channel created: {order_channel.mention}\n"
            f"🗓 Order Date: `{order_date_str}`\n"
            f"📅 Estimated delivery (Order Date + {ORDER_DELIVERY_DAYS} days): `{eta_str}`\n"
            f"⏱ Auto refresh interval: **{interval} minutes**.\n"
            f"🔔 You can toggle DM notifications from the **Toggle DM** button.\n"
            f"📢 You can toggle channel alerts from the **Toggle Alerts** button.\n"
            f"⏸ You can pause/resume auto tracking using **Toggle tracking** button.",
            ephemeral=True,
        )
else:
    print("Slash command /track_order is disabled via ENABLE_TRACK_ORDER=false")


if ENABLE_MY_ORDERS:
    @bot.tree.command(name="my_orders", description="Show all your tracked orders")
    async def my_orders(interaction: discord.Interaction):
        user_id = str(interaction.user.id)

        # ✅ نعلن للديسكورد إننا نحتاج وقت (إيفيمرال + thinking)
        await interaction.response.defer(ephemeral=True, thinking=True)

        # نحدّث شحنات المالك نفسه
        await refresh_user_packages(user_id)

        # (pkg, owner_id)
        packages_for_user = []

        for owner_uid, packages in tracking_data.items():
            owner_uid_int = int(owner_uid)
            for pkg in packages:
                owner_id = pkg.get("owner_id", owner_uid_int)
                shared_users = pkg.get("shared_users") or []
                if interaction.user.id == owner_id or interaction.user.id in shared_users:
                    packages_for_user.append((pkg, owner_id))

        if not packages_for_user:
            await interaction.followup.send(
                "You don't have any tracked orders yet.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="📦 My orders",
            description=f"You have **{len(packages_for_user)}** tracked/shared orders.",
            color=discord.Color.blurple(),
        )

        for idx, (pkg, owner_id) in enumerate(packages_for_user, start=1):
            stage = pkg.get("stage", "processing")
            status_text = STATUS_TEXT.get(stage, stage)
            active = "Active" if pkg.get("active", True) else "Paused"
            nickname = f" – {pkg['nickname']}" if pkg.get("nickname") else ""
            channel_id = pkg.get("channel_id")
            chan_mention = f"<#{channel_id}>" if channel_id else "—"
            interval = pkg.get("refresh_interval_minutes", DEFAULT_REFRESH_INTERVAL_MINUTES)
            order_date_str = pkg.get("order_date", "Not set")
            dm_enabled = pkg.get("dm_enabled", True)
            dm_text = "On 🔔" if dm_enabled else "Off 🔕"
            channel_notifications = pkg.get("channel_notifications", True)
            channel_text = "On 📢" if channel_notifications else "Off 🔕"
            role = "Owner" if interaction.user.id == owner_id else "Shared"

            embed.add_field(
                name=f"{idx}. {pkg['tracking_number']}{nickname}",
                value=(
                    f"• Company: `{pkg['company'].upper()}`\n"
                    f"• Status: `{status_text}`\n"
                    f"• Role: `{role}`\n"
                    f"• Order Date: `{order_date_str}`\n"
                    f"• ETA: `{pkg.get('estimated_delivery', 'Not set')}`\n"
                    f"• Tracking: `{active}` / Interval: `{interval} min`\n"
                    f"• DM: `{dm_text}` | Channel alerts: `{channel_text}`\n"
                    f"• Channel: {chan_mention}"
                ),
                inline=False,
            )

        # ✅ نرد عن طريق followup بعد الـ defer
        await interaction.followup.send(embed=embed, ephemeral=True)
else:
    print("Slash command /my_orders is disabled via ENABLE_MY_ORDERS=false")


if ENABLE_ORDER_DETAILS:
    @bot.tree.command(name="order_details", description="Show details for one order")
    async def order_details(interaction: discord.Interaction):
        # جمع كل الطلبات اللي يقدر يشوفها (مالك أو شريك)
        options: List[discord.SelectOption] = []
        for owner_uid, packages in tracking_data.items():
            owner_uid_int = int(owner_uid)
            for pkg in packages:
                owner_id = pkg.get("owner_id", owner_uid_int)
                shared_users = pkg.get("shared_users") or []
                if interaction.user.id == owner_id or interaction.user.id in shared_users:
                    tn = pkg["tracking_number"]
                    nick = pkg.get("nickname") or ""
                    label = tn if not nick else f"{tn} – {nick}"
                    label = label[:100]
                    stage = STATUS_TEXT.get(pkg.get("stage", ""), pkg.get("stage", ""))
                    desc = f"{pkg.get('company','').upper()} | {stage}"
                    options.append(discord.SelectOption(label=label, value=tn, description=desc[:100]))

        if not options:
            await interaction.response.send_message(
                "You don't have any tracked orders.",
                ephemeral=True,
            )
            return

        view = OrderDetailsSelectView(interaction.user.id, options[:25])
        await interaction.response.send_message(
            "Select an order to view its details:",
            view=view,
            ephemeral=True,
        )
else:
    logger.info("Slash command /order_details is disabled via ENABLE_ORDER_DETAILS=false")


if ENABLE_DELETE_ORDER:
    @bot.tree.command(name="delete_order", description="Delete an order from tracking")
    async def delete_order(interaction: discord.Interaction):
        user_id = str(interaction.user.id)
        user_packages = tracking_data.get(user_id, [])

        if not user_packages:
            await interaction.response.send_message(
                "You don't have any tracked orders.",
                ephemeral=True,
            )
            return

        options: List[discord.SelectOption] = []
        for pkg in user_packages[:25]:
            tn = pkg["tracking_number"]
            nick = pkg.get("nickname") or ""
            label = tn if not nick else f"{tn} – {nick}"
            label = label[:100]
            stage = STATUS_TEXT.get(pkg.get("stage", ""), pkg.get("stage", ""))
            desc = f"{pkg.get('company','').upper()} | {stage}"
            options.append(discord.SelectOption(label=label, value=tn, description=desc[:100]))

        view = DeleteOrderView(interaction.user.id, options)
        await interaction.response.send_message(
            "Select the order you want to delete:",
            view=view,
            ephemeral=True,
        )
else:
    print("Slash command /delete_order is disabled via ENABLE_DELETE_ORDER=false")


if ENABLE_SET_REFRESH_INTERVAL:
    @bot.tree.command(
        name="set_refresh_interval",
        description="Set auto refresh interval (minutes) for one order",
    )
    @app_commands.describe(
        interval_minutes="Interval in minutes (e.g. 5, 10, 30)",
    )
    async def set_refresh_interval(
        interaction: discord.Interaction,
        interval_minutes: int,
    ):
        if interval_minutes <= 0:
            await interaction.response.send_message(
                "Interval must be a positive number of minutes.",
                ephemeral=True,
            )
            return

        user_id = str(interaction.user.id)
        user_packages = tracking_data.get(user_id, [])

        if not user_packages:
            await interaction.response.send_message(
                "You don't have any tracked orders.",
                ephemeral=True,
            )
            return

        options: List[discord.SelectOption] = []
        for pkg in user_packages[:25]:
            tn = pkg["tracking_number"]
            nick = pkg.get("nickname") or ""
            label = tn if not nick else f"{tn} – {nick}"
            label = label[:100]
            stage = STATUS_TEXT.get(pkg.get("stage", ""), pkg.get("stage", ""))
            desc = f"{pkg.get('company','').upper()} | {stage}"
            options.append(discord.SelectOption(label=label, value=tn, description=desc[:100]))

        view = SetIntervalSelectView(interaction.user.id, interval_minutes, options)
        await interaction.response.send_message(
            f"Select the order you want to set interval to **{interval_minutes} minutes**:",
            view=view,
            ephemeral=True,
        )
else:
    logger.info("Slash command /set_refresh_interval is disabled via ENABLE_SET_REFRESH_INTERVAL=false")


# ─────────────────────────────
# /share_order & /unshare_order
# ─────────────────────────────
if ENABLE_SHARE_ORDER:
    @bot.tree.command(
        name="share_order",
        description="Allow another member to control one of your orders.",
    )
    @app_commands.describe(
        user="Member you want to share this order with",
    )
    async def share_order(
        interaction: discord.Interaction,
        user: discord.Member,
    ):
        owner_id_str = str(interaction.user.id)
        user_packages = tracking_data.get(owner_id_str, [])

        if not user_packages:
            await interaction.response.send_message(
                "❌ You don't have any tracked orders to share.",
                ephemeral=True,
            )
            return

        options: List[discord.SelectOption] = []
        for pkg in user_packages[:25]:
            tn = pkg["tracking_number"]
            nick = pkg.get("nickname") or ""
            label = tn if not nick else f"{tn} – {nick}"
            label = label[:100]
            stage = STATUS_TEXT.get(pkg.get("stage", ""), pkg.get("stage", ""))
            desc = f"{pkg.get('company','').upper()} | {stage}"
            options.append(discord.SelectOption(label=label, value=tn, description=desc[:100]))

        view = ShareOrderSelectView(interaction.user.id, user, options)
        await interaction.response.send_message(
            f"Select an order to share with {user.mention}:",
            view=view,
            ephemeral=True,
        )
else:
    print("Slash command /share_order is disabled via ENABLE_SHARE_ORDER=false")


if ENABLE_UNSHARE_ORDER:
    @bot.tree.command(
        name="unshare_order",
        description="Remove a member's access to one of your shared orders.",
    )
    @app_commands.describe(
        user="Member you want to remove from this order",
    )
    async def unshare_order(
        interaction: discord.Interaction,
        user: discord.Member,
    ):
        owner_id_str = str(interaction.user.id)
        user_packages = tracking_data.get(owner_id_str, [])

        if not user_packages:
            await interaction.response.send_message(
                "❌ You don't have any tracked orders.",
                ephemeral=True,
            )
            return

        options: List[discord.SelectOption] = []
        for pkg in user_packages[:25]:
            shared = pkg.get("shared_users") or []
            if user.id in shared:
                tn = pkg["tracking_number"]
                nick = pkg.get("nickname") or ""
                label = tn if not nick else f"{tn} – {nick}"
                label = label[:100]
                stage = STATUS_TEXT.get(pkg.get("stage", ""), pkg.get("stage", ""))
                desc = f"{pkg.get('company','').upper()} | {stage}"
                options.append(discord.SelectOption(label=label, value=tn, description=desc[:100]))

        if not options:
            await interaction.response.send_message(
                f"ℹ {user.mention} does not currently have access to any of your orders.",
                ephemeral=True,
            )
            return

        view = UnshareOrderSelectView(interaction.user.id, user, options)
        await interaction.response.send_message(
            f"Select an order to remove {user.mention} from:",
            view=view,
            ephemeral=True,
        )
else:
    print("Slash command /unshare_order is disabled via ENABLE_UNSHARE_ORDER=false")


# ─────────────────────────────
# /share_all_orders & /unshare_all_orders
# ─────────────────────────────
if ENABLE_SHARE_ALL_ORDERS:
    @bot.tree.command(
        name="share_all_orders",
        description="Share ALL your orders with another member.",
    )
    @app_commands.describe(
        user="Member you want to share all your orders with",
    )
    async def share_all_orders(
        interaction: discord.Interaction,
        user: discord.Member,
    ):
        owner_id_str = str(interaction.user.id)
        user_packages = tracking_data.get(owner_id_str, [])

        if not user_packages:
            await interaction.response.send_message(
                "❌ You don't have any tracked orders to share.",
                ephemeral=True,
            )
            return

        if user.id == interaction.user.id:
            await interaction.response.send_message(
                "⚠ You are already the owner of these orders.",
                ephemeral=True,
            )
            return

        changed_count = 0
        for pkg in user_packages:
            shared = pkg.get("shared_users") or []
            if user.id not in shared:
                shared.append(user.id)
                pkg["shared_users"] = shared
                changed_count += 1

        if changed_count == 0:
            await interaction.response.send_message(
                f"ℹ {user.mention} already has access to all your orders.",
                ephemeral=True,
            )
            return

        save_tracking_data()

        await interaction.response.send_message(
            f"✅ {user.mention} can now control **{changed_count}** of your orders (all your tracked orders).",
            ephemeral=True,
        )

        try:
            await user.send(
                f"📦 You have been granted access to **{changed_count}** orders "
                f"by {interaction.user.mention} in server **{interaction.guild.name}**."
            )
        except Exception:
            pass
else:
    print("Slash command /share_all_orders is disabled via ENABLE_SHARE_ALL_ORDERS=false")


if ENABLE_UNSHARE_ALL_ORDERS:
    @bot.tree.command(
        name="unshare_all_orders",
        description="Remove a member's access from ALL your shared orders.",
    )
    @app_commands.describe(
        user="Member you want to remove from all your orders",
    )
    async def unshare_all_orders(
        interaction: discord.Interaction,
        user: discord.Member,
    ):
        owner_id_str = str(interaction.user.id)
        user_packages = tracking_data.get(owner_id_str, [])

        if not user_packages:
            await interaction.response.send_message(
                "❌ You don't have any tracked orders.",
                ephemeral=True,
            )
            return

        if user.id == interaction.user.id:
            await interaction.response.send_message(
                "⚠ You are the owner of these orders, you can't remove yourself as shared.",
                ephemeral=True,
            )
            return

        changed_count = 0
        for pkg in user_packages:
            shared = pkg.get("shared_users") or []
            if user.id in shared:
                shared.remove(user.id)
                pkg["shared_users"] = shared
                changed_count += 1

        if changed_count == 0:
            await interaction.response.send_message(
                f"ℹ {user.mention} does not have access to any of your orders.",
                ephemeral=True,
            )
            return

        save_tracking_data()

        await interaction.response.send_message(
            f"✅ {user.mention} no longer has access to **{changed_count}** of your orders.",
            ephemeral=True,
        )

        try:
            await user.send(
                f"📦 Your access to **{changed_count}** orders in server **{interaction.guild.name}** "
                f"has been revoked by {interaction.user.mention}."
            )
        except Exception:
            pass
else:
    print("Slash command /unshare_all_orders is disabled via ENABLE_UNSHARE_ALL_ORDERS=false")


# ─────────────────────────────
# /tracking_screenshot (select)
# ─────────────────────────────
if ENABLE_TRACKING_SCREENSHOT_CMD:
    @bot.tree.command(
        name="tracking_screenshot",
        description="Capture a screenshot of the official tracking page for one of your orders.",
    )
    async def tracking_screenshot(interaction: discord.Interaction):
        options: List[discord.SelectOption] = []
        for owner_uid, packages in tracking_data.items():
            owner_uid_int = int(owner_uid)
            for pkg in packages:
                owner_id = pkg.get("owner_id", owner_uid_int)
                shared_users = pkg.get("shared_users") or []
                if interaction.user.id == owner_id or interaction.user.id in shared_users:
                    tn = pkg["tracking_number"]
                    nick = pkg.get("nickname") or ""
                    label = tn if not nick else f"{tn} – {nick}"
                    label = label[:100]
                    stage = STATUS_TEXT.get(pkg.get("stage", ""), pkg.get("stage", ""))
                    desc = f"{pkg.get('company','').upper()} | {stage}"
                    options.append(discord.SelectOption(label=label, value=tn, description=desc[:100]))

        if not options:
            await interaction.response.send_message(
                "You don't have any tracked orders.",
                ephemeral=True,
            )
            return

        view = ScreenshotSelectView(interaction.user.id, options[:25])
        await interaction.response.send_message(
            "Select an order to capture its tracking screenshot:",
            view=view,
            ephemeral=True,
        )
else:
    print("Slash command /tracking_screenshot is disabled via ENABLE_TRACKING_SCREENSHOT_CMD=false")


# ─────────────────────────────
# /delete_dms - Delete bot's DM messages
# ─────────────────────────────
if ENABLE_DELETE_DMS:
    @bot.tree.command(
        name="delete_dms",
        description="Delete bot's messages in DMs (private messages)",
    )
    @app_commands.describe(
        user="User whose DMs to delete from (bot owner only)",
        limit="Number of messages to delete (default: all)",
    )
    async def delete_dms(
        interaction: discord.Interaction,
        user: discord.User = None,
        limit: int = None,
    ):
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Check if user is bot owner
            app_info = await bot.application_info()
            is_owner = interaction.user.id == app_info.owner.id
            
            # If user parameter is provided, check if requester is owner
            target_user = user if (user and is_owner) else interaction.user
            
            if user and not is_owner:
                await interaction.followup.send(
                    "❌ Only the bot owner can delete messages from other users' DMs.",
                    ephemeral=True,
                )
                return
            
            # Get DM channel with the target user
            dm_channel = await target_user.create_dm()
            
            deleted_count = 0
            message_limit = limit if limit and limit > 0 else None
            
            async for message in dm_channel.history(limit=message_limit):
                # Only delete messages sent by the bot
                if message.author == bot.user:
                    try:
                        await message.delete()
                        deleted_count += 1
                    except Exception:
                        pass
            
            if deleted_count > 0:
                if is_owner and user:
                    await interaction.followup.send(
                        f"✅ Deleted **{deleted_count}** message(s) from {target_user.mention}'s DMs.",
                        ephemeral=True,
                    )
                else:
                    await interaction.followup.send(
                        f"✅ Deleted **{deleted_count}** message(s) from your DMs.",
                        ephemeral=True,
                    )
            else:
                if is_owner and user:
                    await interaction.followup.send(
                        f"ℹ️ No bot messages found in {target_user.mention}'s DMs.",
                        ephemeral=True,
                    )
                else:
                    await interaction.followup.send(
                        "ℹ️ No bot messages found in your DMs.",
                        ephemeral=True,
                    )
        except Exception:
            await interaction.followup.send(
                "❌ Failed to delete messages. Please try again later.",
                ephemeral=True,
            )
else:
    logger.info("Slash command /delete_dms is disabled via ENABLE_DELETE_DMS=false")


# ─────────────────────────────
# Background auto updates
# ─────────────────────────────
@tasks.loop(seconds=1)
async def check_tracking_updates():
    if not tracking_data:
        return

    logger.debug("🔄 Checking tracking updates...")

    now = utcnow()
    users_copy = dict(tracking_data)

    for user_id, packages in users_copy.items():
        for pkg in list(packages):
            try:
                if not pkg.get("active", True):
                    continue

                interval = pkg.get("refresh_interval_minutes", DEFAULT_REFRESH_INTERVAL_MINUTES)
                last_str = pkg.get("last_checked_at")
                if last_str:
                    try:
                        last_dt = datetime.fromisoformat(last_str)
                        if last_dt.tzinfo is None:
                            last_dt = last_dt.replace(tzinfo=UTC)
                    except Exception:
                        last_dt = None
                else:
                    last_dt = None

                # فحص سريع من timeline events للكشف عن تحديثات جديدة (حتى لو لم يحن وقت التحديث من API)
                timeline = pkg.get("timeline", [])
                if timeline:
                    last_event = timeline[-1] if timeline else ""
                    last_event_lower = last_event.lower()
                    
                    # كلمات مفتاحية مهمة تتطلب تحديث فوري
                    urgent_keywords = [
                        "delivered",
                        "customs",
                        "warehouse",
                        "out for delivery",
                        "station",
                        "arrived",
                    ]
                    
                    # إذا كان هناك حدث جديد يحتوي على كلمات مفتاحية مهمة، قم بالتحديث فوراً
                    needs_urgent_check = any(kw in last_event_lower for kw in urgent_keywords)
                    
                    # إذا لم يحن وقت التحديث من API، لكن هناك حدث جديد مهم، قم بفحص timeline فقط
                    if last_dt and now - last_dt < timedelta(minutes=interval):
                        if needs_urgent_check:
                            # فحص سريع من timeline events فقط (بدون استدعاء API)
                            # هذا يساعد في اكتشاف التغييرات بسرعة أكبر
                            old_stage = pkg.get("stage", "processing")
                            all_timeline_text = " ".join(timeline[-5:]).lower()
                            
                            # كلمات مفتاحية للكشف عن الحالات (نفس المستخدمة في update_package_status)
                            delivered_keywords = [
                                "package delivered",
                                "delivered",
                                "delivery completed",
                                "successfully delivered",
                                "delivered to customer",
                                "delivered to recipient",
                                "delivery successful",
                            ]
                            customs_keywords = [
                                "import customs clearance complete",
                                "import customs clearance started",
                                "import clearance start",
                                "arrived at linehaul office",
                                "arrived at linehual office",
                                "customs clearance",
                                "customs",
                            ]
                            warehouse_dest_keywords = [
                                "arrived in transit country/region",
                                "arrive at transit country or district",
                                "received by local delivery company",
                                "bahrain station 4】arrived",
                                "bahrain station 3】arrived",
                                "bahrain station 2】arrived",
                                "bahrain station 1】arrived",
                                "station 4】arrived",
                                "station 3】arrived",
                                "station 2】arrived",
                                "station 1】arrived",
                                "arrived at destination country/region sorting center",
                            ]
                            out_for_delivery_keywords = [
                                "out for delivery",
                                "outfordelivery",
                                "on the way to recipient",
                            ]
                            
                            # فحص آخر حدث أولاً (الأهم)
                            last_event_text = timeline[-1].lower() if timeline else ""
                            
                            new_stage = old_stage
                            # 1. Delivered (أعلى أولوية) - فحص شامل
                            if any(kw in last_event_text for kw in delivered_keywords):
                                new_stage = "delivered"
                                logger.info(f"🚨 URGENT: Detected delivered from LAST event in quick check")
                            elif any(kw in all_timeline_text for kw in delivered_keywords):
                                new_stage = "delivered"
                                logger.info(f"🚨 URGENT: Detected delivered from timeline in quick check")
                            # 2. Out for delivery
                            elif any(kw in all_timeline_text for kw in out_for_delivery_keywords):
                                new_stage = "out_for_delivery"
                            # 3. Warehouse (في دولة الوجهة)
                            elif any(kw in all_timeline_text for kw in warehouse_dest_keywords):
                                new_stage = "warehouse_dest"
                            # 4. Customs
                            elif any(kw in all_timeline_text for kw in customs_keywords):
                                new_stage = "customs"
                            
                            if new_stage != old_stage:
                                pkg["stage"] = new_stage
                                pkg["status_index"] = get_stage_index(new_stage)
                                pkg["_stage_changed"] = True
                                save_tracking_data()
                                logger.info(f"🚀 Urgent update: Changed stage to {new_stage} for {pkg['tracking_number']} from timeline (without API call)")
                                
                                # تحديث القناة فوراً
                                channel_id = pkg.get("channel_id")
                                if channel_id:
                                    channel = bot.get_channel(int(channel_id))
                                    if channel:
                                        embed = TrackingEmbed.from_package(pkg, compact=False)
                                        main_msg_id = pkg.get("main_msg_id")
                                        if main_msg_id:
                                            try:
                                                main_msg = await channel.fetch_message(int(main_msg_id))
                                                await main_msg.edit(embed=embed)
                                            except Exception:
                                                pass
                                
                                await move_channel_if_delivered(pkg)
                                await rename_channel_for_stage(pkg)
                        continue

                pkg["last_checked_at"] = utcnow().isoformat()

                changed = await update_package_status(pkg)
                stage_changed = bool(pkg.get("_stage_changed", False))
                last_update_changed = bool(pkg.get("_last_update_changed", False))
                pkg.pop("_stage_changed", None)
                pkg.pop("_last_update_changed", None)

                save_tracking_data()
                await move_channel_if_delivered(pkg)
                await rename_channel_for_stage(pkg)

                channel_id = pkg.get("channel_id")
                if not channel_id:
                    continue

                channel = bot.get_channel(int(channel_id))
                if channel is None:
                    pkg["active"] = False
                    save_tracking_data()
                    continue

                embed = TrackingEmbed.from_package(pkg, compact=False)
                owner_id = int(user_id)
                tracking_number = pkg["tracking_number"]
                company = pkg.get("company", "")

                main_msg_id = pkg.get("message_id")
                main_msg = None
                if main_msg_id:
                    try:
                        main_msg = await channel.fetch_message(int(main_msg_id))
                    except Exception:
                        main_msg = None

                view = OrderView(owner_id, tracking_number, company, pkg.get("guild_id"), compact=False)

                if main_msg is None:
                    main_msg = await channel.send(embed=embed, view=view)
                    pkg["message_id"] = main_msg.id
                    save_tracking_data()
                else:
                    await main_msg.edit(embed=embed, view=view)

                # تنظيف قديم: التأكد من وجود رسالة رئيسية واحدة فقط لحالة الطلب
                if not pkg.get("cleaned_single_message"):
                    try:
                        async for msg in channel.history(limit=50):
                            if msg.author == bot.user and msg.id != main_msg.id:
                                if msg.embeds:
                                    desc = msg.embeds[0].description or ""
                                    if tracking_number in desc:
                                        try:
                                            await msg.delete()
                                        except Exception:
                                            pass
                        pkg["cleaned_single_message"] = True
                        save_tracking_data()
                    except Exception:
                        pass

                if not changed:
                    await asyncio.sleep(1)
                    continue

                if stage_changed or last_update_changed:
                    user = await bot.fetch_user(owner_id)
                    reasons = []
                    if stage_changed:
                        reasons.append(
                            f"status: {STATUS_TEXT.get(pkg.get('stage', ''), pkg.get('stage', ''))}"
                        )
                    if last_update_changed:
                        reasons.append("last update")

                    reason_text = " & ".join(reasons) if reasons else "update"

                    # إشعارات ذكية محسّنة
                    notification_emoji = "📬"
                    notification_title = f"New {reason_text} for your order"
                    
                    # إشعارات خاصة حسب الحالة
                    current_stage = pkg.get("stage", "")
                    if current_stage == "delivered":
                        notification_emoji = "🎉"
                        notification_title = "🎉 Your order has been delivered!"
                    elif current_stage == "out_for_delivery":
                        notification_emoji = "🚚"
                        notification_title = "🚚 Your order is out for delivery!"
                    elif current_stage == "transit":
                        notification_emoji = "✈️"
                        notification_title = "✈️ Your order is in transit"
                    
                    # تحقق من تأخر ETA
                    eta_str = pkg.get("eta", "")
                    is_overdue = False
                    days_overdue = 0
                    if eta_str:
                        try:
                            eta_date = datetime.fromisoformat(eta_str.replace("Z", "+00:00"))
                            days_until = (eta_date - now).days
                            if days_until < 0:
                                is_overdue = True
                                days_overdue = abs(days_until)
                        except Exception:
                            pass
                    
                    # تحقق من قرب التسليم (24 ساعة قبل ETA)
                    eta_warning = ""
                    if eta_str and not is_overdue:
                        try:
                            eta_date = datetime.fromisoformat(eta_str.replace("Z", "+00:00"))
                            hours_until = (eta_date - now).total_seconds() / 3600
                            if 0 < hours_until <= 24:
                                eta_warning = f"\n⏰ **ETA in {int(hours_until)} hours!**"
                        except Exception:
                            pass

                    if pkg.get("channel_notifications", True):
                        notification_content = f"{user.mention} {notification_emoji} {notification_title}"
                        if is_overdue:
                            notification_content += f"\n⚠️ **ETA passed {days_overdue} day(s) ago**"
                        elif eta_warning:
                            notification_content += eta_warning
                        
                        await channel.send(content=notification_content)

                    if pkg.get("dm_enabled", True):
                        try:
                            dm_content = (
                                f"📦 **Update for your order `{tracking_number}`**\n"
                                f"🏢 {COMPANY_DISPLAY.get(company, company.upper() or 'Unknown')}\n"
                                f"📊 Status: {STATUS_TEXT.get(current_stage, current_stage)}\n"
                                f"🌍 Location: {pkg.get('location', 'Unknown')}\n"
                            )
                            
                            if is_overdue:
                                dm_content += f"\n⚠️ **⚠️ WARNING: ETA passed {days_overdue} day(s) ago!**"
                            elif eta_warning:
                                dm_content += eta_warning
                            
                            dm_content += f"\n📍 Server: **{channel.guild.name}**, channel: {channel.mention}"
                            
                            await user.send(
                                content=dm_content,
                                embed=embed,
                            )
                        except Exception:
                            pass
                    
                    # إشعار خاص عند وصول الطرد للدولة (قريب من التسليم)
                    location = pkg.get("location", "").lower()
                    if location and ("bahrain" in location or "delivery" in location or "local" in location):
                        if current_stage in ["transit", "out_for_delivery"]:
                            if pkg.get("channel_notifications", True):
                                await channel.send(
                                    content=f"{user.mention} 🎯 **Your order is nearby!** It's in your country and should arrive soon!"
                                )

                await asyncio.sleep(1)

            except Exception as e:
                logger.error(f"Error checking tracking for {pkg.get('tracking_number')}: {e}")


@check_tracking_updates.before_loop
async def before_check_tracking_updates():
    await bot.wait_until_ready()
    print("⏳ Waiting for bot to be ready before starting auto updates...")


# ─────────────────────────────
# Run bot
# ─────────────────────────────
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
