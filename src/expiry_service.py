"""Expire unspent point lots once per lot and expiry batch."""

import json
from datetime import date

from db import get_connection
from ids import next_id


def expire_points(as_of=None):
    """Expire all remaining lots due by ``as_of`` (defaults to today).

    Returns the batch ID, number of lots processed, and total points expired.
    A lot is reduced to zero in the same transaction as its ledger entry, so a
    repeated run will not expire it again.
    """
    if as_of is None:
        as_of = date.today()

    connection = get_connection()
    cursor = connection.cursor()
    try:
        batch_id = next_id(cursor, "expiry_batches", "EXP", 6)
        cursor.execute(
            """
            INSERT INTO expiry_batches (expiry_batch_id, expiry_date, batch_status)
            VALUES (%s, %s, 'PENDING')
            """,
            (batch_id, as_of),
        )
        cursor.execute(
            """
            SELECT DISTINCT customer_id FROM point_lots
            WHERE points_remaining > 0 AND DATE(expires_at) <= %s
            ORDER BY customer_id
            """,
            (as_of,),
        )
        customer_ids = [row[0] for row in cursor.fetchall()]
        if customer_ids:
            placeholders = ", ".join(["%s"] * len(customer_ids))
            cursor.execute(
                f"""
                SELECT customer_id FROM loyalty_accounts
                WHERE customer_id IN ({placeholders})
                ORDER BY customer_id
                FOR UPDATE
                """,
                tuple(customer_ids),
            )
            cursor.fetchall()

        cursor.execute(
            """
            SELECT lot_id, customer_id, source_transaction_id, points_remaining
            FROM point_lots
            WHERE points_remaining > 0 AND DATE(expires_at) <= %s
            ORDER BY customer_id, expires_at, lot_id
            FOR UPDATE
            """,
            (as_of,),
        )
        lots = cursor.fetchall()
        total_points = 0

        for lot_id, customer_id, source_transaction_id, points in lots:
            transaction_id = next_id(cursor, "points", "PTS", 8)
            cursor.execute(
                """
                INSERT INTO points_transactions
                    (transaction_id, customer_id, transaction_type, points_change,
                     transaction_date, description, idempotency_key,
                     related_transaction_id)
                VALUES (%s, %s, 'EXPIRY', %s, NOW(), %s, %s, %s)
                """,
                (transaction_id, customer_id, -points,
                 f"Expired lot {lot_id}", f"EXPIRY:{lot_id}", source_transaction_id),
            )
            cursor.execute(
                """
                UPDATE loyalty_accounts
                SET current_points = current_points - %s
                WHERE customer_id = %s
                """,
                (points, customer_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Loyalty account missing for customer {customer_id}.")

            cursor.execute(
                """
                UPDATE point_lots
                SET points_remaining = 0, lot_status = 'EXPIRED'
                WHERE lot_id = %s AND points_remaining = %s
                """,
                (lot_id, points),
            )
            cursor.execute(
                """
                INSERT INTO expiry_batch_details
                    (expiry_batch_id, lot_id, transaction_id, points_expired)
                VALUES (%s, %s, %s, %s)
                """,
                (batch_id, lot_id, transaction_id, points),
            )
            cursor.execute(
                """
                INSERT INTO audit_events (entity_type, entity_id, event_type, event_details)
                VALUES ('point_lot', %s, 'EXPIRED', %s)
                """,
                (str(lot_id), json.dumps({
                    "batch_id": batch_id,
                    "points_expired": points,
                    "transaction_id": transaction_id,
                })),
            )
            total_points += points

        cursor.execute(
            "UPDATE expiry_batches SET batch_status = 'COMPLETED' WHERE expiry_batch_id = %s",
            (batch_id,),
        )
        connection.commit()
        return {"batch_id": batch_id, "lots_expired": len(lots),
                "points_expired": total_points}
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


if __name__ == "__main__":
    try:
        result = expire_points()
        print(f"Expiry batch: {result['batch_id']}")
        print(f"Lots expired: {result['lots_expired']}")
        print(f"Points expired: {result['points_expired']}")
    except Exception as error:
        print(f"Expiry failed: {error}")
        raise SystemExit(1) from None
