import json
from woocommerce import API

wordpress_url    = "https://work011.sunnahsoul.com"
consumer__key    = "ck_de0e6a7dabbce34ec5fc9177d8fa8b1d185660ae"
consumer__secret = "cs_4c9d9992629ab022802fca79dae97b30dbfa309c"
json_file        = 'products.json'

wcapi = API(
    url=wordpress_url,
    consumer_key=consumer__key,
    consumer_secret=consumer__secret,
    version="wc/v3",
    timeout=60
)

with open(json_file, 'r', encoding='utf-8') as f:
    shopify_data = json.load(f)

# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────

def clean_url(url):
    return url.split('?')[0] if url else ""

def make_sku(variant, product_id, index):
    """Return a clean SKU — never None or empty."""
    raw = variant.get("sku")
    if raw and str(raw).strip().lower() not in ("", "none"):
        return str(raw).strip()
    # fallback: shopify product_id + variant_id
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
# ATTRIBUTE / VARIATION BUILDERS
# ──────────────────────────────────────────────

def build_attributes(product_data):
    attrs = []
    for opt in product_data.get("options", []):
        values = [v for v in opt.get("values", []) if v.lower() != "default title"]
        if values:
            attrs.append({
                "name": opt["name"],
                "visible": True,
                "variation": True,
                "options": values
            })
    return attrs

def build_variations(product_data, parent_weight_kg, parent_image_url, parent_id):
    variations = []
    for i, variant in enumerate(product_data["variants"]):
        # skip the dummy "Default Title" variant for variable products
        if variant.get("option1", "").lower() == "default title":
            continue

        sku = make_sku(variant, product_data["id"], i)
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

# ──────────────────────────────────────────────
# FIND EXISTING PRODUCT
# ──────────────────────────────────────────────

def find_existing(slug, sku=None):
    """Return existing WooCommerce product dict or None."""
    if sku:
        r = wcapi.get("products", params={"sku": sku, "per_page": 5})
        if r.status_code == 200 and r.json():
            return r.json()[0]
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

    # ── Build tags ──
    tags = [{"name": t} for t in data.get("tags", [])]

    # ── Build product payload ──
    product_payload = {
        "name":              title,
        "type":              "simple" if simple else "variable",
        "status":            "publish",
        "description":       data.get("body_html", ""),
        "short_description": "",
        "slug":              slug,
        "images":            [{"src": clean_url(img["src"])} for img in images],
        "weight":            str(parent_weight),
        "tags":              tags,
        "meta_data":         [{"key": "_vendor", "value": data.get("vendor", "")}],
    }

    if simple:
        product_payload["sku"]            = sku
        product_payload["regular_price"]  = str(first_variant["price"])
        product_payload["manage_stock"]   = False
        product_payload["stock_status"]   = "instock" if first_variant.get("available", True) else "outofstock"
    else:
        product_payload["attributes"] = build_attributes(data)

    # ── Create or Update ──
    existing = find_existing(slug, sku if simple else None)

    if existing:
        pid    = existing["id"]
        resp   = wcapi.put(f"products/{pid}", product_payload)
        action = "Updated"
    else:
        resp   = wcapi.post("products", product_payload)
        action = "Created"

    if resp.status_code not in [200, 201]:
        print(f"  ❌ Failed to {action.lower()} '{title}': {resp.json()}")
        return

    pid = resp.json()["id"]
    print(f"  ✅ {action}: '{title}' (ID: {pid})")

    # ── Add variations for variable products ──
    if not simple:
        variations = build_variations(data, parent_weight, parent_image, pid)
        if not variations:
            print(f"    ℹ No real variations to add (all were Default Title)")
            return
        for var in variations:
            vr = wcapi.post(f"products/{pid}/variations", var)
            if vr.status_code in [200, 201]:
                print(f"    ✅ Variation added — SKU: {var['sku']}")
            else:
                print(f"    ❌ Variation failed — SKU: {var['sku']} | {vr.json().get('message', vr.json())}")

# ──────────────────────────────────────────────
# RUN
# ──────────────────────────────────────────────

print("=" * 60)
print("Starting Shopify → WooCommerce import (price > 2000)")
print("=" * 60)

skipped = 0
imported = 0

for product_data in shopify_data["products"]:
    try:
        price = float(product_data["variants"][0]["price"])
    except (KeyError, ValueError, IndexError):
        skipped += 1
        continue

    if price > 2000:
        print(f"\n→ Processing: {product_data['title']}")
        import_product(product_data)
        imported += 1
    else:
        skipped += 1

print("\n" + "=" * 60)
print(f"Done. Imported: {imported} | Skipped (price ≤ 2000 or invalid): {skipped}")
print("=" * 60)
