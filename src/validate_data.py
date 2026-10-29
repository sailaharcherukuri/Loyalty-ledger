"""Run practical ledger and lifecycle reconciliation checks."""

import json

from db import get_connection


CHECKS = {
    "balance_ledger_mismatches": """
        SELECT a.customer_id, a.current_points,
               COALESCE(SUM(t.points_change), 0) AS ledger_points
        FROM loyalty_accounts AS a
        LEFT JOIN points_transactions AS t ON t.customer_id = a.customer_id
        GROUP BY a.customer_id, a.current_points
        HAVING a.current_points <> COALESCE(SUM(t.points_change), 0)
    """,
    "available_lot_balance_mismatches": """
        SELECT a.customer_id, a.current_points,
               COALESCE(SUM(l.points_remaining), 0) AS lot_points
        FROM loyalty_accounts AS a
        LEFT JOIN point_lots AS l ON l.customer_id = a.customer_id
        GROUP BY a.customer_id, a.current_points
        HAVING GREATEST(a.current_points, 0) <> COALESCE(SUM(l.points_remaining), 0)
    """,
    "accruals_missing_lots": """
        SELECT t.transaction_id, t.customer_id, t.order_id
        FROM points_transactions AS t
        LEFT JOIN point_lots AS l ON l.source_transaction_id = t.transaction_id
        WHERE t.transaction_type = 'ACCRUAL' AND t.points_change > 0
          AND l.lot_id IS NULL
    """,
    "completed_refunds_over_order_total": """
        SELECT o.order_id, o.total_amount, SUM(r.refund_amount) AS refunds
        FROM orders AS o
        JOIN order_returns AS r ON r.order_id = o.order_id
        WHERE r.return_status = 'COMPLETED'
        GROUP BY o.order_id, o.total_amount
        HAVING SUM(r.refund_amount) > o.total_amount
    """,
    "duplicate_order_accruals": """
        SELECT order_id, COUNT(*) AS accrual_count
        FROM points_transactions
        WHERE transaction_type = 'ACCRUAL' AND order_id IS NOT NULL
        GROUP BY order_id
        HAVING COUNT(*) > 1
    """,
    "expired_lots_still_open": """
        SELECT lot_id, customer_id, points_remaining, expires_at
        FROM point_lots
        WHERE points_remaining > 0 AND DATE(expires_at) <= COALESCE(%s, CURRENT_DATE())
    """,
    "invalid_lot_balances": """
        SELECT lot_id, points_earned, points_remaining
        FROM point_lots
        WHERE points_earned <= 0 OR points_remaining < 0
           OR points_remaining > points_earned
    """,
}


def validate_data(sample_limit=20, as_of=None):
    """Return reconciliation results, optionally evaluated at a business date."""
    connection = get_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        report = {"checks": {}, "passed": True}
        for name, query in CHECKS.items():
            if name == "expired_lots_still_open":
                cursor.execute(query, (as_of,))
            else:
                cursor.execute(query)
            rows = cursor.fetchmany(sample_limit)
            count = len(rows)
            # Fetch the remainder to report an accurate count without holding
            # every issue in memory for large generated datasets.
            while True:
                batch = cursor.fetchmany(1000)
                if not batch:
                    break
                count += len(batch)
            report["checks"][name] = {
                "issue_count": count,
                "sample": rows,
            }
            report["passed"] = report["passed"] and count == 0
        return report
    finally:
        cursor.close()
        connection.close()


if __name__ == "__main__":
    try:
        print(json.dumps(validate_data(), indent=2, default=str))
    except Exception as error:
        print(f"Validation could not run: {error}")
