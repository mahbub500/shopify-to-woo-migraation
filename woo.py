import json
import re
from woocommerce import API

wordpress_url    = "https://xyz.com"
consumer__key    = ""woo commerce api consumer__key key
consumer__secret = ""woo commerce api consumer__secret key
json_file        = "products.json"

wcapi = API(
    url=wordpress_url,
    consumer_key=consumer__key,
    consumer_secret=consumer__secret,
    version="wc/v3",
    timeout=60
)

with open(json_file, "r", encoding="utf-8") as f:
    shopify_data = json.load(f)

SKIP_PRODUCT_TYPES = {"PV-Module", "Wechselrichter"}

# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────

def clean_url(url):
    return url.split("?")[0] if url else ""

def make_sku(variant, product_id, index):
    """Return a clean SKU — never None or empty."""
    raw = variant.get("sku")
    if raw and str(raw).strip().lower() not in ("", "none"):
        return str(raw).strip()
    return f"SHP-{product_id}-{variant.get('id', index)}"

def is_default_only(product_data):
    """True when the product has only a single 'Default Title' variant → simple product."""
    variants = product_data.get("variants", [])
    if len(variants) != 1:
        return False
    v = variants[0]
    return (
        v.get("option1", "").lower() == "default title"
        and v.get("option2") is None
        and v.get("option3") is None
    )

def sanitize_html(html):
    """Remove <script> tags and common Shopify tracking pixels from body_html."""
    if not html:
        return ""
    html = re.sub(r"<script[\s\S]*?</script>", "", html, flags=re.IGNORECASE)
    html = re.sub(r"<iframe[\s\S]*?</iframe>", "", html, flags=re.IGNORECASE)
    return html.strip()

def release_sku_conflict(sku, owner_id):
    """
    If this SKU already belongs to a DIFFERENT product/variation,
    clear it there so we can reuse it.
    """
    resp = wcapi.get("products", params={"sku": sku, "per_page": 5})
    if resp.status_code != 200:
        return
    for prod in resp.json():
        if prod["id"] == owner_id:
            continue
        if prod["type"] == "variable":
            vresp = wcapi.get(f"products/{prod['id']}/variations", params={"per_page": 100})
            if vresp.status_code == 200:
                for var in vresp.json():
                    if var.get("sku") == sku:
                        wcapi.put(f"products/{prod['id']}/variations/{var['id']}", {"sku": ""})
                        print(f"    ⚠ Cleared SKU '{sku}' from product {prod['id']} variation {var['id']}")
        else:
            wcapi.put(f"products/{prod['id']}", {"sku": ""})
            print(f"    ⚠ Cleared SKU '{sku}' from simple product {prod['id']}")

# ──────────────────────────────────────────────
# CATEGORY HELPER
# ──────────────────────────────────────────────

_category_cache = {}

def get_or_create_category(name):
    """Return WooCommerce category ID for the given name, creating it if needed."""
    if not name:
        return None

    name = name.strip()
    if name in _category_cache:
        return _category_cache[name]

    r = wcapi.get("products/categories", params={"search": name, "per_page": 10})
    if r.status_code == 200:
        for cat in r.json():
            if cat["name"].lower() == name.lower():
                _category_cache[name] = cat["id"]
                print(f"    📂 Found existing category: '{name}' (ID: {cat['id']})")
                return cat["id"]

    r = wcapi.post("products/categories", {"name": name})
    if r.status_code in [200, 201]:
        cat_id = r.json()["id"]
        _category_cache[name] = cat_id
        print(f"    📂 Created new category: '{name}' (ID: {cat_id})")
        return cat_id

    print(f"    ⚠ Failed to create category '{name}': {r.json()}")
    return None

# ──────────────────────────────────────────────
# ATTRIBUTE / VARIATION BUILDERS
# ──────────────────────────────────────────────

def build_attributes(product_data):
    attrs = []
    for opt in product_data.get("options", []):
        values = [v for v in opt.get("values", []) if v.lower() != "default title"]
        if values:
            attrs.append({
                "name":      opt["name"],
                "visible":   True,
                "variation": True,
                "options":   values
            })
    return attrs

def build_variations(product_data, parent_weight_kg, parent_image_url, parent_id):
    """
    Build variation payloads. SKU conflicts are resolved AFTER the parent
    product is saved (parent_id is now valid).
    """
    variations = []
    for i, variant in enumerate(product_data["variants"]):
        if variant.get("option1", "").lower() == "default title":
            continue

        sku = make_sku(variant, product_data["id"], i)
        # Resolve conflict now that we have a valid parent_id
        release_sku_conflict(sku, parent_id)

        var_weight = variant.get("grams", 0)
        weight_kg  = var_weight / 1000 if var_weight else parent_weight_kg

        variation = {
            "regular_price": str(variant["price"]),
            "sku":           sku,
            "manage_stock":  False,
            "stock_status":  "instock" if variant.get("available", True) else "outofstock",
            "weight":        str(weight_kg),
            "attributes":    []
        }

        options_list = product_data.get("options", [])
        for j, key in enumerate(["option1", "option2", "option3"]):
            val = variant.get(key)
            if val and j < len(options_list):
                variation["attributes"].append({
                    "name":   options_list[j]["name"],
                    "option": val
                })

        if variant.get("featured_image"):
            variation["image"] = {"src": clean_url(variant["featured_image"]["src"])}
        elif parent_image_url:
            variation["image"] = {"src": clean_url(parent_image_url)}

        variations.append(variation)
    return variations

def delete_existing_variations(pid):
    """Delete all existing variations on a product before re-adding them (prevents duplicates on update)."""
    vresp = wcapi.get(f"products/{pid}/variations", params={"per_page": 100})
    if vresp.status_code != 200 or not vresp.json():
        return
    ids = [v["id"] for v in vresp.json()]
    if not ids:
        return
    # Batch delete
    batch_payload = {"delete": ids}
    dr = wcapi.post(f"products/{pid}/variations/batch", batch_payload)
    if dr.status_code in [200, 201]:
        print(f"    🗑 Deleted {len(ids)} old variation(s) before re-import")
    else:
        print(f"    ⚠ Could not delete old variations: {dr.json().get('message', dr.status_code)}")

def push_variations_batch(pid, variations):
    """Use WooCommerce batch endpoint to create all variations in one request."""
    if not variations:
        print("    ℹ No real variations to add (all were Default Title)")
        return

    batch_payload = {"create": variations}
    br = wcapi.post(f"products/{pid}/variations/batch", batch_payload)

    if br.status_code not in [200, 201]:
        print(f"    ❌ Batch variation push failed: {br.json().get('message', br.status_code)}")
        return

    results = br.json().get("create", [])
    for res in results:
        if res.get("error"):
            print(f"    ❌ Variation error — {res['error'].get('message', res['error'])}")
        else:
            print(f"    ✅ Variation added — SKU: {res.get('sku', '?')} (ID: {res.get('id')})")

# ──────────────────────────────────────────────
# FIND EXISTING PRODUCT
# ──────────────────────────────────────────────

def find_existing(slug, sku=None):
    """Return existing WooCommerce product dict or None."""
    # For simple products, try SKU first (more reliable)
    if sku:
        r = wcapi.get("products", params={"sku": sku, "per_page": 5})
        if r.status_code == 200 and r.json():
            return r.json()[0]
    # Fall back to slug for both simple and variable
    r = wcapi.get("products", params={"slug": slug, "per_page": 5})
    if r.status_code == 200 and r.json():
        return r.json()[0]
    return None

# ──────────────────────────────────────────────
# MAIN IMPORT FUNCTION
# ──────────────────────────────────────────────

def import_product(data):
    title    = data["title"]
    variants = data.get("variants", [])
    images   = data.get("images", [])

    if not variants:
        print(f"  ⚠ Skipped '{title}' — no variants found")
        return

    first_variant = variants[0]
    simple        = is_default_only(data)
    parent_weight = first_variant.get("grams", 0) / 1000
    parent_image  = images[0]["src"] if images else None

    slug = data.get("handle") or title.lower().replace(" ", "-")
    sku  = make_sku(first_variant, data["id"], 0) if simple else ""

    tags       = [{"name": t} for t in data.get("tags", [])]
    categories = []
    product_type = data.get("product_type", "").strip()
    if product_type:
        cat_id = get_or_create_category(product_type)
        if cat_id:
            categories.append({"id": cat_id})

    product_payload = {
        "name":              title,
        "type":              "simple" if simple else "variable",
        "status":            "publish",
        "description":       sanitize_html(data.get("body_html", "")),
        "short_description": "",
        "slug":              slug,
        "images":            [{"src": clean_url(img["src"])} for img in images],
        "weight":            str(parent_weight),
        "tags":              tags,
        "categories":        categories,
        "meta_data":         [{"key": "_vendor", "value": data.get("vendor", "")}],
    }

    if simple:
        product_payload["sku"]           = sku
        product_payload["regular_price"] = str(first_variant["price"])
        product_payload["manage_stock"]  = False
        product_payload["stock_status"]  = "instock" if first_variant.get("available", True) else "outofstock"
        # Resolve SKU conflicts for simple products before saving
        existing_check = find_existing(slug, sku)
        if not existing_check:
            release_sku_conflict(sku, -1)  # -1 = product doesn't exist yet
    else:
        product_payload["attributes"] = build_attributes(data)

    # ── Create or Update ──
    existing = find_existing(slug, sku if simple else None)

    if existing:
        pid    = existing["id"]
        resp   = wcapi.put(f"products/{pid}", product_payload)
        action = "Updated"
        is_update = True
    else:
        resp   = wcapi.post("products", product_payload)
        action = "Created"
        is_update = False

    if resp.status_code not in [200, 201]:
        print(f"  ❌ Failed to {action.lower()} '{title}': {resp.json()}")
        return

    pid = resp.json()["id"]
    print(f"  ✅ {action}: '{title}' (ID: {pid})")

    # ── Handle variations for variable products ──
    if not simple:
        # On update: clear old variations first to prevent duplicates
        if is_update:
            delete_existing_variations(pid)

        variations = build_variations(data, parent_weight, parent_image, pid)
        push_variations_batch(pid, variations)

# ──────────────────────────────────────────────
# RUN
# ──────────────────────────────────────────────

print("=" * 60)
print("Starting Shopify → WooCommerce import (price > 2000)")
print("=" * 60)

skipped  = 0
imported = 0

for product_data in shopify_data["products"]:
    title        = product_data.get("title", "Unknown")
    product_type = product_data.get("product_type", "")

    # ── Skip by product type ──
    if product_type in SKIP_PRODUCT_TYPES:
        print(f"  ⏭ Skipped '{title}' — product type: '{product_type}'")
        skipped += 1
        continue

    # ── Skip by price ──
    try:
        price = float(product_data["variants"][0]["price"])
    except (KeyError, ValueError, IndexError):
        print(f"  ⚠ Skipped '{title}' — could not read price")
        skipped += 1
        continue

    if price > 2000:
        print(f"\n→ Processing: {title}")
        import_product(product_data)
        imported += 1
    else:
        skipped += 1

print("\n" + "=" * 60)
print(f"Done. Imported: {imported} | Skipped: {skipped}")
print("=" * 60)
