"""Expire unspent point lots once per lot and expiry batch."""

import json
from datetime import date, datetime, time, timedelta, timezone

from db import get_connection
from ids import next_id


def expire_points(as_of=None):
    """Expire remaining lots at or before a UTC instant.

    A ``date`` argument means the end of that UTC business date. A ``datetime``
    argument is an exact, timezone-naive UTC instant. Omitting it uses now.

    Returns the batch ID, number of lots processed, and total points expired.
    A lot is reduced to zero in the same transaction as its ledger entry, so a
    repeated run will not expire it again.
    """
    if as_of is None:
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None)
        expiry_date = cutoff.date()
        cutoff_operator = "<="
    elif isinstance(as_of, datetime):
        cutoff = as_of.astimezone(timezone.utc).replace(tzinfo=None) if as_of.tzinfo else as_of
        expiry_date = cutoff.date()
        cutoff_operator = "<="
    elif isinstance(as_of, date):
        expiry_date = as_of
        cutoff = datetime.combine(as_of + timedelta(days=1), time.min)
        cutoff_operator = "<"
    else:
        raise TypeError("as_of must be a date, datetime, or None.")

    connection = get_connection()
    cursor = connection.cursor()
    try:
        batch_id = next_id(cursor, "expiry_batches", "EXP", 6)
        cursor.execute(
            """
            INSERT INTO expiry_batches (expiry_batch_id, expiry_date, batch_status)
            VALUES (%s, %s, 'PENDING')
            """,
            (batch_id, expiry_date),
        )
        cursor.execute(
            f"""
            SELECT DISTINCT customer_id FROM point_lots
            WHERE points_remaining > 0 AND expires_at {cutoff_operator} %s
            ORDER BY customer_id
            """,
            (cutoff,),
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
            f"""
            SELECT lot_id, customer_id, source_transaction_id,
                   points_remaining, expires_at
            FROM point_lots
            WHERE points_remaining > 0 AND expires_at {cutoff_operator} %s
            ORDER BY customer_id, expires_at, lot_id
            FOR UPDATE
            """,
            (cutoff,),
        )
        lots = cursor.fetchall()
        total_points = 0

        for lot_id, customer_id, source_transaction_id, points, expires_at in lots:
            transaction_id = next_id(cursor, "points", "PTS", 8)
            cursor.execute(
                """
                INSERT INTO points_transactions
                    (transaction_id, customer_id, transaction_type, points_change,
                     transaction_date, description, idempotency_key,
                     related_transaction_id)
                VALUES (%s, %s, 'EXPIRY', %s, %s, %s, %s, %s)
                """,
                (transaction_id, customer_id, -points, expires_at,
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
