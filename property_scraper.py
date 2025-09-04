# gogordian_importer.py
import os
import re
import json
import time
from datetime import datetime
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


# =========================
# ---- CONFIG (EDIT) ------
# =========================
BACKEND_URL = "https://www.propertpro.com/api/properties/create_property/"
ACCESS_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ0b2tlbl90eXBlIjoiYWNjZXNzIiwiZXhwIjoxNzU3MDEwMDUwLCJpYXQiOjE3NTY5OTU2NTAsImp0aSI6Ijc4MWVlOTc1OTkxYzQwNWJiNjk4MWQyZWE4NGEyOGJmIiwidXNlcl9pZCI6MTJ9.n8dM-hITyCAtVmKLN1NNF7nwYRy-zLX9gIZWoP7UL5M"
URLS_FILE = "property_urls.txt"  # one URL per line (e.g., gogordian listing URLs)
HEADLESS = True                  # set False for debugging browser
POST_ENABLED = True              # set False to dry-run (no POSTs)

# Default fallbacks if site lacks some info
DEFAULT_COUNTRY = "Cyprus"
DEFAULT_PROPERTY_STATUS = "for_sale"

# If your endpoint requires is_published/featured defaults:
DEFAULT_IS_PUBLISHED = True
DEFAULT_IS_FEATURED = False

# Optional: rate limit between pages (seconds)
REQUEST_DELAY_SEC = 1.0


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


def all_texts(soup: BeautifulSoup, selectors: List[str]) -> List[str]:
    out = []
    for sel in selectors:
        for el in soup.select(sel):
            t = el.get_text(" ", strip=True)
            if t:
                out.append(t)
    return out


def to_int(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    m = re.search(r"(\d+)", text.replace("\xa0", " "))
    return int(m.group(1)) if m else None


def to_float(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)", text.replace("\xa0", " "))
    if not m:
        return None
    return float(m.group(1).replace(",", "."))


def money_to_string_amount(text: Optional[str]) -> Optional[str]:
    """
    Extract a numeric amount and return as string with two decimals: e.g. '280000.00'.
    """
    f = to_float(text)
    if f is None:
        return None
    return f"{f:.2f}"


def normalize_whitespace(s: Optional[str]) -> Optional[str]:
    if not s:
        return s
    return re.sub(r"\s+", " ", s).strip()


# =========================
# ---- AMENITIES MAP ------
# =========================
# Map keyword → (id, label, category)
# Add/adjust as needed. Keywords are matched case-insensitively if they appear in feature text.
AMENITY_KEYWORDS = [
    (["parking", "garage", "car park", "open parking", "covered parking"], ("parking", "Parking", "Legacy")),
    (["balcony", "veranda", "terrace", "patio"], ("balcony", "Balcony", "Exterior")),
    (["garden", "yard", "private garden"], ("garden", "Garden", "Exterior")),
    (["roof", "roof deck", "roof-deck", "rooftop", "roof terrace"], ("roofDeck", "roofDeck", "Unknown")),
    (["security", "alarm", "cctv", "security door", "gated"], ("security", "security", "Unknown")),
    (["air conditioning", "a/c", "ac", "κλιματισμός"], ("ac", "Air Conditioning", "Legacy")),
    (["storage", "storeroom", "storage room"], ("storage", "Storage Space", "Interior")),
    (["pet", "pets allowed", "pet-friendly"], ("pets", "Pet Friendly", "Policy")),
    (["sea view", "seaview", "waterfront", "beachfront", "on the sea", "by the sea"], ("waterfront", "Waterfront", "Location")),
    (["bbq", "barbeque", "barbecue"], ("bbq", "BBQ", "Exterior")),
    (["elevator", "lift"], ("elevator", "Elevator", "Interior")),
    (["fireplace"], ("fireplace", "Fireplace", "Interior")),
    (["solar water", "solar heater"], ("solar", "Solar Water Heater", "Utilities")),
]


def map_amenities(features_texts: List[str]) -> Tuple[List[str], List[Dict[str, str]]]:
    """
    Given a list of feature strings, return (amenities_raw, amenities_detailed[]).
    """
    raw_ids_ordered: List[str] = []
    detailed_map: Dict[str, Dict[str, str]] = {}

    blob = " | ".join(ft.lower() for ft in features_texts)
    for keywords, (aid, label, category) in AMENITY_KEYWORDS:
        if any(k.lower() in blob for k in keywords):
            if aid not in raw_ids_ordered:
                raw_ids_ordered.append(aid)
            detailed_map[aid] = {"id": aid, "label": label, "category": category}

    amenities_detailed = [detailed_map[aid] for aid in raw_ids_ordered if aid in detailed_map]
    return raw_ids_ordered, amenities_detailed


# =========================
# ---- PARSER (site) ------
# =========================
def parse_gogordian(html: str, url: str) -> Dict:
    """
    Parse a Gogordian property page HTML into your exact payload.
    NOTE: Selectors are robust heuristics and can be tweaked if the site changes.
    """
    soup = BeautifulSoup(html, "html.parser")

    # ---- Title ----
    title = first_text(soup, ["h1", "h1.property-title", "div.property-title h1"])
    title = normalize_whitespace(title)

    # ---- Price ---- (e.g., "€ 505,000")
    price_text = first_text(soup, [".price", "[data-testid='price']", "h1 + div, h1 + *"])
    price = money_to_string_amount(price_text)

    # ---- Description ----
    # Prefer a 'Description' section, fallback to any long text block.
    description = None
    # 1) Look for heading that contains "Description"
    for h in soup.find_all(["h2", "h3"]):
        if "description" in h.get_text(" ", strip=True).lower():
            # The text may be in the next siblings
            blocks = []
            cursor = h.find_next_sibling()
            hops = 0
            while cursor and hops < 5:
                txt = cursor.get_text("\n", strip=True)
                if txt:
                    blocks.append(txt)
                cursor = cursor.find_next_sibling()
                hops += 1
            if blocks:
                description = "\n".join(blocks)
                break
    # 2) Fallback common containers
    if not description:
        description = first_text(soup, ["#Description", "div#description", ".description", "[itemprop='description']"])
    description = description or ""

    # ---- Location breakdown ----
    # Try to infer "City, Region, Country"
    location_text = first_text(soup, [".location", ".property-location", "[data-testid='location']", "header + * .location"])
    location_text = normalize_whitespace(location_text)
    country = DEFAULT_COUNTRY
    city = region = ""
    if location_text and "," in location_text:
        parts = [p.strip() for p in location_text.split(",")]
        if len(parts) >= 1:
            city = parts[0]
        if len(parts) >= 2:
            region = parts[1]
        if len(parts) >= 3:
            country = parts[-1]

    # ---- Areas / numbers ----
    # Look for labels like "Covered Area", "Total Covered Area", "Plot", etc.
    page_txt = soup.get_text("\n", strip=True)

    def find_labeled_number(labels: List[str]) -> Optional[float]:
        for label in labels:
            # handle both ":" and no colon, and support "m2", "m²", "sqm"
            m = re.search(rf"{re.escape(label)}\s*[:\-]?\s*([\d\.,]+)\s*(?:m2|m²|sqm|sq\.? m)?",
                          page_txt, re.IGNORECASE)
            if m:
                return to_float(m.group(1))
        return None

    area_num = find_labeled_number(["Covered Area", "Total Covered Area", "Internal Area", "Area"])
    lot_size_num = find_labeled_number(["Plot", "Land Area", "Plot Size", "Lot Size"])

    # ---- Bedrooms / Bathrooms / Floors ----
    def find_count(label_keywords: List[str]) -> Optional[int]:
        for kw in label_keywords:
            m = re.search(rf"\b{kw}\b\s*[:\-]?\s*(\d+)", page_txt, re.IGNORECASE)
            if m:
                return int(m.group(1))
        return None

    bedrooms = find_count(["Rooms", "Bedrooms"])
    bathrooms = find_count(["Baths", "Bathrooms", "WCs"])

    # sometimes there's a "1.5 bath" style; try to capture decimal
    if bathrooms is None:
        mhalf = re.search(r"\b(Baths?|Bathrooms?)\b\s*[:\-]?\s*(\d+(?:\.\d+)?)", page_txt, re.IGNORECASE)
        if mhalf:
            bathrooms = mhalf.group(2)

    # total floors / floor level (if present)
    total_floors = find_count(["Floors", "Total Floors", "Storeys", "Stories"])
    floor_level = find_count(["Floor", "Level"])

    # ---- Year built / Energy rating ----
    year_built = None
    myear = re.search(r"\b(?:Year\s*Built|Build\s*Year|Construction\s*Year)\b\s*[:\-]?\s*(\d{4})", page_txt, re.IGNORECASE)
    if myear:
        year_built = int(myear.group(1))
    energy_rating = None
    meng = re.search(r"\bEnergy\s*Class\b\s*[:\-]?\s*([A-D][+\-]?)", page_txt, re.IGNORECASE)
    if meng:
        energy_rating = meng.group(1).upper()

    # ---- Coordinates (if embedded) ----
    latitude = longitude = ""
    # Try to find latitude/longitude patterns in page HTML
    mlat = re.search(r'"latitude"\s*:\s*([0-9\.\-]+)', html)
    mlng = re.search(r'"longitude"\s*:\s*([0-9\.\-]+)', html)
    if mlat and mlng:
        latitude = mlat.group(1)
        longitude = mlng.group(1)

    # ---- Property type inference ----
    # Heuristics from title or features (e.g., "apartment", "house", "maisonette")
    tlow = (title or "").lower()
    if any(k in tlow for k in ["apartment", "flat", "studio"]):
        property_type = "apartment"
    elif any(k in tlow for k in ["maisonette", "semi-detached", "detached", "villa", "house"]):
        property_type = "house"
    else:
        property_type = "house"  # default; adjust if needed per site

    # ---- Features / amenities ----
    # Collect visible feature texts from likely sections
    feature_texts = []
    # Lists or blocks that often contain features
    feature_texts += all_texts(soup, [
        "section:has(h2:contains('Features')) li",
        "section:has(h2:contains('Main Features')) li",
        "section:has(h2:contains('Further Features')) li",
        ".amenities li",
        ".features li",
        "[data-amenity]",
    ])
    # If not found, fall back to any “Features” section text
    for h in soup.find_all(["h2", "h3"]):
        if "feature" in h.get_text(" ", strip=True).lower():
            txt = h.find_next().get_text(" | ", strip=True)
            if txt:
                feature_texts.append(txt)

    # Map features to your amenity schema
    amenities_raw, amenities_detailed = map_amenities(feature_texts)

    # ---- Ref/External ID if present ----
    external_id = None
    mref = re.search(r"\bRef\s*No\s*:\s*([A-Za-z0-9\-/]+)", page_txt, re.IGNORECASE)
    if mref:
        external_id = mref.group(1)

    # ---- Contact (rarely exposed; placeholders if not available) ----
    contact_phone = ""
    contact_email = ""
    mphone = re.search(r"(\+?\d[\d\s\-]{6,})", page_txt)
    if mphone:
        contact_phone = mphone.group(1).strip()

    memail = re.search(r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})", page_txt)
    if memail:
        contact_email = memail.group(1).strip()

    # ---- Postal/street (often absent) ----
    postal_code = ""
    street = ""

    # ---- Property status ----
    property_status = DEFAULT_PROPERTY_STATUS

    # ---- Available from (no reliable source on many listings) ----
    available_from = None  # string "YYYY-MM-DD" if you find it

    # ---- Assemble exact payload (match your example keys & types) ----
    payload = {
        "id": None,  # your backend will usually assign this; leave None or omit
        "title": title or "",
        "description": description or "",
        "price": price if price is not None else "0.00",          # string with 2 decimals
        "property_type": property_type,
        "location": normalize_whitespace(f"{city}, {region}, {country}") if city or region else (location_text or country or ""),
        "country": country or "",
        "region": region or "",
        "city": city or "",
        "postal_code": postal_code,
        "street": street,
        "latitude": latitude,
        "longitude": longitude,
        "bedrooms": bedrooms if isinstance(bedrooms, int) else (int(float(bedrooms)) if isinstance(bedrooms, str) and re.match(r"^\d+(\.\d+)?$", bedrooms) else 0),
        "bathrooms": str(bathrooms) if bathrooms is not None else "0",
        "area": int(area_num) if area_num is not None else 0,
        "year_built": year_built if year_built is not None else 0,
        "parking_spaces": 0,  # not reliably parsed; amend if you can detect it
        "lot_size": f"{lot_size_num:.2f}" if lot_size_num is not None else "0.00",
        "energy_rating": energy_rating or "",
        "construction_material": None,
        "floor_level": floor_level if floor_level is not None else 0,
        "total_floors": total_floors if total_floors is not None else 0,
        "available_from": available_from or "",  # "YYYY-MM-DD" or empty string
        "property_status": property_status,
        "contact_phone": contact_phone,
        "contact_email": contact_email,
        "virtual_tour_url": None,
        "video_url": None,
        "amenities_raw": amenities_raw,
        "amenities_detailed": amenities_detailed,
        "is_published": DEFAULT_IS_PUBLISHED,
        "is_featured": DEFAULT_IS_FEATURED,
        # Server should set created_at/updated_at; if you must send them, format ISO 8601:
        # "created_at": datetime.utcnow().isoformat() + "Z",
        # "updated_at": datetime.utcnow().isoformat() + "Z",
    }

    # Remove keys your server might reject if null/None
    if payload["id"] is None:
        del payload["id"]

    return payload


# =========================
# ---- NETWORKING ----------
# =========================
def send_to_backend(property_data):
    """
    Post as application/x-www-form-urlencoded (form data),
    converting arrays and booleans to strings your server will accept.
    """
    def as_str(v):
        if v is None:
            return ""
        if isinstance(v, bool):
            return "true" if v else "false"
        return str(v)

    # Flatten to form fields
    form = {
        "title": as_str(property_data.get("title")),
        "description": as_str(property_data.get("description")),
        "price": as_str(property_data.get("price")),                  # "280000.00"
        "property_type": as_str(property_data.get("property_type")),  # "house", "apartment", ...
        "location": as_str(property_data.get("location")),
        "country": as_str(property_data.get("country")),
        "region": as_str(property_data.get("region")),
        "city": as_str(property_data.get("city")),
        "postal_code": as_str(property_data.get("postal_code")),
        "street": as_str(property_data.get("street")),
        "latitude": as_str(property_data.get("latitude")),
        "longitude": as_str(property_data.get("longitude")),
        "bedrooms": as_str(property_data.get("bedrooms")),            # int → str
        "bathrooms": as_str(property_data.get("bathrooms")),          # keep string (e.g., "1.5")
        "area": as_str(property_data.get("area")),                    # int → str
        "year_built": as_str(property_data.get("year_built")),
        "parking_spaces": as_str(property_data.get("parking_spaces")),
        "lot_size": as_str(property_data.get("lot_size")),            # "309.00"
        "energy_rating": as_str(property_data.get("energy_rating")),
        "construction_material": as_str(property_data.get("construction_material")),
        "floor_level": as_str(property_data.get("floor_level")),
        "total_floors": as_str(property_data.get("total_floors")),
        "available_from": as_str(property_data.get("available_from")),  # "YYYY-MM-DD" or ""
        "property_status": as_str(property_data.get("property_status")),
        "contact_phone": as_str(property_data.get("contact_phone")),
        "contact_email": as_str(property_data.get("contact_email")),
        "virtual_tour_url": as_str(property_data.get("virtual_tour_url")),
        "video_url": as_str(property_data.get("video_url")),
        "is_published": as_str(property_data.get("is_published")),    # "true"/"false"
        "is_featured": as_str(property_data.get("is_featured")),      # "true"/"false"
    }

    # Arrays: server previously accepted newline-joined strings
    amenities_raw = property_data.get("amenities_raw") or []
    images = property_data.get("images") or []

    form["amenities_raw"] = "\n".join(amenities_raw)
    # amenities_detailed: send as JSON text (common pattern) OR newline-joined; pick what your backend expects.
    # Based on your sample, it’s an array — many form handlers accept JSON strings for array fields:
    form["amenities_detailed"] = json.dumps(property_data.get("amenities_detailed") or [], ensure_ascii=False)
    form["images"] = "\n".join(images)

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        # DO NOT set Content-Type manually; requests will set form-encoded for `data=...`
        "Accept": "application/json",
    }

    try:
        r = requests.post(BACKEND_URL, headers=headers, data=form, timeout=40)
        if r.status_code in (200, 201):
            print(f"[OK] Uploaded: {form.get('title')}")
        else:
            print(f"[FAIL] {form.get('title')} - {r.status_code}")
            print(r.text[:1000])
    except Exception as e:
        print(f"[ERROR] Sending {form.get('title')}: {e}")



# =========================
# ---- MAIN FLOW ----------
# =========================
def scrape_once(driver: webdriver.Chrome, url: str) -> Optional[Dict]:
    driver.get(url)
    wait = WebDriverWait(driver, 20)
    # Wait for a reliable element (title or a key container)
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "h1, h1.property-title, .property-title h1")))
    # Some galleries/descriptions lazy-load; small scroll helps
    driver.execute_script("window.scrollTo(0, Math.min(1200, document.body.scrollHeight));")
    time.sleep(0.5)
    html = driver.page_source
    payload = parse_gogordian(html, url)
    # Title is required (server expects); skip if missing
    if not payload.get("title"):
        print(f"[SKIP] No title for {url}")
        return None
    return payload


def main():
    # Basic validation
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

            # Log what we’re about to send
            print("[DEBUG] Payload preview:")
            print(json.dumps(payload, indent=2, ensure_ascii=False))

            if POST_ENABLED:
                status, body = send_to_backend(payload)
                if status in (200, 201):
                    ok += 1
                    print(f"[OK] {status} {payload.get('title')}")
                else:
                    fail += 1
                    print(f"[FAIL] {status} {payload.get('title')}")
                    print(body[:1000])
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
