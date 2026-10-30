-- Upgrade a v2 Loyalty Ledger database for effective tier rules and auditable
-- earning metadata. The migration is safe to rerun.
USE loyalty_ledger;

SET @ddl := IF(
    (SELECT COUNT(*) FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'rules'
       AND COLUMN_NAME = 'tier_id') = 0,
    'ALTER TABLE rules ADD COLUMN tier_id INT NULL AFTER rule_id',
    'SELECT 1'
);
PREPARE migrate_stmt FROM @ddl;
EXECUTE migrate_stmt;
DEALLOCATE PREPARE migrate_stmt;

UPDATE rules AS r
JOIN tiers AS t ON t.tier_id = r.rule_id
SET r.tier_id = t.tier_id,
    r.points_per_100 = t.points_per_100
WHERE r.tier_id IS NULL;

INSERT INTO rules
    (rule_id, tier_id, rule_name, points_per_100, effective_from,
     effective_to, is_active)
SELECT 1000000 + t.tier_id, t.tier_id,
       CONCAT(t.tier_name, ' earning rate'), t.points_per_100,
       '2024-01-01', NULL, TRUE
FROM tiers AS t
WHERE NOT EXISTS (SELECT 1 FROM rules AS r WHERE r.tier_id = t.tier_id)
ON DUPLICATE KEY UPDATE
    tier_id = VALUES(tier_id),
    points_per_100 = VALUES(points_per_100);

SET @ddl := IF(
    (SELECT COUNT(*) FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'points_transactions'
       AND COLUMN_NAME = 'rule_id') = 0,
    'ALTER TABLE points_transactions ADD COLUMN rule_id INT NULL',
    'SELECT 1'
);
PREPARE migrate_stmt FROM @ddl;
EXECUTE migrate_stmt;
DEALLOCATE PREPARE migrate_stmt;

SET @ddl := IF(
    (SELECT COUNT(*) FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'points_transactions'
       AND COLUMN_NAME = 'earning_rate') = 0,
    'ALTER TABLE points_transactions ADD COLUMN earning_rate DECIMAL(6,2) NULL',
    'SELECT 1'
);
PREPARE migrate_stmt FROM @ddl;
EXECUTE migrate_stmt;
DEALLOCATE PREPARE migrate_stmt;

SET @ddl := IF(
    (SELECT COUNT(*) FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'points_transactions'
       AND COLUMN_NAME = 'eligible_spend') = 0,
    'ALTER TABLE points_transactions ADD COLUMN eligible_spend DECIMAL(12,2) NULL',
    'SELECT 1'
);
PREPARE migrate_stmt FROM @ddl;
EXECUTE migrate_stmt;
DEALLOCATE PREPARE migrate_stmt;

SET @ddl := IF(
    (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
     WHERE CONSTRAINT_SCHEMA = DATABASE()
       AND TABLE_NAME = 'rules' AND CONSTRAINT_NAME = 'fk_rules_tier') = 0,
    'ALTER TABLE rules ADD CONSTRAINT fk_rules_tier FOREIGN KEY (tier_id) REFERENCES tiers(tier_id)',
    'SELECT 1'
);
PREPARE migrate_stmt FROM @ddl;
EXECUTE migrate_stmt;
DEALLOCATE PREPARE migrate_stmt;

SET @ddl := IF(
    (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
     WHERE CONSTRAINT_SCHEMA = DATABASE()
       AND TABLE_NAME = 'points_transactions'
       AND CONSTRAINT_NAME = 'fk_points_rule') = 0,
    'ALTER TABLE points_transactions ADD CONSTRAINT fk_points_rule FOREIGN KEY (rule_id) REFERENCES rules(rule_id)',
    'SELECT 1'
);
PREPARE migrate_stmt FROM @ddl;
EXECUTE migrate_stmt;
DEALLOCATE PREPARE migrate_stmt;

CREATE TABLE IF NOT EXISTS reversal_lot_allocations (
    reversal_transaction_id VARCHAR(20) NOT NULL,
    lot_id BIGINT NOT NULL,
    points_removed INT NOT NULL,
    PRIMARY KEY (reversal_transaction_id, lot_id),
    FOREIGN KEY (reversal_transaction_id)
        REFERENCES points_transactions(transaction_id),
    FOREIGN KEY (lot_id) REFERENCES point_lots(lot_id),
    CHECK (points_removed > 0)
) ENGINE=InnoDB;

-- Keep event ordering precise enough to distinguish operations within one second.
ALTER TABLE customers MODIFY created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6);
ALTER TABLE loyalty_accounts MODIFY created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6);
ALTER TABLE account_tier_history MODIFY changed_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6);
ALTER TABLE orders MODIFY order_date DATETIME(6) NOT NULL;
ALTER TABLE points_transactions MODIFY transaction_date DATETIME(6) NOT NULL;
ALTER TABLE point_lots
    MODIFY earned_at DATETIME(6) NOT NULL,
    MODIFY expires_at DATETIME(6) NULL;
ALTER TABLE redemptions MODIFY redemption_date DATETIME(6) NOT NULL;
ALTER TABLE order_returns MODIFY return_date DATETIME(6) NOT NULL;
ALTER TABLE expiry_batches MODIFY created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6);
ALTER TABLE audit_events MODIFY created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6);

-- Rebuild tier and qualifying-spend caches from completed loyalty events.
UPDATE loyalty_accounts AS account
LEFT JOIN (
    SELECT facts.customer_id,
           SUM(GREATEST(0, facts.eligible_spend - CASE
               WHEN facts.total_amount > 0
               THEN facts.refunds * facts.eligible_spend / facts.total_amount
               ELSE 0 END)) AS qualifying_spend
    FROM (
        SELECT o.customer_id, o.order_id, o.total_amount,
               CASE WHEN EXISTS (
                   SELECT 1 FROM order_lines AS any_line
                   WHERE any_line.order_id = o.order_id
               ) THEN COALESCE((
                   SELECT SUM(line.quantity * line.unit_price - line.discount_amount)
                   FROM order_lines AS line
                   WHERE line.order_id = o.order_id AND line.is_eligible = TRUE
               ), 0) ELSE o.total_amount END AS eligible_spend,
               COALESCE((
                   SELECT SUM(r.refund_amount) FROM order_returns AS r
                   WHERE r.order_id = o.order_id AND r.return_status = 'COMPLETED'
               ), 0) AS refunds
        FROM orders AS o
        WHERE o.order_status IN ('COMPLETED', 'RETURNED')
          AND (EXISTS (SELECT 1 FROM points_transactions AS tx
                       WHERE tx.order_id = o.order_id
                         AND tx.transaction_type = 'ACCRUAL')
               OR EXISTS (SELECT 1 FROM audit_events AS event
                          WHERE event.entity_type = 'order'
                            AND event.entity_id = o.order_id
                            AND event.event_type = 'QUALIFYING_SPEND_RECORDED'))
    ) AS facts
    GROUP BY facts.customer_id
) AS spend ON spend.customer_id = account.customer_id
SET account.qualifying_spend = COALESCE(spend.qualifying_spend, 0),
    account.tier_id = (
        SELECT tier.tier_id FROM tiers AS tier
        WHERE tier.minimum_spend <= COALESCE(spend.qualifying_spend, 0)
        ORDER BY tier.minimum_spend DESC, tier.tier_id
        LIMIT 1
    );

-- Older accruals deliberately keep NULL rule metadata: v2 did not record the
-- applied tier/rate, so the historical rule cannot be reconstructed reliably.
