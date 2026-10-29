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


def _audit(cursor, entity_type, entity_id, event_type, details):
    cursor.execute(
        """
        INSERT INTO audit_events (entity_type, entity_id, event_type, event_details)
        VALUES (%s, %s, %s, %s)
        """,
        (entity_type, str(entity_id), event_type, json.dumps(details, default=str)),
    )


def _qualifying_spend(cursor, customer_id):
    cursor.execute(
        """
        SELECT COALESCE(SUM(
            (CASE
                WHEN EXISTS (SELECT 1 FROM order_lines AS any_line
                             WHERE any_line.order_id = orders.order_id)
                THEN COALESCE(
                    (SELECT SUM((line.quantity * line.unit_price) - line.discount_amount)
                     FROM order_lines AS line
                     WHERE line.order_id = orders.order_id AND line.is_eligible = TRUE),
                    0
                )
                ELSE orders.total_amount
            END) - COALESCE(
                (SELECT SUM(order_returns.refund_amount)
                 FROM order_returns
                 WHERE order_returns.order_id = orders.order_id
                   AND order_returns.return_status = 'COMPLETED'),
                0
            )
        ), 0)
        FROM orders
        WHERE customer_id = %s AND order_status IN ('COMPLETED', 'RETURNED')
        """,
        (customer_id,),
    )
    return max(Decimal("0"), Decimal(str(cursor.fetchone()[0])))


def _refresh_tier(cursor, customer_id):
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
    new_tier_id = tier[0] if tier else None

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
                    (customer_id, old_tier_id, new_tier_id, qualifying_spend)
                VALUES (%s, %s, %s, %s)
                """,
                (customer_id, old_tier_id, new_tier_id, spend),
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

        cursor.execute(
            """
            SELECT COALESCE(SUM((quantity * unit_price) - discount_amount), 0)
            FROM order_lines
            WHERE order_id = %s AND is_eligible = TRUE
            """,
            (order_id,),
        )
        eligible_spend = cursor.fetchone()[0]
        cursor.execute(
            "SELECT COUNT(*) FROM order_lines WHERE order_id = %s",
            (order_id,),
        )
        if cursor.fetchone()[0] == 0:
            eligible_spend = total_amount

        cursor.execute(
            "SELECT tier_id, current_points FROM loyalty_accounts "
            "WHERE customer_id = %s FOR UPDATE",
            (customer_id,),
        )
        account = cursor.fetchone()
        if account is None:
            raise ValueError("Loyalty account not found.")
        starting_balance = account[1]

        points_per_100 = Decimal("1")
        if account[0] is not None:
            cursor.execute(
                "SELECT points_per_100 FROM tiers WHERE tier_id = %s",
                (account[0],),
            )
            tier = cursor.fetchone()
            if tier:
                points_per_100 = Decimal(str(tier[0]))
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
            _refresh_tier(cursor, customer_id)
            _audit(cursor, "order", order_id, "QUALIFYING_SPEND_RECORDED", {
                "customer_id": customer_id,
                "eligible_spend": str(eligible_spend),
                "points_awarded": 0,
            })
            connection.commit()
            return {"order_id": order_id, "points": 0, "transaction_id": None}

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

        transaction_id = next_id(cursor, "points", "PTS", 8)
        cursor.execute(
            """
            INSERT INTO points_transactions
                (transaction_id, customer_id, order_id, transaction_type,
                 points_change, transaction_date, description, idempotency_key)
            VALUES (%s, %s, %s, 'ACCRUAL', %s, NOW(), %s, %s)
            """,
            (transaction_id, customer_id, order_id, points,
             f"Eligible spend {eligible_spend} earned {points} points", idempotency_key),
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
             order_date, order_date, expiry_days, lot_status),
        )
        cursor.execute(
            """
            UPDATE loyalty_accounts SET current_points = current_points + %s
            WHERE customer_id = %s
            """,
            (points, customer_id),
        )
        _refresh_tier(cursor, customer_id)
        _audit(cursor, "points_transaction", transaction_id, "ACCRUED", {
            "customer_id": customer_id,
            "order_id": order_id,
            "points": points,
            "eligible_spend": str(eligible_spend),
        })
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
            SELECT r.order_id, r.refund_amount, r.return_status,
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
        order_id, refund_amount, status, customer_id, order_amount = row
        if status != "REQUESTED":
            raise ValueError("Only a requested return can be completed.")
        if order_amount <= 0:
            raise ValueError("Cannot process a return for a zero-value order.")

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
            SELECT transaction_id, points_change FROM points_transactions
            WHERE order_id = %s AND transaction_type = 'ACCRUAL'
            """,
            (order_id,),
        )
        accrual = cursor.fetchone()
        reversed_points = 0
        reversal_id = None
        if accrual:
            accrual_id, accrual_points = accrual
            cursor.execute(
                """
                SELECT COALESCE(SUM(-points_change), 0)
                FROM points_transactions
                WHERE order_id = %s AND transaction_type = 'REVERSAL'
                """,
                (order_id,),
            )
            already_reversed = cursor.fetchone()[0]
            total_refunds = previous_refunds + Decimal(str(refund_amount))
            target_reversal = int(
                (Decimal(accrual_points) * total_refunds / Decimal(str(order_amount)))
                .quantize(Decimal("1"), rounding=ROUND_FLOOR)
            )
            reversed_points = max(0, target_reversal - already_reversed)
            if reversed_points:
                reversal_id = next_id(cursor, "points", "PTS", 8)
                cursor.execute(
                    """
                    INSERT INTO points_transactions
                        (transaction_id, customer_id, order_id, transaction_type,
                         points_change, transaction_date, description,
                         idempotency_key, related_transaction_id)
                    VALUES (%s, %s, %s, 'REVERSAL', %s, NOW(), %s, %s, %s)
                    """,
                    (reversal_id, customer_id, order_id, -reversed_points,
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
                    remaining_to_remove -= removed

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

        _refresh_tier(cursor, customer_id)
        _audit(cursor, "order_return", return_id, "RETURN_COMPLETED", {
            "order_id": order_id,
            "refund_amount": str(refund_amount),
            "points_reversed": reversed_points,
            "transaction_id": reversal_id,
        })
        connection.commit()
        return {"return_id": return_id, "points_reversed": reversed_points,
                "transaction_id": reversal_id}
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()
