"""Core accrual, return reversal, balance, and tier operations."""

import json
from datetime import timedelta
from decimal import Decimal, ROUND_FLOOR

from db import get_connection
from ids import next_id


def calculate_points(eligible_spend, points_per_100=1):
    """Calculate whole points from eligible net spend using a tier's rate."""
    spend = Decimal(str(eligible_spend))
    rate = Decimal(str(points_per_100))
    if spend < 0 or rate < 0:
        raise ValueError("Spend and earning rate cannot be negative.")
    points = (spend * rate / Decimal("100")).quantize(
        Decimal("1"), rounding=ROUND_FLOOR
    )
    return int(points)


def _audit(cursor, entity_type, entity_id, event_type, details, event_time=None):
    if event_time is None:
        cursor.execute(
            """
            INSERT INTO audit_events (entity_type, entity_id, event_type, event_details)
            VALUES (%s, %s, %s, %s)
            """,
            (entity_type, str(entity_id), event_type, json.dumps(details, default=str)),
        )
    else:
        cursor.execute(
            """
            INSERT INTO audit_events
                (entity_type, entity_id, event_type, event_details, created_at)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (entity_type, str(entity_id), event_type,
             json.dumps(details, default=str), event_time),
        )


def _eligible_order_spend(cursor, order_id, order_total):
    cursor.execute(
        """
        SELECT COUNT(*), COALESCE(SUM(
            CASE WHEN is_eligible = TRUE
                 THEN (quantity * unit_price) - discount_amount ELSE 0 END
        ), 0)
        FROM order_lines WHERE order_id = %s
        """,
        (order_id,),
    )
    line_count, eligible_spend = cursor.fetchone()
    return Decimal(str(eligible_spend if line_count else order_total))


def _qualifying_spend(cursor, customer_id, as_of=None, exclude_order=None):
    cursor.execute(
        """
        SELECT COALESCE(SUM(GREATEST(
            0,
            eligible_spend - CASE WHEN total_amount > 0
                THEN refunds * eligible_spend / total_amount ELSE 0 END
        )), 0)
        FROM (
            SELECT o.order_id, o.total_amount,
                   CASE WHEN EXISTS (
                       SELECT 1 FROM order_lines AS any_line
                       WHERE any_line.order_id = o.order_id
                   ) THEN COALESCE((
                       SELECT SUM((line.quantity * line.unit_price) - line.discount_amount)
                       FROM order_lines AS line
                       WHERE line.order_id = o.order_id AND line.is_eligible = TRUE
                   ), 0) ELSE o.total_amount END AS eligible_spend,
                   COALESCE((
                       SELECT SUM(r.refund_amount) FROM order_returns AS r
                       WHERE r.order_id = o.order_id
                         AND r.return_status = 'COMPLETED'
                         AND (%s IS NULL OR r.return_date <= %s)
                   ), 0) AS refunds
            FROM orders AS o
            WHERE o.customer_id = %s
              AND o.order_status IN ('COMPLETED', 'RETURNED')
              AND (%s IS NULL OR o.order_date < %s
                   OR (o.order_date = %s AND o.order_id < %s))
              AND (
                  EXISTS (SELECT 1 FROM points_transactions AS t
                          WHERE t.order_id = o.order_id AND t.transaction_type = 'ACCRUAL')
                  OR EXISTS (SELECT 1 FROM audit_events AS a
                             WHERE a.entity_type = 'order' AND a.entity_id = o.order_id
                               AND a.event_type = 'QUALIFYING_SPEND_RECORDED')
              )
        ) AS order_spend
        """,
        (as_of, as_of, customer_id, as_of, as_of, as_of, exclude_order or ""),
    )
    return max(Decimal("0"), Decimal(str(cursor.fetchone()[0])))


def _tier_for_spend(cursor, spend):
    cursor.execute(
        """
        SELECT tier_id FROM tiers
        WHERE minimum_spend <= %s
        ORDER BY minimum_spend DESC, tier_id
        LIMIT 1
        """,
        (spend,),
    )
    tier = cursor.fetchone()
    return tier[0] if tier else None


def _active_earning_rule(cursor, tier_id, event_time):
    cursor.execute(
        """
        SELECT rule_id, points_per_100 FROM rules
        WHERE tier_id = %s AND is_active = TRUE
          AND effective_from <= %s
          AND (effective_to IS NULL OR effective_to >= %s)
        ORDER BY effective_from DESC, rule_id DESC
        LIMIT 1
        """,
        (tier_id, event_time.date(), event_time.date()),
    )
    return cursor.fetchone()


def _require_event_order(cursor, customer_id, event_time):
    cursor.execute(
        """
        SELECT MAX(event_time) FROM (
            SELECT transaction_date AS event_time
            FROM points_transactions WHERE customer_id = %s
            UNION ALL
            SELECT a.created_at AS event_time
            FROM audit_events AS a
            JOIN orders AS o ON o.order_id = a.entity_id
            WHERE o.customer_id = %s AND a.entity_type = 'order'
              AND a.event_type = 'QUALIFYING_SPEND_RECORDED'
        ) AS processed_events
        """,
        (customer_id, customer_id),
    )
    latest_event = cursor.fetchone()[0]
    if latest_event is not None and event_time < latest_event:
        raise ValueError(
            "Process loyalty events in timestamp order. For historical imports, "
            "sort and replay the source events before applying them."
        )


def _refresh_tier(cursor, customer_id, event_time=None):
    spend = _qualifying_spend(cursor, customer_id)
    cursor.execute(
        "SELECT tier_id, current_points FROM loyalty_accounts "
        "WHERE customer_id = %s FOR UPDATE",
        (customer_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise ValueError("Loyalty account not found.")
    old_tier_id = row[0]

    new_tier_id = _tier_for_spend(cursor, spend)

    cursor.execute(
        """
        UPDATE loyalty_accounts
        SET tier_id = %s, qualifying_spend = %s
        WHERE customer_id = %s
        """,
        (new_tier_id, spend, customer_id),
    )
    if new_tier_id != old_tier_id:
        if new_tier_id is not None:
            cursor.execute(
                """
                INSERT INTO account_tier_history
                    (customer_id, old_tier_id, new_tier_id, qualifying_spend,
                     changed_at)
                VALUES (%s, %s, %s, %s, COALESCE(%s, UTC_TIMESTAMP()))
                """,
                (customer_id, old_tier_id, new_tier_id, spend, event_time),
            )
            _audit(cursor, "loyalty_account", customer_id, "TIER_CHANGED", {
                "old_tier_id": old_tier_id,
                "new_tier_id": new_tier_id,
                "qualifying_spend": str(spend),
            })


def get_customer_balance(customer_id):
    """Return the cached current points balance, or None when no account exists."""
    connection = get_connection()
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT current_points FROM loyalty_accounts WHERE customer_id = %s",
                (customer_id,),
            )
            result = cursor.fetchone()
            return result[0] if result else None
        finally:
            cursor.close()
    finally:
        connection.close()


def award_points(order_id, expiry_days=365):
    """Accrue points once for a completed order and create its expiry lot."""
    if expiry_days <= 0:
        raise ValueError("Expiry days must be a positive number.")

    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            SELECT customer_id, total_amount, order_status, order_date
            FROM orders WHERE order_id = %s FOR UPDATE
            """,
            (order_id,),
        )
        order = cursor.fetchone()
        if order is None:
            raise ValueError("Order not found.")
        customer_id, total_amount, status, order_date = order
        if status != "COMPLETED":
            raise ValueError("Only completed orders earn points.")
        idempotency_key = f"ACCRUAL:{order_id}"
        cursor.execute(
            """
            SELECT transaction_id FROM points_transactions
            WHERE idempotency_key = %s
               OR (order_id = %s AND transaction_type = 'ACCRUAL')
            """,
            (idempotency_key, order_id),
        )
        if cursor.fetchone():
            raise ValueError("Points have already been awarded for this order.")

        # Serialize all customer balance/tier decisions before reading the
        # event-time spend. Different orders for one account may be awarded
        # concurrently by separate workers.
        cursor.execute(
            "SELECT customer_id FROM loyalty_accounts "
            "WHERE customer_id = %s FOR UPDATE",
            (customer_id,),
        )
        if cursor.fetchone() is None:
            raise ValueError("Loyalty account not found.")
        cursor.execute("SELECT UTC_TIMESTAMP(6)")
        processed_at = cursor.fetchone()[0]
        _require_event_order(cursor, customer_id, processed_at)

        cursor.execute(
            """
            SELECT COALESCE(SUM((quantity * unit_price) - discount_amount), 0)
            FROM order_lines
            WHERE order_id = %s AND is_eligible = TRUE
            """,
            (order_id,),
        )
        eligible_spend = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM order_lines WHERE order_id = %s", (order_id,))
        if cursor.fetchone()[0] == 0:
            eligible_spend = total_amount
        eligible_spend = Decimal(str(eligible_spend))
        cursor.execute(
            """
            SELECT COALESCE(SUM(refund_amount), 0)
            FROM order_returns
            WHERE order_id = %s AND return_status = 'COMPLETED'
              AND return_date <= %s
            """,
            (order_id, processed_at),
        )
        completed_refunds = Decimal(str(cursor.fetchone()[0]))
        eligible_spend = max(
            Decimal("0"), eligible_spend -
            (completed_refunds * eligible_spend / Decimal(str(total_amount)))
        )

        prior_spend = _qualifying_spend(
            cursor, customer_id, as_of=processed_at, exclude_order=order_id
        )
        event_tier_id = _tier_for_spend(cursor, prior_spend)
        if event_tier_id is None:
            raise ValueError("No tier is configured for the order's event-time spend.")
        rule = _active_earning_rule(cursor, event_tier_id, processed_at)
        if rule is None:
            raise ValueError(
                "No active earning rule covers the order date and event-time tier."
            )
        rule_id, points_per_100 = rule
        points_per_100 = Decimal(str(points_per_100))

        cursor.execute(
            "SELECT tier_id, current_points FROM loyalty_accounts "
            "WHERE customer_id = %s FOR UPDATE",
            (customer_id,),
        )
        account = cursor.fetchone()
        if account is None:
            raise ValueError("Loyalty account not found.")
        starting_balance = account[1]

        points = calculate_points(eligible_spend, points_per_100)
        if points == 0:
            cursor.execute(
                """
                SELECT audit_id FROM audit_events
                WHERE entity_type = 'order' AND entity_id = %s
                  AND event_type = 'QUALIFYING_SPEND_RECORDED'
                """,
                (order_id,),
            )
            if cursor.fetchone():
                raise ValueError("This order's qualifying spend was already processed.")
            _refresh_tier(cursor, customer_id, event_time=processed_at)
            _audit(cursor, "order", order_id, "QUALIFYING_SPEND_RECORDED", {
                "customer_id": customer_id,
                "eligible_spend": str(eligible_spend),
                "points_awarded": 0,
                "rule_id": rule_id,
                "points_per_100": str(points_per_100),
                "tier_id": event_tier_id,
            }, event_time=processed_at)
            connection.commit()
            return {"order_id": order_id, "points": 0, "transaction_id": None}

        transaction_id = next_id(cursor, "points", "PTS", 8)
        cursor.execute(
            """
            INSERT INTO points_transactions
                (transaction_id, customer_id, order_id, transaction_type,
                 points_change, transaction_date, description, idempotency_key,
                 rule_id, earning_rate, eligible_spend)
            VALUES (%s, %s, %s, 'ACCRUAL', %s, %s, %s, %s, %s, %s, %s)
            """,
            (transaction_id, customer_id, order_id, points, processed_at,
             f"Eligible spend {eligible_spend} earned {points} points", idempotency_key,
             rule_id, points_per_100, eligible_spend),
        )

        lot_remaining = min(points, max(0, starting_balance + points))
        lot_status = (
            "EXHAUSTED" if lot_remaining == 0
            else "PARTIALLY_USED" if lot_remaining < points
            else "ACTIVE"
        )
        cursor.execute(
            """
            INSERT INTO point_lots
                (customer_id, source_transaction_id, points_earned,
                 points_remaining, earned_at, expires_at, lot_status)
            VALUES (%s, %s, %s, %s, %s, DATE_ADD(%s, INTERVAL %s DAY), %s)
            """,
            (customer_id, transaction_id, points, lot_remaining,
             processed_at, processed_at, expiry_days, lot_status),
        )
        cursor.execute(
            """
            UPDATE loyalty_accounts SET current_points = current_points + %s
            WHERE customer_id = %s
            """,
            (points, customer_id),
        )
        _refresh_tier(cursor, customer_id, event_time=processed_at)
        _audit(cursor, "points_transaction", transaction_id, "ACCRUED", {
            "customer_id": customer_id,
            "order_id": order_id,
            "points": points,
            "eligible_spend": str(eligible_spend),
            "rule_id": rule_id,
            "points_per_100": str(points_per_100),
            "tier_id": event_tier_id,
        }, event_time=processed_at)
        connection.commit()
        return {"order_id": order_id, "points": points, "transaction_id": transaction_id}
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


def complete_return(return_id):
    """Complete a requested full/partial return and reverse earned points proportionally."""
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            SELECT r.order_id, r.refund_amount, r.return_status, r.return_date,
                   o.customer_id, o.total_amount
            FROM order_returns AS r
            JOIN orders AS o ON o.order_id = r.order_id
            WHERE r.return_id = %s FOR UPDATE
            """,
            (return_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise ValueError("Return not found.")
        order_id, refund_amount, status, return_time, customer_id, order_amount = row
        if status != "REQUESTED":
            raise ValueError("Only a requested return can be completed.")
        if order_amount <= 0:
            raise ValueError("Cannot process a return for a zero-value order.")
        _require_event_order(cursor, customer_id, return_time)

        cursor.execute(
            """
            SELECT transaction_id FROM points_transactions
            WHERE order_id = %s AND transaction_type = 'ACCRUAL'
            """,
            (order_id,),
        )
        accrual = cursor.fetchone()

        cursor.execute(
            """
            SELECT COALESCE(SUM(refund_amount), 0)
            FROM order_returns
            WHERE order_id = %s AND return_status = 'COMPLETED'
            """,
            (order_id,),
        )
        previous_refunds = Decimal(str(cursor.fetchone()[0]))
        if previous_refunds + Decimal(str(refund_amount)) > Decimal(str(order_amount)):
            raise ValueError("Completed refunds cannot exceed the order total.")

        cursor.execute(
            "UPDATE order_returns SET return_status = 'COMPLETED' WHERE return_id = %s",
            (return_id,),
        )
        cursor.execute(
            "SELECT customer_id FROM loyalty_accounts WHERE customer_id = %s FOR UPDATE",
            (customer_id,),
        )
        if cursor.fetchone() is None:
            raise ValueError("Loyalty account not found.")
        cursor.execute(
            """
            SELECT transaction_id, points_change, eligible_spend, transaction_date
            FROM points_transactions
            WHERE order_id = %s AND transaction_type = 'ACCRUAL'
            """,
            (order_id,),
        )
        accrual = cursor.fetchone()
        reversed_points = 0
        reversal_id = None
        points_removed_from_lots = 0
        eligible_spend = _eligible_order_spend(cursor, order_id, order_amount)
        if accrual:
            accrual_id, accrual_points, accrued_eligible_spend, accrual_time = accrual
            cursor.execute(
                """
                SELECT COALESCE(SUM(-points_change), 0)
                FROM points_transactions
                WHERE order_id = %s AND transaction_type = 'REVERSAL'
                """,
                (order_id,),
            )
            already_reversed = cursor.fetchone()[0]
            cursor.execute(
                """
                SELECT COALESCE(SUM(refund_amount), 0)
                FROM order_returns
                WHERE order_id = %s AND return_status = 'COMPLETED'
                  AND return_date > %s AND return_date <= %s
                """,
                (order_id, accrual_time, return_time),
            )
            refunds_since_accrual = Decimal(str(cursor.fetchone()[0]))
            eligible_refund = (refunds_since_accrual * eligible_spend /
                               Decimal(str(order_amount)))
            target_reversal = (
                int((Decimal(accrual_points) * eligible_refund /
                     Decimal(str(accrued_eligible_spend)))
                    .quantize(Decimal("1"), rounding=ROUND_FLOOR))
                if accrued_eligible_spend and Decimal(str(accrued_eligible_spend)) > 0 else 0
            )
            cursor.execute(
                """
                SELECT COALESCE(SUM(d.points_expired), 0)
                FROM point_lots AS l
                JOIN expiry_batch_details AS d ON d.lot_id = l.lot_id
                JOIN points_transactions AS expiry_tx
                  ON expiry_tx.transaction_id = d.transaction_id
                WHERE l.source_transaction_id = %s
                  AND expiry_tx.transaction_date <= %s
                """,
                (accrual_id, return_time),
            )
            expired_points = int(cursor.fetchone()[0])
            points_after_previous_reversals = max(
                0, target_reversal - int(already_reversed)
            )
            expired_offset = min(expired_points, points_after_previous_reversals)
            reversed_points = points_after_previous_reversals - expired_offset
            if reversed_points:
                reversal_id = next_id(cursor, "points", "PTS", 8)
                cursor.execute(
                    """
                    INSERT INTO points_transactions
                        (transaction_id, customer_id, order_id, transaction_type,
                         points_change, transaction_date, description,
                         idempotency_key, related_transaction_id)
                    VALUES (%s, %s, %s, 'REVERSAL', %s, %s, %s, %s, %s)
                    """,
                    (reversal_id, customer_id, order_id, -reversed_points, return_time,
                     f"Return {return_id} reversed {reversed_points} points",
                     f"REVERSAL:{return_id}", accrual_id),
                )
                cursor.execute(
                    """
                    UPDATE loyalty_accounts SET current_points = current_points - %s
                    WHERE customer_id = %s
                    """,
                    (reversed_points, customer_id),
                )
                cursor.execute(
                    """
                    SELECT lot_id, source_transaction_id, points_remaining
                    FROM point_lots
                    WHERE customer_id = %s AND points_remaining > 0
                    ORDER BY (source_transaction_id = %s) DESC,
                             expires_at IS NULL, expires_at, earned_at, lot_id
                    FOR UPDATE
                    """,
                    (customer_id, accrual_id),
                )
                remaining_to_remove = reversed_points
                for lot_id, source_id, lot_balance in cursor.fetchall():
                    if remaining_to_remove == 0:
                        break
                    removed = min(lot_balance, remaining_to_remove)
                    new_lot_balance = lot_balance - removed
                    new_status = "REVERSED" if new_lot_balance == 0 else "PARTIALLY_USED"
                    cursor.execute(
                        """
                        UPDATE point_lots
                        SET points_remaining = %s, lot_status = %s
                        WHERE lot_id = %s
                        """,
                        (new_lot_balance, new_status, lot_id),
                    )
                    cursor.execute(
                        """
                        INSERT INTO reversal_lot_allocations
                            (reversal_transaction_id, lot_id, points_removed)
                        VALUES (%s, %s, %s)
                        """,
                        (reversal_id, lot_id, removed),
                    )
                    remaining_to_remove -= removed
                    points_removed_from_lots += removed
        else:
            expired_offset = 0
            target_reversal = 0

        cursor.execute(
            """
            SELECT COALESCE(SUM(refund_amount), 0)
            FROM order_returns WHERE order_id = %s AND return_status = 'COMPLETED'
            """,
            (order_id,),
        )
        cumulative_refund = Decimal(str(cursor.fetchone()[0]))
        if cumulative_refund >= Decimal(str(order_amount)):
            cursor.execute(
                "UPDATE orders SET order_status = 'RETURNED' WHERE order_id = %s",
                (order_id,),
            )

        _refresh_tier(cursor, customer_id, event_time=return_time)
        _audit(cursor, "order_return", return_id, "RETURN_COMPLETED", {
            "order_id": order_id,
            "refund_amount": str(refund_amount),
            "points_reversed": reversed_points,
            "target_reversal": target_reversal,
            "expired_points_offset": expired_offset,
            "spent_point_debt": max(0, reversed_points - points_removed_from_lots),
            "transaction_id": reversal_id,
        }, event_time=return_time)
        connection.commit()
        return {"return_id": return_id, "points_reversed": reversed_points,
                "expired_points_offset": expired_offset,
                "transaction_id": reversal_id}
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()
