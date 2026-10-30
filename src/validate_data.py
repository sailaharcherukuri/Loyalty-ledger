"""Run practical ledger and lifecycle reconciliation checks."""

import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR

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
        WHERE points_remaining > 0 AND expires_at <= COALESCE(%s, UTC_TIMESTAMP())
    """,
    "invalid_lot_balances": """
        SELECT lot_id, points_earned, points_remaining
        FROM point_lots
        WHERE points_earned <= 0 OR points_remaining < 0
           OR points_remaining > points_earned
    """,
    "accrual_rule_metadata_mismatches": """
        SELECT t.transaction_id, t.order_id, t.rule_id, t.earning_rate,
               t.eligible_spend, t.points_change
        FROM points_transactions AS t
        LEFT JOIN rules AS r ON r.rule_id = t.rule_id
        WHERE t.transaction_type = 'ACCRUAL'
          AND NOT (t.rule_id IS NULL AND t.earning_rate IS NULL
                   AND t.eligible_spend IS NULL)
          AND (t.rule_id IS NULL OR t.earning_rate IS NULL
               OR t.eligible_spend IS NULL OR r.rule_id IS NULL
               OR t.earning_rate <> r.points_per_100
               OR DATE(t.transaction_date) < r.effective_from
               OR (r.effective_to IS NOT NULL
                   AND DATE(t.transaction_date) > r.effective_to)
               OR t.points_change <> FLOOR(t.eligible_spend * t.earning_rate / 100))
    """,
    "completed_returns_missing_or_wrong_reversals": None,
    "current_tier_and_spend_cache_mismatches": """
        WITH order_facts AS (
            SELECT o.customer_id, o.order_id, o.total_amount,
                   CASE WHEN EXISTS (SELECT 1 FROM order_lines AS any_line
                                     WHERE any_line.order_id = o.order_id)
                        THEN COALESCE((SELECT SUM(l.quantity * l.unit_price - l.discount_amount)
                                       FROM order_lines AS l
                                       WHERE l.order_id = o.order_id AND l.is_eligible = TRUE), 0)
                        ELSE o.total_amount END AS eligible_spend,
                   COALESCE((SELECT SUM(r.refund_amount) FROM order_returns AS r
                             WHERE r.order_id = o.order_id
                               AND r.return_status = 'COMPLETED'), 0) AS refunds
            FROM orders AS o
            WHERE o.order_status IN ('COMPLETED', 'RETURNED')
              AND (EXISTS (SELECT 1 FROM points_transactions AS t
                           WHERE t.order_id = o.order_id AND t.transaction_type = 'ACCRUAL')
                   OR EXISTS (SELECT 1 FROM audit_events AS a
                              WHERE a.entity_type = 'order' AND a.entity_id = o.order_id
                                AND a.event_type = 'QUALIFYING_SPEND_RECORDED'))
        ), customer_spend AS (
            SELECT customer_id,
                   SUM(GREATEST(0, eligible_spend - CASE WHEN total_amount > 0
                       THEN refunds * eligible_spend / total_amount ELSE 0 END)) AS spend
            FROM order_facts GROUP BY customer_id
        )
        SELECT a.customer_id, a.qualifying_spend AS recorded_spend,
               COALESCE(s.spend, 0) AS expected_spend, a.tier_id AS recorded_tier,
               (SELECT tier_id FROM tiers WHERE minimum_spend <= COALESCE(s.spend, 0)
                ORDER BY minimum_spend DESC, tier_id LIMIT 1) AS expected_tier
        FROM loyalty_accounts AS a
        LEFT JOIN customer_spend AS s ON s.customer_id = a.customer_id
        WHERE ABS(a.qualifying_spend - COALESCE(s.spend, 0)) > 0.01
           OR NOT (a.tier_id <=> (SELECT tier_id FROM tiers
                                  WHERE minimum_spend <= COALESCE(s.spend, 0)
                                  ORDER BY minimum_spend DESC, tier_id LIMIT 1))
    """,
    "tier_history_event_time_mismatches": """
        WITH order_facts AS (
            SELECT o.customer_id, o.order_id, o.order_date, o.total_amount,
                   CASE WHEN EXISTS (SELECT 1 FROM order_lines AS any_line
                                     WHERE any_line.order_id = o.order_id)
                        THEN COALESCE((SELECT SUM(l.quantity * l.unit_price - l.discount_amount)
                                       FROM order_lines AS l
                                       WHERE l.order_id = o.order_id AND l.is_eligible = TRUE), 0)
                        ELSE o.total_amount END AS eligible_spend
            FROM orders AS o
            WHERE o.order_status IN ('COMPLETED', 'RETURNED')
              AND (EXISTS (SELECT 1 FROM points_transactions AS t
                           WHERE t.order_id = o.order_id AND t.transaction_type = 'ACCRUAL')
                   OR EXISTS (SELECT 1 FROM audit_events AS a
                              WHERE a.entity_type = 'order' AND a.entity_id = o.order_id
                                AND a.event_type = 'QUALIFYING_SPEND_RECORDED'))
        ), events AS (
            SELECT customer_id, order_date AS event_at, eligible_spend AS spend_change
            FROM order_facts
            UNION ALL
            SELECT o.customer_id, r.return_date AS event_at,
                   -(r.refund_amount * f.eligible_spend / NULLIF(f.total_amount, 0)) AS spend_change
            FROM order_returns AS r
            JOIN order_facts AS f ON f.order_id = r.order_id
            JOIN orders AS o ON o.order_id = r.order_id
            WHERE r.return_status = 'COMPLETED'
        ), history_spend AS (
            SELECT h.history_id, h.customer_id, h.old_tier_id, h.new_tier_id,
                   h.qualifying_spend, h.changed_at,
                   GREATEST(0, COALESCE(SUM(e.spend_change), 0)) AS expected_spend
            FROM account_tier_history AS h
            LEFT JOIN events AS e ON e.customer_id = h.customer_id
                                 AND e.event_at <= h.changed_at
            GROUP BY h.history_id, h.customer_id, h.old_tier_id, h.new_tier_id,
                     h.qualifying_spend, h.changed_at
        )
        SELECT h.history_id, h.customer_id, h.qualifying_spend, h.expected_spend,
               h.new_tier_id AS recorded_tier,
               (SELECT tier_id FROM tiers WHERE minimum_spend <= h.expected_spend
                ORDER BY minimum_spend DESC, tier_id LIMIT 1) AS expected_tier
        FROM history_spend AS h
        WHERE ABS(h.qualifying_spend - h.expected_spend) > 0.01
           OR h.new_tier_id <> (SELECT tier_id FROM tiers
                                WHERE minimum_spend <= h.expected_spend
                                ORDER BY minimum_spend DESC, tier_id LIMIT 1)
    """,
    "lot_source_ownership_mismatches": """
        SELECT l.lot_id, l.customer_id AS lot_customer,
               t.customer_id AS source_customer, t.transaction_type,
               t.points_change, t.order_id
        FROM point_lots AS l
        LEFT JOIN points_transactions AS t
          ON t.transaction_id = l.source_transaction_id
        WHERE t.transaction_id IS NULL
           OR t.transaction_type NOT IN ('ACCRUAL', 'ADJUSTMENT')
           OR t.customer_id <> l.customer_id OR t.points_change <> l.points_earned
           OR (t.transaction_type = 'ADJUSTMENT' AND t.order_id IS NOT NULL)
    """,
    "redemption_lot_event_and_ownership_mismatches": """
        SELECT c.redemption_id, c.lot_id, r.customer_id AS redemption_customer,
               l.customer_id AS lot_customer, r.redemption_date, l.earned_at, l.expires_at
        FROM lot_consumptions AS c
        JOIN redemptions AS r ON r.redemption_id = c.redemption_id
        JOIN point_lots AS l ON l.lot_id = c.lot_id
        WHERE r.customer_id <> l.customer_id
           OR r.redemption_date < l.earned_at
           OR (l.expires_at IS NOT NULL AND r.redemption_date >= l.expires_at)
    """,
    "redemption_allocation_and_transaction_mismatches": """
        SELECT r.redemption_id, r.customer_id, r.points_redeemed,
               COALESCE(SUM(c.points_used), 0) AS allocated_points,
               t.transaction_type, t.points_change, t.customer_id AS transaction_customer
        FROM redemptions AS r
        LEFT JOIN lot_consumptions AS c ON c.redemption_id = r.redemption_id
        LEFT JOIN points_transactions AS t ON t.transaction_id = r.transaction_id
        WHERE r.redemption_status = 'COMPLETED'
        GROUP BY r.redemption_id, r.customer_id, r.points_redeemed,
                 t.transaction_type, t.points_change, t.customer_id
        HAVING allocated_points <> r.points_redeemed
            OR t.transaction_type <> 'REDEMPTION'
            OR t.points_change <> -r.points_redeemed
            OR t.customer_id <> r.customer_id
    """,
    "expiry_reference_and_duplicate_mismatches": """
        SELECT d.lot_id, d.transaction_id, l.customer_id AS lot_customer,
               t.customer_id AS transaction_customer, t.transaction_type,
               t.points_change, d.points_expired, l.expires_at, t.transaction_date
        FROM expiry_batch_details AS d
        JOIN point_lots AS l ON l.lot_id = d.lot_id
        LEFT JOIN points_transactions AS t ON t.transaction_id = d.transaction_id
        WHERE t.transaction_id IS NULL OR t.transaction_type <> 'EXPIRY'
           OR t.customer_id <> l.customer_id OR t.related_transaction_id <> l.source_transaction_id
           OR t.points_change <> -d.points_expired
           OR t.transaction_date < l.expires_at
           OR (SELECT COUNT(*) FROM expiry_batch_details AS other
               WHERE other.lot_id = d.lot_id) > 1
    """,
    "reversal_source_and_lot_allocation_mismatches": """
        SELECT t.transaction_id, t.customer_id, t.order_id,
               t.related_transaction_id, source.customer_id AS source_customer,
               source.order_id AS source_order, a.lot_id, l.customer_id AS lot_customer
        FROM points_transactions AS t
        LEFT JOIN points_transactions AS source
          ON source.transaction_id = t.related_transaction_id
        LEFT JOIN reversal_lot_allocations AS a
          ON a.reversal_transaction_id = t.transaction_id
        LEFT JOIN point_lots AS l ON l.lot_id = a.lot_id
        WHERE t.transaction_type = 'REVERSAL'
          AND (source.transaction_id IS NULL OR source.transaction_type <> 'ACCRUAL'
               OR source.customer_id <> t.customer_id OR source.order_id <> t.order_id
               OR t.points_change >= 0
               OR (a.lot_id IS NOT NULL AND l.customer_id <> t.customer_id)
               OR (SELECT COALESCE(SUM(x.points_removed), 0)
                   FROM reversal_lot_allocations AS x
                   WHERE x.reversal_transaction_id = t.transaction_id)
                  > -t.points_change)
    """,
}


def validate_data(sample_limit=20, as_of=None):
    """Return reconciliation results, optionally evaluated at a business date."""
    connection = get_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            """SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS
               WHERE TABLE_SCHEMA = DATABASE()
                 AND ((TABLE_NAME = 'rules' AND COLUMN_NAME = 'tier_id')
                      OR (TABLE_NAME = 'points_transactions'
                          AND COLUMN_NAME IN ('rule_id', 'earning_rate', 'eligible_spend')))"""
        )
        available = {(row["TABLE_NAME"], row["COLUMN_NAME"])
                     for row in cursor.fetchall()}
        required = {
            ("rules", "tier_id"),
            ("points_transactions", "rule_id"),
            ("points_transactions", "earning_rate"),
            ("points_transactions", "eligible_spend"),
        }
        if not required.issubset(available):
            raise RuntimeError(
                "The database uses the pre-v3 schema. Back it up, apply "
                "sql/migrate_v3.sql, then rerun validation."
            )
        cursor.execute(
            """SELECT COUNT(*) AS table_count FROM information_schema.TABLES
               WHERE TABLE_SCHEMA = DATABASE()
                 AND TABLE_NAME = 'reversal_lot_allocations'"""
        )
        if cursor.fetchone()["table_count"] != 1:
            raise RuntimeError(
                "The database uses the pre-v3 schema. Back it up, apply "
                "sql/migrate_v3.sql, then rerun validation."
            )
        report = {"checks": {}, "passed": True}
        if isinstance(as_of, datetime):
            expiry_cutoff = (as_of.astimezone(timezone.utc).replace(tzinfo=None)
                             if as_of.tzinfo else as_of)
            expiry_operator = "<="
        elif isinstance(as_of, date):
            expiry_cutoff = datetime.combine(as_of + timedelta(days=1), time.min)
            expiry_operator = "<"
        elif as_of is None:
            expiry_cutoff = None
            expiry_operator = "<="
        else:
            raise TypeError("as_of must be a date, datetime, or None.")
        for name, query in CHECKS.items():
            if name == "completed_returns_missing_or_wrong_reversals":
                count, rows = _check_return_reversals(cursor, sample_limit)
                report["checks"][name] = {"issue_count": count, "sample": rows}
                report["passed"] = report["passed"] and count == 0
                continue
            if name == "expired_lots_still_open":
                cursor.execute(query.replace("<= COALESCE", f"{expiry_operator} COALESCE"),
                               (expiry_cutoff,))
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


def _check_return_reversals(cursor, sample_limit):
    """Replay return targets against accruals and expiry events in event time."""
    cursor.execute(
        """
        SELECT r.return_id, r.order_id, r.refund_amount, r.return_date,
               o.customer_id, o.total_amount,
               a.transaction_id AS accrual_id, a.points_change AS earned_points,
               a.eligible_spend AS accrued_eligible_spend,
               a.transaction_date AS accrual_time,
               EXISTS (SELECT 1 FROM audit_events AS z
                       WHERE z.entity_type = 'order' AND z.entity_id = o.order_id
                         AND z.event_type = 'QUALIFYING_SPEND_RECORDED') AS zero_spend_recorded
        FROM order_returns AS r
        JOIN orders AS o ON o.order_id = r.order_id
        LEFT JOIN points_transactions AS a
          ON a.order_id = o.order_id AND a.transaction_type = 'ACCRUAL'
        WHERE r.return_status = 'COMPLETED'
        ORDER BY o.order_id, r.return_date, r.return_id
        """
    )
    returns = cursor.fetchall()
    cursor.execute(
        """
        SELECT l.source_transaction_id, t.transaction_date, d.points_expired
        FROM expiry_batch_details AS d
        JOIN point_lots AS l ON l.lot_id = d.lot_id
        JOIN points_transactions AS t ON t.transaction_id = d.transaction_id
        ORDER BY l.source_transaction_id, t.transaction_date
        """
    )
    expiry_by_accrual = {}
    for row in cursor.fetchall():
        expiry_by_accrual.setdefault(row["source_transaction_id"], []).append(
            (row["transaction_date"], int(row["points_expired"]))
        )
    cursor.execute(
        """
        SELECT transaction_id, idempotency_key, points_change
        FROM points_transactions WHERE transaction_type = 'REVERSAL'
        """
    )
    reversals_by_key = {}
    for row in cursor.fetchall():
        reversals_by_key.setdefault(row["idempotency_key"], []).append(row)

    issues = []
    by_order = {}
    for row in returns:
        by_order.setdefault(row["order_id"], []).append(row)
    for order_id, order_returns in by_order.items():
        first = order_returns[0]
        accrual_id = first["accrual_id"]
        earned = int(first["earned_points"] or 0)
        accrued_eligible_spend = Decimal(str(first["accrued_eligible_spend"] or 0))
        accrual_time = first["accrual_time"]
        total = Decimal(str(first["total_amount"]))
        if accrual_id is None and not first["zero_spend_recorded"]:
            for row in order_returns:
                issues.append({
                    "return_id": row["return_id"], "order_id": order_id,
                    "issue": "completed return has no recorded accrual decision",
                })
            continue

        cumulative_refund = Decimal("0")
        expected_reversed = 0
        for row in order_returns:
            # Returns completed before accrual are already reflected in its
            # eligible_spend and must not generate a second reversal.
            if accrual_time is not None and row["return_date"] <= accrual_time:
                continue
            cumulative_refund += Decimal(str(row["refund_amount"]))
            # Order-line eligibility can be mixed. The accrual's eligible spend
            # records the post-pre-accrual-refund base; reconstruct the original
            # eligible fraction from source lines below.
            cursor.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(
                    CASE WHEN is_eligible THEN quantity * unit_price - discount_amount
                         ELSE 0 END), 0)
                FROM order_lines WHERE order_id = %s
                """,
                (order_id,),
            )
            eligibility_row = cursor.fetchone()
            line_count, original_eligible = eligibility_row.values()
            original_eligible = (Decimal(str(original_eligible)) if line_count
                                 else total)
            eligible_refund = cumulative_refund * original_eligible / total if total else 0
            target = (int((Decimal(earned) * eligible_refund / accrued_eligible_spend)
                          .to_integral_value(rounding=ROUND_FLOOR))
                      if accrued_eligible_spend > 0 else 0)
            expired_before_return = sum(
                points for expired_at, points in expiry_by_accrual.get(accrual_id, [])
                if expired_at <= row["return_date"]
            ) if accrual_id is not None else 0
            expected_increment = max(
                0, target - expected_reversed - expired_before_return
            )
            key = f"REVERSAL:{row['return_id']}"
            actual_rows = reversals_by_key.get(key, [])
            actual_increment = sum(-int(item["points_change"]) for item in actual_rows)
            if len(actual_rows) != (1 if expected_increment else 0) \
                    or actual_increment != expected_increment:
                issues.append({
                    "return_id": row["return_id"], "order_id": order_id,
                    "expected_reversal": expected_increment,
                    "actual_reversal": actual_increment,
                    "expired_points_offset": min(
                        expired_before_return,
                        max(0, target - expected_reversed),
                    ),
                })
            expected_reversed += expected_increment
    return len(issues), issues[:sample_limit]


if __name__ == "__main__":
    try:
        report = validate_data()
        print(json.dumps(report, indent=2, default=str))
        if not report["passed"]:
            raise SystemExit(1)
    except Exception as error:
        print(f"Validation could not run: {error}")
        raise SystemExit(1) from None
