"""Command-line entry point for awarding points to a completed order."""

import argparse

from loyalty_service import award_points


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("order_id", help="Completed order ID, e.g. ORD-2026-000001")
    parser.add_argument("--expiry-days", type=int, default=365,
                        help="Days until newly earned points expire (default: 365)")
    args = parser.parse_args()

    result = award_points(args.order_id, expiry_days=args.expiry_days)
    if result["points"] == 0:
        print("This order earned no points under the current earning rule.")
        return
    print(f"Awarded {result['points']} points.")
    print(f"Transaction: {result['transaction_id']}")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError) as error:
        print(f"Could not award points: {error}")
        raise SystemExit(1) from None
    except Exception as error:
        print(f"Database error: {error}")
        raise SystemExit(1) from None
