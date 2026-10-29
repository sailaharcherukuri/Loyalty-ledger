"""Complete a previously requested full or partial order return."""

import argparse

from loyalty_service import complete_return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("return_id", help="Return ID in REQUESTED state")
    args = parser.parse_args()
    result = complete_return(args.return_id)
    print(f"Return completed: {result['return_id']}")
    print(f"Points reversed: {result['points_reversed']}")
    if result["transaction_id"]:
        print(f"Ledger transaction: {result['transaction_id']}")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError) as error:
        print(f"Could not complete return: {error}")
        raise SystemExit(1) from None
    except Exception as error:
        print(f"Database error: {error}")
        raise SystemExit(1) from None
