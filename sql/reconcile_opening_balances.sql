-- Reconcile positive account balances that existed before points_transactions
-- and point_lots were introduced. Safe to run more than once.
-- Run only after sql/migrate_v2.sql has created the v2 tables and columns.
USE loyalty_ledger;

START TRANSACTION;

-- Add one adjustment for each account balance that is greater than the sum of
-- its recorded transactions. Deterministic IDs and idempotency keys make this
-- safe to rerun after a partial execution.
INSERT INTO points_transactions (
    transaction_id,
    customer_id,
    order_id,
    transaction_type,
    points_change,
    transaction_date,
    description,
    idempotency_key
)
SELECT
    CONCAT('OPEN-', LPAD(a.customer_id, 10, '0')),
    a.customer_id,
    NULL,
    'ADJUSTMENT',
    a.current_points - COALESCE(SUM(t.points_change), 0),
    CURRENT_TIMESTAMP,
    'Opening balance carried forward during ledger migration',
    CONCAT('OPENING-BALANCE-CUSTOMER-', a.customer_id)
FROM loyalty_accounts AS a
LEFT JOIN points_transactions AS t ON t.customer_id = a.customer_id
GROUP BY a.customer_id, a.current_points
HAVING a.current_points - COALESCE(SUM(t.points_change), 0) > 0
   AND NOT EXISTS (
       SELECT 1
       FROM points_transactions AS existing
       WHERE existing.idempotency_key =
             CONCAT('OPENING-BALANCE-CUSTOMER-', a.customer_id)
   )
   AND NOT EXISTS (
       SELECT 1
       FROM points_transactions AS existing
       WHERE existing.transaction_id =
             CONCAT('OPEN-', LPAD(a.customer_id, 10, '0'))
   );

-- Create a matching non-expiring legacy lot, limited to the portion of the
-- account balance not already represented by open lots.
INSERT INTO point_lots (
    customer_id,
    source_transaction_id,
    points_earned,
    points_remaining,
    earned_at,
    expires_at,
    lot_status
)
SELECT
    tx.customer_id,
    tx.transaction_id,
    tx.points_change,
    LEAST(
        tx.points_change,
        GREATEST(0, a.current_points - COALESCE(SUM(l.points_remaining), 0))
    ),
    tx.transaction_date,
    NULL,
    CASE
        WHEN LEAST(
            tx.points_change,
            GREATEST(0, a.current_points - COALESCE(SUM(l.points_remaining), 0))
        ) = 0 THEN 'EXHAUSTED'
        WHEN LEAST(
            tx.points_change,
            GREATEST(0, a.current_points - COALESCE(SUM(l.points_remaining), 0))
        ) = tx.points_change THEN 'ACTIVE'
        ELSE 'PARTIALLY_USED'
    END
FROM points_transactions AS tx
JOIN loyalty_accounts AS a ON a.customer_id = tx.customer_id
LEFT JOIN point_lots AS l ON l.customer_id = tx.customer_id
WHERE tx.transaction_type = 'ADJUSTMENT'
  AND tx.idempotency_key = CONCAT('OPENING-BALANCE-CUSTOMER-', tx.customer_id)
  AND NOT EXISTS (
      SELECT 1 FROM point_lots AS existing_lot
      WHERE existing_lot.source_transaction_id = tx.transaction_id
  )
GROUP BY tx.customer_id, tx.transaction_id, tx.points_change,
         tx.transaction_date, a.current_points;

COMMIT;

-- Show any remaining account-to-ledger differences. This should return no rows.
SELECT a.customer_id,
       a.current_points,
       COALESCE(SUM(t.points_change), 0) AS ledger_points
FROM loyalty_accounts AS a
LEFT JOIN points_transactions AS t ON t.customer_id = a.customer_id
GROUP BY a.customer_id, a.current_points
HAVING a.current_points <> COALESCE(SUM(t.points_change), 0);
