# gogordian_importer.py
import os
import re
import io
import json
import time
import mimetypes
import urllib.parse
from typing import List, Dict, Optional, Tuple

import requests
from bs4 import BeautifulSoup

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager
from urllib.parse import urljoin, urlparse


# ========= Image conversion (WEBP → JPEG) =========
CONVERT_WEBP_TO_JPEG = True  # enable to maximize backend compatibility
try:
    from PIL import Image  # pip install pillow (and optionally pillow-avif-plugin)
except Exception:
    Image = None

# =========================
# ---- CONFIG (EDIT) ------
# =========================
BACKEND_URL = "https://www.propertpro.com/api/properties/create_property/"
ACCESS_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ0b2tlbl90eXBlIjoiYWNjZXNzIiwiZXhwIjoxNzU3MDEwMDUwLCJpYXQiOjE3NTY5OTU2NTAsImp0aSI6Ijc4MWVlOTc1OTkxYzQwNWJiNjk4MWQyZWE4NGEyOGJmIiwidXNlcl9pZCI6MTJ9.n8dM-hITyCAtVmKLN1NNF7nwYRy-zLX9gIZWoP7UL5M"
URLS_FILE = "property_urls.txt"   # one URL per line (gogordian listing URLs)
HEADLESS = True                   # set False for debugging browser
POST_ENABLED = True               # set False to dry-run (no POSTs)

# Fallbacks if site lacks some info
DEFAULT_COUNTRY = "Cyprus"
DEFAULT_PROPERTY_STATUS = "for_sale"
DEFAULT_IS_PUBLISHED = True
DEFAULT_IS_FEATURED = False
DEFAULT_CONTACT_PHONE = "+0000000000"
DEFAULT_CONTACT_EMAIL = "info@propertpro.com"

# Throttling & timeouts
REQUEST_DELAY_SEC = 1.0
HTTP_TIMEOUT = 25

# Max images per listing to upload
MAX_IMAGES = 30

# =========================
# ---- HELPERS ------------
# =========================
def init_driver() -> webdriver.Chrome:
    chrome_options = Options()
    if HEADLESS:
        chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--window-size=1366,900")
    return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)

def first_text(soup: BeautifulSoup, selectors: List[str]) -> Optional[str]:
    for sel in selectors:
        try:
            el = soup.select_one(sel)
            if el:
                txt = el.get_text(" ", strip=True)
                if txt:
                    return txt
        except Exception:
            continue
    return None

def session_with_cookies(driver, page_url: str) -> requests.Session:
    """
    Build a requests.Session that carries over Selenium cookies and
    sets a proper UA + Referer so the image host doesn't block us.
    """
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0",
        "Referer": page_url,
        "Accept": "image/*",
    })
    host = urlparse(page_url).hostname or ""
    for ck in driver.get_cookies():
        # Keep only cookies relevant to the current host
        dom = ck.get("domain") or host
        if dom.startswith("."):
            dom = dom[1:]
        if dom in host or host.endswith(dom):
            s.cookies.set(ck["name"], ck["value"], domain=ck.get("domain"), path=ck.get("path", "/"))
    return s

def all_texts(soup: BeautifulSoup, selectors: List[str]) -> List[str]:
    out = []
    for sel in selectors:
        for el in soup.select(sel):
            t = el.get_text(" ", strip=True)
            if t:
                out.append(t)
    return out

def _sniff_image_format(b: bytes) -> Optional[str]:
    if not b or len(b) < 12:
        return None
    # JPEG
    if b[:2] == b"\xFF\xD8":
        return "jpeg"
    # PNG
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    # GIF
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    # WEBP (RIFF...WEBP)
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "webp"
    # AVIF (ftypavif/…)
    if b[:4] == b"\x00\x00\x00\x20" and b[4:8] == b"ftyp" and b[8:12] in (b"avif", b"heic", b"heix", b"hevc"):
        return "avif"
    return None

def to_int(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    m = re.search(r"(\d+)", text.replace("\xa0", " "))
    return int(m.group(1)) if m else None

def _parse_money_to_str(s: Optional[str]) -> Optional[str]:
    """
    Robustly parse currency strings:
    - Handles 280.000 -> 280000.00  (dot as thousands)
    - Handles 180,000 -> 180000.00  (comma as thousands)
    - Handles 1.200.000,50 -> 1200000.50  (EU format)
    - Handles 1,200,000.50 -> 1200000.50  (US format)
    - Ignores words like '+ VAT', '/month', currency symbols, spaces
    """
    if not s:
        return None
    s = s.strip()
    # keep only digits, dot, comma
    raw = re.sub(r"[^0-9\.,]", "", s)
    if not raw:
        return None

    # If both separators present, decide decimal by last occurrence
    if "." in raw and "," in raw:
        last_dot = raw.rfind(".")
        last_comma = raw.rfind(",")
        if last_comma > last_dot:
            # EU style: thousands='.', decimal=','
            thousands, decimal = ".", ","
        else:
            # US style: thousands=',', decimal='.'
            thousands, decimal = ",", "."
        tmp = raw.replace(thousands, "")
        tmp = tmp.replace(decimal, ".")
        try:
            val = float(tmp)
            return f"{val:.2f}"
        except Exception:
            pass

    # Only one separator or none
    if "," in raw and "." not in raw:
        # Could be thousands or decimal; if last group has exactly 2 digits, treat as decimal
        parts = raw.split(",")
        if len(parts[-1]) == 2:
            tmp = "".join(parts[:-1]) + "." + parts[-1]
        else:
            tmp = "".join(parts)  # thousands commas
        try:
            val = float(tmp)
            return f"{val:.2f}"
        except Exception:
            return None

    if "." in raw and "," not in raw:
        parts = raw.split(".")
        # If last group has 2 digits, assume decimal. Else, dot is thousands sep.
        if len(parts[-1]) == 2:
            tmp = ".".join(parts)
        else:
            tmp = "".join(parts)
        try:
            val = float(tmp)
            return f"{val:.2f}"
        except Exception:
            return None

    # No separators, plain integer
    try:
        val = float(raw)
        return f"{val:.2f}"
    except Exception:
        return None

def normalize_whitespace(s: Optional[str]) -> Optional[str]:
    if not s:
        return s
    return re.sub(r"\s+", " ", s).strip()

def nonempty(text: Optional[str]) -> str:
    return (text or "").strip()

def is_date_yyyy_mm_dd(s: Optional[str]) -> bool:
    if not s:
        return False
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}$", s))

# ---------- Location helpers ----------
def split_parts(s: Optional[str]) -> List[str]:
    if not s:
        return []
    raw = re.split(r"\s*,\s*|--", s)
    return [p.strip() for p in raw if p and p.strip()]

def prefer(a: Optional[str], b: Optional[str]) -> Optional[str]:
    return a if a and a.strip() else (b if b and b.strip() else None)

def extract_location_from_jsonld(soup: BeautifulSoup) -> Dict[str, Optional[str]]:
    out = {"city": None, "region": None, "country": None, "postal_code": None, "street": None, "lat": None, "lng": None}
    for sc in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(sc.string or "")
            nodes = data if isinstance(data, list) else [data]
            for obj in nodes:
                addr = obj.get("address")
                if isinstance(addr, list) and addr:
                    addr = addr[0]
                if isinstance(addr, dict):
                    out["city"] = prefer(out["city"], addr.get("addressLocality"))
                    out["region"] = prefer(out["region"], addr.get("addressRegion"))
                    out["country"] = prefer(out["country"], addr.get("addressCountry"))
                    out["postal_code"] = prefer(out["postal_code"], addr.get("postalCode"))
                    out["street"] = prefer(out["street"], addr.get("streetAddress"))
                geo = obj.get("geo") or {}
                if isinstance(geo, dict):
                    if geo.get("latitude") is not None:
                        out["lat"] = prefer(out["lat"], str(geo.get("latitude")))
                    if geo.get("longitude") is not None:
                        out["lng"] = prefer(out["lng"], str(geo.get("longitude")))
        except Exception:
            continue
    return out

def extract_location_from_microdata(soup: BeautifulSoup) -> Dict[str, Optional[str]]:
    out = {"city": None, "region": None, "country": None, "postal_code": None, "street": None}
    addr = soup.select_one('[itemprop="address"], [itemtype*="PostalAddress"]')
    if not addr:
        return out
    def get_itemprop(name):
        el = addr.select_one(f'[itemprop="{name}"]')
        return el.get_text(" ", strip=True) if el else None
    out["city"] = get_itemprop("addressLocality")
    out["region"] = get_itemprop("addressRegion")
    out["country"] = get_itemprop("addressCountry")
    out["postal_code"] = get_itemprop("postalCode")
    out["street"] = get_itemprop("streetAddress")
    return out

def extract_location_from_breadcrumbs(soup: BeautifulSoup) -> Dict[str, Optional[str]]:
    out = {"city": None, "region": None, "country": None}
    bc = soup.find(attrs={"class": re.compile(r"breadcrumb", re.I)})
    if not bc:
        bc = soup.find("nav", attrs={"aria-label": re.compile(r"breadcrumb", re.I)})
    if not bc:
        return out
    items = [t.strip() for t in bc.get_text(">", strip=True).split(">") if t.strip()]
    # heuristics: last items are city/region/country
    if items:
        out["country"] = items[-1] if len(items) >= 1 else None
        out["region"] = items[-2] if len(items) >= 2 else None
        out["city"] = items[-3] if len(items) >= 3 else None
    return out

def extract_location_from_url(url: str) -> Dict[str, Optional[str]]:
    out = {"city": None, "region": None}
    try:
        path = urllib.parse.urlparse(url).path
        if "/property/" in path:
            slug = path.split("/property/")[-1]
            slug = slug.rsplit("-", 1)[0]  # drop trailing numeric id
            parts = split_parts(slug.replace("-", " "))
            # pick last 1-2 tokens as area hints
            if parts:
                out["city"] = parts[-1].title()
                if len(parts) >= 2:
                    out["region"] = parts[-2].title()
    except Exception:
        pass
    return out

# ---------- Description & images ----------
def extract_description(soup: BeautifulSoup) -> str:
    # Look for heading text, then read siblings
    for h in soup.find_all(["h2", "h3"]):
        if "description" in h.get_text(" ", strip=True).lower():
            blocks = []
            node = h.find_next_sibling()
            hops = 0
            while node and hops < 6:
                txt = node.get_text("\n", strip=True)
                if txt:
                    blocks.append(txt)
                node = node.find_next_sibling()
                hops += 1
            if blocks:
                return "\n".join(blocks).strip()
    for sel in ["#Description", "div#description", ".description", "[itemprop='description']"]:
        el = soup.select_one(sel)
        if el:
            t = el.get_text("\n", strip=True)
            if t:
                return t.strip()
    page_text = soup.get_text(" ", strip=True)
    return page_text[:400].strip() if page_text else "No description provided."

def extract_images_robust(html: str, soup: BeautifulSoup) -> List[str]:
    urls: List[str] = []
    # JSON-LD images
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            nodes = data if isinstance(data, list) else [data]
            for obj in nodes:
                imgs = obj.get("image") or obj.get("images")
                if isinstance(imgs, str):
                    urls.append(imgs)
                elif isinstance(imgs, list):
                    urls.extend([u for u in imgs if isinstance(u, str)])
        except Exception:
            pass
    # og:image
    for m in soup.select('meta[property="og:image"], meta[name="og:image"]'):
        c = m.get("content")
        if c:
            urls.append(c.strip())
    # <img> tags (incl. lazy attrs)
    for sel in ["img", "picture source"]:
        for el in soup.select(sel):
            for attr in ["srcset", "data-srcset", "data-src", "data-lazy", "data-original", "src"]:
                val = (el.get(attr) or "").strip()
                if not val:
                    continue
                if "srcset" in attr:
                    parts = [p.strip().split(" ")[0] for p in val.split(",") if p.strip()]
                    if parts:
                        urls.append(max(parts, key=len))
                else:
                    urls.append(val)
    # Clean & dedupe
    cleaned, seen = [], set()
    for u in urls:
        u = u.replace("/thumbs/", "/").replace("/thumb/", "/")
        if u and u not in seen and not u.startswith("data:"):
            seen.add(u)
            cleaned.append(u)
    return cleaned

# =========================
# ---- AMENITIES MAP ------
# =========================
# Map keyword(s) → (id, label, category) matching your platform
AMENITY_KEYWORDS = [
    # --- Parking ---
    (["parking", "parking space", "car park", "open parking", "covered parking",
      "θέση στάθμευσης", "σταθμευ", "παρκινγκ"],
     ("parking_space", "Parking Space", "Parking")),

    # --- Exterior ---
    (["balcony", "veranda", "terrace", "patio", "βεράντα", "μπαλκόνι", "ταράτσα"],
     ("balcony", "Balcony", "Exterior")),
    (["garden", "yard", "private garden", "κήπος", "αυλή"],
     ("garden", "Garden", "Exterior")),
    (["roof deck", "rooftop", "roof terrace", "δώμα"],
     ("roof_deck", "Roof Deck", "Exterior")),
    (["bbq", "barbeque", "barbecue", "μπάρμπεκιου"],
     ("bbq", "BBQ", "Exterior")),

    # --- Building ---
    (["corner", "γωνιακό"],
     ("corner", "Corner", "Building")),
    (["elevator", "lift", "ασανσέρ", "ανελκυστήρας"],
     ("elevator", "Elevator", "Building")),

    # --- Interior ---
    (["bright", "luminous", "φωτεινό", "ηλιόλουστο"],
     ("bright", "Bright", "Interior")),
    (["storage", "storeroom", "storage room", "αποθήκη"],
     ("storage", "Storage Space", "Interior")),
    (["fireplace", "τζάκι"],
     ("fireplace", "Fireplace", "Interior")),

    # --- Climate ---
    (["air conditioning", "a/c", "ac", "κλιματισμός"],
     ("air_conditioning", "Air Conditioning", "Climate")),
    (["central heating", "radiator heating", "θέρμανση", "κεντρική θέρμανση"],
     ("central_heating", "central_heating", "Unknown")),  # as per your sample

    # --- Utilities ---
    (["solar water", "solar heater", "ηλιακός", "ηλιακός θερμοσίφωνας"],
     ("solar", "Solar Water Heater", "Utilities")),
    (["security", "alarm", "cctv", "security door", "gated", "συναγερμός", "πόρτα ασφαλείας"],
     ("security", "security", "Unknown")),

    # --- Location ---
    (["view", "sea view", "mountain view", "θέα", "θέα θάλασσα"],
     ("view", "View", "Location")),
    (["waterfront", "beachfront", "on the sea", "by the sea", "παραθαλάσσιο", "παραλία"],
     ("waterfront", "Waterfront", "Location")),

    # --- Policy ---
    (["pets", "pet friendly", "pets allowed", "κατοικίδια"],
     ("pet_friendly", "Pet Friendly", "Policy")),
]

def map_amenities(features_texts: List[str]) -> Tuple[List[str], List[Dict[str, str]]]:
    raw_ids_ordered: List[str] = []
    detailed_map: Dict[str, Dict[str, str]] = {}

    blob = " | ".join(ft.lower() for ft in features_texts)

    for keywords, (aid, label, category) in AMENITY_KEYWORDS:
        kws = keywords if isinstance(keywords, list) else [keywords]
        if any(k.lower() in blob for k in kws):
            if aid not in raw_ids_ordered:
                raw_ids_ordered.append(aid)
            detailed_map[aid] = {"id": aid, "label": label, "category": category}

    amenities_detailed = [detailed_map[aid] for aid in raw_ids_ordered if aid in detailed_map]
    return raw_ids_ordered, amenities_detailed

# =========================
# ---- PARSER (site) ------
# =========================
def gather_feature_texts(soup: BeautifulSoup) -> List[str]:
    texts: List[str] = []

    # 1) Headers that say Features/Main/Further Features → collect next <ul>/<ol> items
    for h in soup.find_all(["h2", "h3"]):
        ht = h.get_text(" ", strip=True).lower()
        if any(k in ht for k in ["features", "main features", "further features", "other features",
                                 "amenities", "χαρακτηριστικ", "παροχές"]):
            ul = h.find_next_sibling()
            hop = 0
            while ul and hop < 4 and ul.name not in ("ul", "ol"):
                ul = ul.find_next_sibling()
                hop += 1
            if ul and ul.name in ("ul", "ol"):
                for li in ul.find_all("li"):
                    t = li.get_text(" ", strip=True)
                    if t:
                        texts.append(t)

    # 2) Any list with amenity/feature in class name
    for ul in soup.find_all("ul"):
        cls = " ".join(ul.get("class", []))
        if re.search(r"(amenit|feature|χαρακτηριστικ|παροχ)", cls, re.I):
            for li in ul.find_all("li"):
                t = li.get_text(" ", strip=True)
                if t:
                    texts.append(t)

    # 3) Data attributes
    for el in soup.select("[data-amenity]"):
        t = el.get("data-amenity")
        if t:
            texts.append(t)

    # 4) Tokenize description / page text by common separators (helps Greek comma-separated lists)
    page_text = soup.get_text("\n", strip=True)
    for chunk in re.split(r"[•·\-\u2022,\n;/|]+", page_text):
        c = chunk.strip()
        if 2 <= len(c) <= 60:  # lightweight noise filter
            texts.append(c)

    # dedupe while preserving order
    seen, out = set(), []
    for t in texts:
        k = t.lower()
        if k not in seen:
            seen.add(k)
            out.append(t)
    return out

def parse_price(soup: BeautifulSoup, html: str) -> Optional[str]:
    """
    Prefer JSON-LD offers.price / aggregateOffer.lowPrice/highPrice,
    then itemprop price/meta, then visible '.price' blocks,
    then a last-resort regex near currency symbols.
    Always return '123456.78' string or None.
    """
    # 1) JSON-LD
    for sc in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(sc.string or "")
            nodes = data if isinstance(data, list) else [data]
            for obj in nodes:
                offers = obj.get("offers")
                if not offers:
                    continue
                # offers can be dict or list
                off_list = offers if isinstance(offers, list) else [offers]
                for off in off_list:
                    # aggregate
                    for key in ("price", "lowPrice", "highPrice", "minPrice", "maxPrice"):
                        if key in off and off.get(key):
                            s_val = str(off.get(key))
                            parsed = _parse_money_to_str(s_val)
                            if parsed:
                                return parsed
        except Exception:
            pass

    # 2) Microdata / itemprop price
    for el in soup.select('[itemprop="price"], meta[itemprop="price"], [content*="price"]'):
        txt = el.get("content") or el.get_text(" ", strip=True)
        parsed = _parse_money_to_str(txt)
        if parsed:
            return parsed

    # 3) Visible price blocks (tighten selectors to avoid areas)
    candidates = []
    # common price containers
    for sel in [
        ".price", ".property-price", ".listing-price", "[data-testid='price']",
        "[class*='price']", "span.price", "strong.price"
    ]:
        for el in soup.select(sel):
            t = el.get_text(" ", strip=True)
            if t:
                candidates.append(t)

    for t in candidates:
        # ignore obvious non-price lines
        if re.search(r"\b(area|sqm|m2|m²|plot|bed|bath|storey|floor)\b", t, re.I):
            continue
        parsed = _parse_money_to_str(t)
        if parsed:
            return parsed

    # 4) Last-resort regex: look for a currency marker near a number
    # Try to find a €/$ sign or 'EUR' within 50 chars of a number
    m = re.search(r"(€|EUR|\$)\s*([0-9][\d\.\, ]*(?:[.,]\d{2})?)", html, re.I)
    if m:
        parsed = _parse_money_to_str(m.group(0))
        if parsed:
            return parsed

    # Final fallback: first big number that looks like a money amount
    m2 = re.search(r"\b\d{1,3}(?:[.,]\d{3}){1,4}(?:[.,]\d{2})?\b", html)
    if m2:
        parsed = _parse_money_to_str(m2.group(0))
        if parsed:
            return parsed

    return None


def parse_gogordian(html: str, url: str) -> Optional[Dict]:
    soup = BeautifulSoup(html, "html.parser")

    # Title & price
    title = normalize_whitespace(first_text(soup, ["h1", "h1.property-title", "div.property-title h1"]))
    price = parse_price(soup, html)

    # Description
    description = extract_description(soup) or "No description provided."

    # Location: merge multiple sources
    loc_jsonld = extract_location_from_jsonld(soup)
    loc_micro = extract_location_from_microdata(soup)
    loc_bc = extract_location_from_breadcrumbs(soup)
    loc_str = normalize_whitespace(first_text(soup, [".location", ".property-location", "[data-testid='location']", "header + * .location"])) or ""
    loc_url = extract_location_from_url(url)

    city = prefer(loc_jsonld.get("city"), prefer(loc_micro.get("city"), prefer(loc_bc.get("city"), prefer(loc_url.get("city"), None))))
    region = prefer(loc_jsonld.get("region"), prefer(loc_micro.get("region"), prefer(loc_bc.get("region"), prefer(loc_url.get("region"), None))))
    country = prefer(loc_jsonld.get("country"), prefer(loc_micro.get("country"), prefer(loc_bc.get("country"), DEFAULT_COUNTRY)))
    postal_code = prefer(loc_jsonld.get("postal_code"), loc_micro.get("postal_code"))
    street = prefer(loc_jsonld.get("street"), loc_micro.get("street"))

    # Lat/Lng
    latitude = loc_jsonld.get("lat")
    longitude = loc_jsonld.get("lng")
    if not (latitude and longitude):
        mlat = re.search(r'"latitude"\s*:\s*([0-9\.\-]+)', html)
        mlng = re.search(r'"longitude"\s*:\s*([0-9\.\-]+)', html)
        if mlat and mlng:
            latitude = latitude or mlat.group(1)
            longitude = longitude or mlng.group(1)

    # If still no city/region, try visible location string
    if (not city or not region) and loc_str:
        parts = split_parts(loc_str)
        if parts:
            if not city:
                city = parts[0]
            if len(parts) >= 2 and not region:
                region = parts[1]
            if len(parts) >= 3 and not country:
                country = parts[-1]

    # Page text for labeled values
    page_txt = soup.get_text("\n", strip=True)

    def find_labeled_number(labels: List[str]) -> Optional[float]:
        for label in labels:
            m = re.search(rf"{re.escape(label)}\s*[:\-]?\s*([\d\.,]+)\s*(?:m2|m²|sqm|sq\.? m)?",
                          page_txt, re.IGNORECASE)
            if m:
                # area can have thousands separators; use the same robust parser then cast to int/float
                parsed = _parse_money_to_str(m.group(1))
                if parsed is not None:
                    return float(parsed)
        return None

    area_num = find_labeled_number(["Covered Area", "Total Covered Area", "Internal Area", "Area"])
    lot_size_num = find_labeled_number(["Plot", "Land Area", "Plot Size", "Lot Size"])

    def find_count(label_keywords: List[str]) -> Optional[int]:
        for kw in label_keywords:
            m = re.search(rf"\b{kw}\b\s*[:\-]?\s*(\d+)", page_txt, re.IGNORECASE)
            if m:
                return int(m.group(1))
        return None

    bedrooms = find_count(["Rooms", "Bedrooms"])
    bathrooms = find_count(["Baths", "Bathrooms", "WCs"])
    if bathrooms is None:
        mhalf = re.search(r"\b(Baths?|Bathrooms?)\b\s*[:\-]?\s*(\d+(?:\.\d+)?)", page_txt, re.IGNORECASE)
        if mhalf:
            bathrooms = mhalf.group(2)

    total_floors = find_count(["Floors", "Total Floors", "Storeys", "Stories"])
    floor_level = find_count(["Floor", "Level"])

    # Year built: not always shown; try several phrasings
    year_built = None
    for pat in [
        r"\b(?:Year\s*Built|Build\s*Year|Construction\s*Year)\b\s*[:\-]?\s*(\d{4})",
        r"\bBuilt\s+in\s+(\d{4})\b",
        r"\bConstruction\s+(\d{4})\b",
    ]:
        m = re.search(pat, page_txt, re.IGNORECASE)
        if m:
            y = int(m.group(1))
            if 1900 <= y <= 2100:
                year_built = y
                break

    # Property type heuristic
    tlow = (title or "").lower()
    if any(k in tlow for k in ["apartment", "flat", "studio"]):
        property_type = "apartment"
    elif any(k in tlow for k in ["maisonette", "semi-detached", "detached", "villa", "house"]):
        property_type = "house"
    else:
        property_type = "house"

    # Features / amenities (robust traversal)
    feature_texts = gather_feature_texts(soup)
    amenities_raw, amenities_detailed = map_amenities(feature_texts)

    # Contact (fallbacks)
    contact_phone = ""
    contact_email = ""
    mphone = re.search(r"(\+?\d[\d\s\-]{6,})", page_txt)
    if mphone:
        contact_phone = mphone.group(1).strip()
    memail = re.search(r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})", page_txt)
    if memail:
        contact_email = memail.group(1).strip()
    contact_phone = nonempty(contact_phone) or DEFAULT_CONTACT_PHONE
    contact_email = nonempty(contact_email) or DEFAULT_CONTACT_EMAIL

    # Images (required)
    images = extract_images_robust(html, soup)
    if not images:
        print("[SKIP] No images found; server requires images.")
        return None

    # Final payload
    payload: Dict = {
        "title": title or "",
        "description": nonempty(description) or "No description provided.",
        "price": price if price is not None else "0.00",
        "property_type": property_type,
        "location": normalize_whitespace(f"{city or ''}, {region or ''}, {country or DEFAULT_COUNTRY}").strip(" ,"),
        "country": country or DEFAULT_COUNTRY,
        "region": region or "",
        "city": city or "",
        "postal_code": postal_code or "",
        "street": street or "",
        "latitude": latitude or "",
        "longitude": longitude or "",
        "bedrooms": bedrooms if isinstance(bedrooms, int) else (int(float(bedrooms)) if isinstance(bedrooms, str) and re.match(r"^\d+(\.\d+)?$", bedrooms) else 0),
        "bathrooms": str(bathrooms) if bathrooms is not None else "0",
        "area": int(area_num) if area_num is not None else 0,
        "year_built": year_built if year_built is not None else 0,
        "parking_spaces": 0,
        "lot_size": f"{lot_size_num:.2f}" if lot_size_num is not None else "0.00",
        "energy_rating": "",
        "construction_material": None,
        "floor_level": floor_level if floor_level is not None else 0,
        "total_floors": total_floors if total_floors is not None else 0,
        # "available_from" only added if valid
        "property_status": DEFAULT_PROPERTY_STATUS,
        "contact_phone": contact_phone,
        "contact_email": contact_email,
        "virtual_tour_url": None,
        "video_url": None,
        "amenities_raw": amenities_raw,
        "amenities_detailed": amenities_detailed,
        "is_published": DEFAULT_IS_PUBLISHED,
        "is_featured": DEFAULT_IS_FEATURED,
        "images": images,  # list of URLs, downloaded then uploaded as files
    }

    # energy rating from page if present
    meng = re.search(r"\bEnergy\s*Class\b\s*[:\-]?\s*([A-D][+\-]?)", page_txt, re.IGNORECASE)
    if meng:
        payload["energy_rating"] = meng.group(1).upper()

    return payload

# =========================
# ---- IMAGE DOWNLOAD -----
# =========================
def guess_filename(url: str, idx: int) -> str:
    name = os.path.basename(url.split("?")[0]).strip() or f"image_{idx}"
    # ensure extension; fixed later by MIME if missing
    return name

def guess_mime_from_ext(filename: str) -> str:
    mime, _ = mimetypes.guess_type(filename)
    return mime or "application/octet-stream"

def fetch_image_bytes(session: requests.Session, url: str, idx: int) -> Optional[Tuple[str, bytes, str]]:
    try:
        r = session.get(url, timeout=HTTP_TIMEOUT, stream=True)
        if r.status_code != 200:
            r = session.get(url, timeout=HTTP_TIMEOUT)
        r.raise_for_status()

        content = r.content or b""
        # Skip empty/tiny files (< 5 KB)
        if len(content) < 5 * 1024:
            return None

        # MIME from server, but we'll verify ourselves too
        mime = (r.headers.get("Content-Type") or "").lower()

        # Skip obvious non-images (html/svg)
        if "text/html" in mime or "svg" in mime:
            return None

        # Sniff actual format from bytes
        sniff = _sniff_image_format(content)

        # If still unknown, try Pillow verify
        fmt_from_pillow = None
        if sniff is None and Image is not None:
            try:
                im = Image.open(io.BytesIO(content))
                im.verify()  # quick integrity check
                fmt_from_pillow = (im.format or "").lower()
            except Exception:
                return None

        fmt = sniff or fmt_from_pillow or ""
        # Handle formats
        if fmt == "webp" and CONVERT_WEBP_TO_JPEG and Image is not None:
            try:
                im = Image.open(io.BytesIO(content)).convert("RGB")
                out_io = io.BytesIO()
                im.save(out_io, format="JPEG", quality=90)
                content = out_io.getvalue()
                fmt = "jpeg"
                mime = "image/jpeg"
            except Exception:
                return None
        elif fmt == "avif":
            if Image is not None:
                try:
                    im = Image.open(io.BytesIO(content)).convert("RGB")
                    out_io = io.BytesIO()
                    im.save(out_io, format="JPEG", quality=90)
                    content = out_io.getvalue()
                    fmt = "jpeg"
                    mime = "image/jpeg"
                except Exception:
                    return None
            else:
                return None
        elif fmt in ("jpeg", "png", "gif"):
            if not mime.startswith("image/"):
                mime = f"image/{fmt}"
        else:
            return None

        # Filename & extension
        filename = guess_filename(url, idx)
        if fmt == "jpeg" and not filename.lower().endswith((".jpg", ".jpeg")):
            filename += ".jpg"
        elif fmt == "png" and not filename.lower().endswith(".png"):
            filename += ".png"
        elif fmt == "gif" and not filename.lower().endswith(".gif"):
            filename += ".gif"

        return filename, content, mime
    except Exception:
        return None

def _normalize_img_url(u: str, page_url: str) -> str:
    if not u:
        return ""
    u = u.strip()
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("/"):
        return urljoin(page_url, u)
    return u

def download_images(image_urls: List[str], page_url: str, driver, max_images: int = MAX_IMAGES) -> List[Tuple[str, bytes, str]]:
    out: List[Tuple[str, bytes, str]] = []
    s = session_with_cookies(driver, page_url)
    seen = set()
    idx = 0

    for raw in image_urls:
        if len(out) >= max_images:
            break
        url = _normalize_img_url(raw, page_url)
        if not url or url in seen:
            continue
        seen.add(url)
        idx += 1

        res = fetch_image_bytes(s, url, idx)   # verified fetcher
        if res:
            out.append(res)

    return out

# =========================
# ---- NETWORKING ----------
# =========================
def send_to_backend(property_data: Dict, page_url: str, driver) -> Tuple[int, str]:
    """
    POST as multipart/form-data.
    - Scalar fields in 'data'
    - Each image as file with key 'images[]' (so the API gets the full list)
    - amenities_* JSON-stringified in 'data'
    """
    def as_str(v):
        if v is None:
            return ""
        if isinstance(v, bool):
            return "true" if v else "false"
        return str(v)

    # Download images with Referer
    image_urls = property_data.get("images") or []
    image_blobs = download_images(image_urls, page_url, driver)
    if not image_blobs:
        return 400, '{"images":["No downloadable images"]}'

    data = {
        "title": as_str(property_data.get("title")),
        "description": as_str(property_data.get("description")),
        "price": as_str(property_data.get("price")),
        "property_type": as_str(property_data.get("property_type")),
        "location": as_str(property_data.get("location")),
        "country": as_str(property_data.get("country")),
        "region": as_str(property_data.get("region")),
        "city": as_str(property_data.get("city")),
        "postal_code": as_str(property_data.get("postal_code")),
        "street": as_str(property_data.get("street")),
        "latitude": as_str(property_data.get("latitude")),
        "longitude": as_str(property_data.get("longitude")),
        "bedrooms": as_str(property_data.get("bedrooms")),
        "bathrooms": as_str(property_data.get("bathrooms")),
        "area": as_str(property_data.get("area")),
        "year_built": as_str(property_data.get("year_built")),
        "parking_spaces": as_str(property_data.get("parking_spaces")),
        "lot_size": as_str(property_data.get("lot_size")),
        "energy_rating": as_str(property_data.get("energy_rating")),
        "construction_material": as_str(property_data.get("construction_material")),
        "floor_level": as_str(property_data.get("floor_level")),
        "total_floors": as_str(property_data.get("total_floors")),
        "property_status": as_str(property_data.get("property_status")),
        "contact_phone": as_str(property_data.get("contact_phone")) or DEFAULT_CONTACT_PHONE,
        "contact_email": as_str(property_data.get("contact_email")) or DEFAULT_CONTACT_EMAIL,
        "virtual_tour_url": as_str(property_data.get("virtual_tour_url")),
        "video_url": as_str(property_data.get("video_url")),
        "is_published": as_str(property_data.get("is_published")),
        "is_featured": as_str(property_data.get("is_featured")),
        "amenities_raw": json.dumps(property_data.get("amenities_raw") or [], ensure_ascii=False),
        "amenities_detailed": json.dumps(property_data.get("amenities_detailed") or [], ensure_ascii=False),
    }
    # Only include available_from if valid
    if is_date_yyyy_mm_dd(property_data.get("available_from")):
        data["available_from"] = property_data["available_from"]

    # Files: use images[] so backend picks up all
    files: List[Tuple[str, Tuple[str, bytes, str]]] = []
    for (fname, content, mime) in image_blobs:
        files.append(("images[]", (fname, content, mime)))

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Accept": "application/json",
        # Let requests set the multipart boundary automatically
    }

    r = requests.post(BACKEND_URL, headers=headers, data=data, files=files, timeout=40)
    return r.status_code, (r.text or "")

# =========================
# ---- MAIN FLOW ----------
# =========================
def scrape_once(driver: webdriver.Chrome, url: str) -> Optional[Dict]:
    driver.get(url)
    wait = WebDriverWait(driver, 20)
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "h1, h1.property-title, .property-title h1")))
    # Help lazy loaders
    driver.execute_script("window.scrollTo(0, Math.min(1800, document.body.scrollHeight));")
    time.sleep(0.6)
    # Extra scroll to help load galleries
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight * 0.75);")
    time.sleep(0.8)
    html = driver.page_source
    payload = parse_gogordian(html, url)
    if not payload:
        return None
    if not payload.get("title"):
        print(f"[SKIP] No title for {url}")
        return None
    if not payload.get("images"):
        print(f"[SKIP] No images (required) for {url}")
        return None
    if not payload.get("description"):
        payload["description"] = "No description provided."
    return payload

def main():
    if not ACCESS_TOKEN or ACCESS_TOKEN == "REPLACE_WITH_YOUR_ACCESS_TOKEN":
        print("[ERROR] Please set ACCESS_TOKEN or API_TOKEN env var.")
        return
    if not os.path.exists(URLS_FILE):
        print(f"[ERROR] URLs file not found: {URLS_FILE}")
        return

    with open(URLS_FILE, "r", encoding="utf-8") as f:
        urls = [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]

    if not urls:
        print("[ERROR] No URLs to process.")
        return

    driver = init_driver()

    ok, fail = 0, 0
    for url in urls:
        print(f"\n[INFO] Processing: {url}")
        try:
            payload = scrape_once(driver, url)
            if not payload:
                fail += 1
                continue

            print("[DEBUG] Payload (without image bytes):")
            preview = {k: v for k, v in payload.items() if k != "images"}
            print(json.dumps(preview, indent=2, ensure_ascii=False))
            print(f"[DEBUG] Images found: {len(payload.get('images', []))}")

            if POST_ENABLED:
                status, body = send_to_backend(payload, url, driver)
                if status in (200, 201):
                    ok += 1
                    print(f"[OK] {status} {payload.get('title')}")
                else:
                    fail += 1
                    print(f"[FAIL] {status} {payload.get('title')}")
                    print(body[:1500])
            else:
                ok += 1
                print("[DRY-RUN] Skipped POST.")
        except Exception as e:
            fail += 1
            print(f"[ERROR] {url}: {e}")

        time.sleep(max(0.2, REQUEST_DELAY_SEC))

    driver.quit()
    print(f"\n[DONE] success={ok} failed={fail}")

if __name__ == "__main__":
    main()
