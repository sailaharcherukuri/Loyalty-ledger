-- Upgrade the original starter schema in place. Run once against the existing
-- loyalty_ledger database after taking a backup. Requires MySQL 8.0.29+.
USE loyalty_ledger;

CREATE TABLE IF NOT EXISTS id_sequences (
    sequence_name VARCHAR(30) PRIMARY KEY,
    sequence_value BIGINT NOT NULL DEFAULT 0,
    CHECK (sequence_value >= 0)
) ENGINE=InnoDB;

ALTER TABLE customers
    ADD COLUMN customer_code VARCHAR(20) NULL;
UPDATE customers
SET customer_code = CONCAT('LOY-', LPAD(customer_id, 6, '0'))
WHERE customer_code IS NULL;
ALTER TABLE customers
    MODIFY COLUMN customer_code VARCHAR(20) NOT NULL,
    ADD UNIQUE INDEX uq_customer_code (customer_code);

ALTER TABLE loyalty_accounts
    ADD COLUMN tier_id INT NULL,
    ADD COLUMN qualifying_spend DECIMAL(12,2) NOT NULL DEFAULT 0;

SET @balance_check := (
    SELECT CONSTRAINT_NAME
    FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE()
      AND TABLE_NAME = 'loyalty_accounts'
      AND CONSTRAINT_TYPE = 'CHECK'
    LIMIT 1
);
SET @drop_balance_check := IF(
    @balance_check IS NULL,
    'SELECT 1',
    CONCAT('ALTER TABLE loyalty_accounts DROP CHECK `', @balance_check, '`')
);
PREPARE drop_check_stmt FROM @drop_balance_check;
EXECUTE drop_check_stmt;
DEALLOCATE PREPARE drop_check_stmt;

ALTER TABLE loyalty_accounts
    ADD CONSTRAINT fk_account_tier FOREIGN KEY (tier_id) REFERENCES tiers(tier_id);

CREATE TABLE IF NOT EXISTS account_tier_history (
    history_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    customer_id INT NOT NULL,
    old_tier_id INT,
    new_tier_id INT NOT NULL,
    qualifying_spend DECIMAL(12,2) NOT NULL,
    changed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id),
    FOREIGN KEY (old_tier_id) REFERENCES tiers(tier_id),
    FOREIGN KEY (new_tier_id) REFERENCES tiers(tier_id)
) ENGINE=InnoDB;

ALTER TABLE rules
    ADD COLUMN effective_from DATE NOT NULL DEFAULT '2026-01-01',
    ADD COLUMN effective_to DATE NULL;

CREATE TABLE IF NOT EXISTS order_lines (
    order_line_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    order_id VARCHAR(20) NOT NULL,
    sku VARCHAR(40) NOT NULL,
    quantity INT NOT NULL,
    unit_price DECIMAL(12,2) NOT NULL,
    discount_amount DECIMAL(12,2) NOT NULL DEFAULT 0,
    is_eligible BOOLEAN NOT NULL DEFAULT TRUE,
    FOREIGN KEY (order_id) REFERENCES orders(order_id),
    CHECK (quantity > 0),
    CHECK (unit_price >= 0),
    CHECK (discount_amount >= 0),
    CHECK (discount_amount <= quantity * unit_price)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS rewards (
    reward_id INT PRIMARY KEY,
    reward_name VARCHAR(100) NOT NULL,
    points_cost INT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    effective_from DATE NOT NULL,
    effective_to DATE,
    CHECK (points_cost > 0),
    CHECK (effective_to IS NULL OR effective_to >= effective_from)
) ENGINE=InnoDB;

ALTER TABLE points_transactions
    ADD COLUMN idempotency_key VARCHAR(100) NULL,
    ADD COLUMN related_transaction_id VARCHAR(20) NULL,
    ADD UNIQUE INDEX uq_points_idempotency (idempotency_key),
    ADD CONSTRAINT fk_points_related FOREIGN KEY (related_transaction_id)
        REFERENCES points_transactions(transaction_id);

ALTER TABLE point_lots
    MODIFY COLUMN lot_status ENUM(
        'ACTIVE','PARTIALLY_USED','EXHAUSTED','EXPIRED','REVERSED'
    ) NOT NULL DEFAULT 'ACTIVE',
    ADD UNIQUE INDEX uq_lot_source_transaction (source_transaction_id);

ALTER TABLE redemptions
    ADD COLUMN reward_id INT NULL,
    ADD COLUMN transaction_id VARCHAR(20) NULL,
    ADD UNIQUE INDEX uq_redemption_transaction (transaction_id),
    ADD CONSTRAINT fk_redemption_reward FOREIGN KEY (reward_id)
        REFERENCES rewards(reward_id),
    ADD CONSTRAINT fk_redemption_transaction FOREIGN KEY (transaction_id)
        REFERENCES points_transactions(transaction_id);

CREATE TABLE IF NOT EXISTS lot_consumptions (
    redemption_id VARCHAR(20) NOT NULL,
    lot_id BIGINT NOT NULL,
    points_used INT NOT NULL,
    PRIMARY KEY (redemption_id, lot_id),
    FOREIGN KEY (redemption_id) REFERENCES redemptions(redemption_id),
    FOREIGN KEY (lot_id) REFERENCES point_lots(lot_id),
    CHECK (points_used > 0)
) ENGINE=InnoDB;

ALTER TABLE expiry_batch_details
    ADD COLUMN transaction_id VARCHAR(20) NULL,
    ADD UNIQUE INDEX uq_expiry_transaction (transaction_id),
    ADD CONSTRAINT fk_expiry_transaction FOREIGN KEY (transaction_id)
        REFERENCES points_transactions(transaction_id);

-- Backfill historical accruals as lots. FIFO use consumes oldest lots first,
-- so any points still available at migration time remain in the newest lots.
CREATE TEMPORARY TABLE migration_point_lots AS
SELECT
    tx.customer_id,
    tx.transaction_id,
    tx.points_change AS points_earned,
    LEAST(
        tx.points_change,
        GREATEST(0, GREATEST(0, account.current_points) -
            COALESCE(SUM(tx.points_change) OVER (
                PARTITION BY tx.customer_id
                ORDER BY tx.transaction_date DESC, tx.transaction_id DESC
                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
            ), 0)
        )
    ) AS points_remaining,
    tx.transaction_date AS earned_at,
    DATE_ADD(tx.transaction_date, INTERVAL 365 DAY) AS expires_at
FROM points_transactions AS tx
JOIN loyalty_accounts AS account ON account.customer_id = tx.customer_id
WHERE tx.transaction_type = 'ACCRUAL' AND tx.points_change > 0;

INSERT INTO point_lots
    (customer_id, source_transaction_id, points_earned, points_remaining,
     earned_at, expires_at, lot_status)
SELECT customer_id, transaction_id, points_earned, points_remaining,
       earned_at, expires_at,
       CASE WHEN points_remaining = 0 THEN 'EXHAUSTED'
            WHEN points_remaining = points_earned THEN 'ACTIVE'
            ELSE 'PARTIALLY_USED' END
FROM migration_point_lots AS old_lot
WHERE NOT EXISTS (
    SELECT 1 FROM point_lots AS current_lot
    WHERE current_lot.source_transaction_id = old_lot.transaction_id
);
DROP TEMPORARY TABLE migration_point_lots;

-- Start counters after existing records to avoid reusing IDs from the starter.
INSERT INTO id_sequences (sequence_name, sequence_value)
SELECT 'points', COALESCE(MAX(CAST(SUBSTRING_INDEX(transaction_id, '-', -1) AS UNSIGNED)), 0)
FROM points_transactions
ON DUPLICATE KEY UPDATE sequence_value = GREATEST(sequence_value, VALUES(sequence_value));

INSERT INTO id_sequences (sequence_name, sequence_value)
SELECT 'redemptions', COALESCE(MAX(CAST(SUBSTRING_INDEX(redemption_id, '-', -1) AS UNSIGNED)), 0)
FROM redemptions
ON DUPLICATE KEY UPDATE sequence_value = GREATEST(sequence_value, VALUES(sequence_value));

INSERT INTO id_sequences (sequence_name, sequence_value)
SELECT 'expiry_batches', COALESCE(MAX(CAST(SUBSTRING_INDEX(expiry_batch_id, '-', -1) AS UNSIGNED)), 0)
FROM expiry_batches
ON DUPLICATE KEY UPDATE sequence_value = GREATEST(sequence_value, VALUES(sequence_value));
