import argparse

from integration import create_odoo_products, load_products_from_json


def main() -> None:
    parser = argparse.ArgumentParser(description="Import products from JSON into Odoo")
    parser.add_argument("--json", default="products.json", help="Path to products JSON")
    args = parser.parse_args()

    products = load_products_from_json(args.json)
    result = create_odoo_products(products)
    print(result)


if __name__ == "__main__":
    main()