"""Redemption service using oldest-expiring-points-first lot consumption."""

import json
from datetime import date

from db import get_connection
from ids import next_id


def redeem_points(customer_id, points, reward_id=None):
    """Redeem points atomically and return the redemption ID and new balance."""
    if isinstance(points, bool) or not isinstance(points, int) or points <= 0:
        raise ValueError("Points to redeem must be a positive whole number.")

    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            SELECT current_points FROM loyalty_accounts
            WHERE customer_id = %s FOR UPDATE
            """,
            (customer_id,),
        )
        account = cursor.fetchone()
        if account is None:
            raise ValueError("Loyalty account not found.")
        current_balance = account[0]
        if current_balance < 0 or points > current_balance:
            raise ValueError(
                f"Not enough points. Available: {max(0, current_balance)}; "
                f"requested: {points}."
            )

        if reward_id is not None:
            cursor.execute(
                """
                SELECT points_cost FROM rewards
                WHERE reward_id = %s AND is_active = TRUE
                  AND effective_from <= %s
                  AND (effective_to IS NULL OR effective_to >= %s)
                """,
                (reward_id, date.today(), date.today()),
            )
            reward = cursor.fetchone()
            if reward is None:
                raise ValueError("Reward does not exist or is not currently available.")
            if points != reward[0]:
                raise ValueError("Points requested must match the reward's listed cost.")

        cursor.execute(
            """
            SELECT lot_id, points_remaining
            FROM point_lots
            WHERE customer_id = %s AND points_remaining > 0
              AND (expires_at IS NULL OR expires_at > NOW())
            ORDER BY expires_at IS NULL, expires_at, earned_at, lot_id
            FOR UPDATE
            """,
            (customer_id,),
        )
        lots = cursor.fetchall()
        available_lots = sum(lot[1] for lot in lots)
        if available_lots != max(0, current_balance):
            raise ValueError(
                "The account balance does not match its unexpired point lots. "
                "Run expiry processing or reconcile the ledger first."
            )

        redemption_id = next_id(cursor, "redemptions", "RDM", 6)
        transaction_id = next_id(cursor, "points", "PTS", 8)
        cursor.execute(
            """
            INSERT INTO points_transactions
                (transaction_id, customer_id, transaction_type, points_change,
                 transaction_date, description, idempotency_key)
            VALUES (%s, %s, 'REDEMPTION', %s, NOW(), %s, %s)
            """,
            (transaction_id, customer_id, -points,
             f"Redeemed {points} points", f"REDEMPTION:{redemption_id}"),
        )
        cursor.execute(
            """
            INSERT INTO redemptions
                (redemption_id, customer_id, reward_id, transaction_id,
                 points_redeemed, redemption_date, redemption_status)
            VALUES (%s, %s, %s, %s, %s, NOW(), 'COMPLETED')
            """,
            (redemption_id, customer_id, reward_id, transaction_id, points),
        )

        remaining_to_use = points
        for lot_id, lot_balance in lots:
            if remaining_to_use == 0:
                break
            used = min(lot_balance, remaining_to_use)
            new_lot_balance = lot_balance - used
            lot_status = "EXHAUSTED" if new_lot_balance == 0 else "PARTIALLY_USED"
            cursor.execute(
                """
                UPDATE point_lots SET points_remaining = %s, lot_status = %s
                WHERE lot_id = %s
                """,
                (new_lot_balance, lot_status, lot_id),
            )
            cursor.execute(
                """
                INSERT INTO lot_consumptions (redemption_id, lot_id, points_used)
                VALUES (%s, %s, %s)
                """,
                (redemption_id, lot_id, used),
            )
            remaining_to_use -= used

        cursor.execute(
            """
            UPDATE loyalty_accounts SET current_points = current_points - %s
            WHERE customer_id = %s AND current_points >= %s
            """,
            (points, customer_id, points),
        )
        if cursor.rowcount != 1:
            raise ValueError("The balance changed before redemption completed.")

        cursor.execute(
            """
            INSERT INTO audit_events (entity_type, entity_id, event_type, event_details)
            VALUES ('redemption', %s, 'REDEEMED', %s)
            """,
            (redemption_id, json.dumps({
                "customer_id": customer_id,
                "points": points,
                "reward_id": reward_id,
                "transaction_id": transaction_id,
            })),
        )
        connection.commit()
        return {"redemption_id": redemption_id,
                "transaction_id": transaction_id,
                "remaining_points": current_balance - points}
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


if __name__ == "__main__":
    try:
        customer_id = int(input("Customer ID: "))
        points = int(input("Points to redeem: "))
        result = redeem_points(customer_id, points)
        print(f"Redemption successful: {result['redemption_id']}")
        print(f"Transaction: {result['transaction_id']}")
        print(f"Points remaining: {result['remaining_points']}")
    except (ValueError, RuntimeError) as error:
        print(f"Redemption failed: {error}")
        raise SystemExit(1) from None
    except Exception as error:
        print(f"Database error: {error}")
        raise SystemExit(1) from None
