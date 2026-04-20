import requests
import json
from woocommerce import API

wordpress_url = "https://work011.sunnahsoul.com"
consumer__key = "ck_de0e6a7dabbce34ec5fc9177d8fa8b1d185660ae"
consumer__secret = "cs_4c9d9992629ab022802fca79dae97b30dbfa309c"
json_file = 'products.json'

wcapi = API(
    url=wordpress_url,
    consumer_key=consumer__key,
    consumer_secret=consumer__secret,
    version="wc/v3",
    timeout=30
)

with open(json_file, 'r') as file:
    shopify_data = json.load(file)

def clean_image_url(url):
    return url.split('?')[0]

def create_attributes(product_data):
    attributes = []
    for option in product_data.get("options", []):
        if option.get("values"):
            attributes.append({
                "name": option["name"],
                "visible": True,
                "variation": True,
                "options": option["values"]
            })
    return attributes

# ✅ FIX 1: Generate a fallback SKU if variant SKU is missing/None
def get_variant_sku(variant, product_id, index):
    sku = variant.get("sku")
    if not sku or str(sku).strip() == "" or str(sku).lower() == "none":
        # Generate unique SKU from product id + variant id or index
        return f"{product_id}-var-{variant.get('id', index)}"
    return str(sku).strip()

# ✅ FIX 2: If an SKU exists on a DIFFERENT product/variation, clear it there first
def release_sku_if_duplicate(sku, current_product_id):
    response = wcapi.get("products", params={"sku": sku, "per_page": 5})
    if response.status_code != 200:
        return
    existing = response.json()
    for prod in existing:
        if prod["id"] == current_product_id:
            continue  # Same product, no conflict
        # It's on a different product — clear the SKU from that product's variations
        if prod["type"] == "variable":
            var_resp = wcapi.get(f"products/{prod['id']}/variations", params={"per_page": 100})
            if var_resp.status_code == 200:
                for var in var_resp.json():
                    if var.get("sku") == sku:
                        wcapi.put(f"products/{prod['id']}/variations/{var['id']}", {"sku": ""})
                        print(f"  ⚠ Cleared duplicate SKU '{sku}' from product {prod['id']} variation {var['id']}")
        elif prod["type"] == "simple":
            wcapi.put(f"products/{prod['id']}", {"sku": ""})
            print(f"  ⚠ Cleared duplicate SKU '{sku}' from simple product {prod['id']}")

def create_variations(product_data, parent_weight, parent_image, product_id):
    variations = []
    for i, variant in enumerate(product_data["variants"]):
        sku = get_variant_sku(variant, product_data["id"], i)

        # ✅ FIX 3: Release SKU from any conflicting product before using it
        release_sku_if_duplicate(sku, product_id)

        variation = {
            "regular_price": str(variant["price"]),
            "sku": sku,
            "manage_stock": False,
            "stock_status": "instock" if variant.get("available", True) else "outofstock",
            "weight": str(variant.get("grams", parent_weight * 1000) / 1000),
            "attributes": []
        }

        for j, option_key in enumerate(["option1", "option2", "option3"], start=1):
            if variant.get(option_key) and j <= len(product_data.get("options", [])):
                variation["attributes"].append({
                    "name": product_data["options"][j - 1]["name"],
                    "option": variant[option_key]
                })

        if variant.get("featured_image"):
            variation["image"] = {"src": clean_image_url(variant["featured_image"]["src"])}
        elif parent_image:
            variation["image"] = {"src": clean_image_url(parent_image)}

        if i == 0:
            variation["menu_order"] = 0  # first = default in WooCommerce

        variations.append(variation)
    return variations

def create_or_update_product(product_data):
    is_variable = len(product_data["variants"]) > 1
    parent_weight = product_data["variants"][0].get("grams", 0) / 1000
    parent_image = product_data["images"][0]["src"] if product_data.get("images") else None

    product = {
        "name": product_data["title"],
        "type": "variable" if is_variable else "simple",
        "regular_price": product_data["variants"][0]["price"] if not is_variable else "",
        "description": product_data.get("body_html", ""),
        "short_description": "",
        "slug": product_data["handle"] if product_data.get("handle") else product_data["title"].lower().replace(" ", "-"),
        "images": [{"src": clean_image_url(img["src"])} for img in product_data.get("images", [])],
        "meta_data": [{"key": "_vendor", "value": product_data.get("vendor", "")}],
        "weight": str(parent_weight)
    }

    # Only set SKU for simple products
    if not is_variable:
        sku = get_variant_sku(product_data["variants"][0], product_data["id"], 0)
        product["sku"] = sku

    if is_variable:
        product["attributes"] = create_attributes(product_data)

    # Check if product exists
    if not is_variable and product.get("sku"):
        response = wcapi.get("products", params={"sku": product["sku"]})
    else:
        response = wcapi.get("products", params={"slug": product["slug"]})

    existing_products = response.json() if response.status_code == 200 else []

    if existing_products:
        product_id = existing_products[0]["id"]
        response = wcapi.put(f"products/{product_id}", product)
        action = "updated"
    else:
        response = wcapi.post("products", product)
        action = "created"

    if response.status_code in [200, 201]:
        product_id = response.json().get("id")
        print(f"✅ Product '{product_data['title']}' {action} (ID: {product_id})")

        if is_variable:
            variations = create_variations(product_data, parent_weight, parent_image, product_id)
            for variation in variations:
                var_response = wcapi.post(f"products/{product_id}/variations", variation)
                if var_response.status_code in [200, 201]:
                    print(f"  ✅ Variation added. SKU: {variation['sku']}")
                else:
                    err = var_response.json()
                    print(f"  ❌ Variation failed. SKU: {variation['sku']} | Error: {err.get('message', err)}")
    else:
        print(f"❌ Failed to {action} '{product_data['title']}': {response.json()}")

# Import products with price > 2000
for product_data in shopify_data["products"]:
    try:
        price = float(product_data["variants"][0]["price"])
    except (KeyError, ValueError, IndexError):
        continue

    if price > 2000:
        create_or_update_product(product_data)
