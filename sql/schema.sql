CREATE DATABASE IF NOT EXISTS loyalty_ledger
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_0900_ai_ci;
USE loyalty_ledger;

CREATE TABLE IF NOT EXISTS tiers (
    tier_id INT PRIMARY KEY,
    tier_name VARCHAR(30) NOT NULL UNIQUE,
    minimum_spend DECIMAL(12,2) NOT NULL DEFAULT 0,
    points_per_100 DECIMAL(6,2) NOT NULL,
    CHECK (minimum_spend >= 0),
    CHECK (points_per_100 >= 0)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS customers (
    customer_id INT PRIMARY KEY,
    customer_code VARCHAR(20) NOT NULL UNIQUE,
    full_name VARCHAR(100) NOT NULL,
    email VARCHAR(150) NOT NULL UNIQUE,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS loyalty_accounts (
    account_id INT PRIMARY KEY,
    customer_id INT NOT NULL UNIQUE,
    tier_id INT,
    current_points INT NOT NULL DEFAULT 0,
    qualifying_spend DECIMAL(12,2) NOT NULL DEFAULT 0,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    CONSTRAINT fk_account_customer FOREIGN KEY (customer_id)
        REFERENCES customers(customer_id),
    CONSTRAINT fk_account_tier FOREIGN KEY (tier_id)
        REFERENCES tiers(tier_id),
    CHECK (qualifying_spend >= 0)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS account_tier_history (
    history_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    customer_id INT NOT NULL,
    old_tier_id INT,
    new_tier_id INT NOT NULL,
    qualifying_spend DECIMAL(12,2) NOT NULL,
    changed_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id),
    FOREIGN KEY (old_tier_id) REFERENCES tiers(tier_id),
    FOREIGN KEY (new_tier_id) REFERENCES tiers(tier_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS rules (
    rule_id INT PRIMARY KEY,
    tier_id INT NOT NULL,
    rule_name VARCHAR(100) NOT NULL,
    points_per_100 DECIMAL(6,2) NOT NULL,
    effective_from DATE NOT NULL,
    effective_to DATE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    FOREIGN KEY (tier_id) REFERENCES tiers(tier_id),
    UNIQUE (tier_id, effective_from),
    CHECK (points_per_100 >= 0),
    CHECK (effective_to IS NULL OR effective_to >= effective_from)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS orders (
    order_id VARCHAR(20) PRIMARY KEY,
    customer_id INT NOT NULL,
    order_date DATETIME(6) NOT NULL,
    total_amount DECIMAL(12,2) NOT NULL,
    order_status ENUM('PLACED','COMPLETED','RETURNED','CANCELLED')
        NOT NULL DEFAULT 'PLACED',
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id),
    CHECK (total_amount >= 0),
    INDEX idx_orders_customer_date (customer_id, order_date)
) ENGINE=InnoDB;

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

CREATE TABLE IF NOT EXISTS id_sequences (
    sequence_name VARCHAR(30) PRIMARY KEY,
    sequence_value BIGINT NOT NULL DEFAULT 0,
    CHECK (sequence_value >= 0)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS points_transactions (
    transaction_id VARCHAR(20) PRIMARY KEY,
    customer_id INT NOT NULL,
    order_id VARCHAR(20),
    transaction_type ENUM('ACCRUAL','REDEMPTION','EXPIRY','REVERSAL','ADJUSTMENT')
        NOT NULL,
    points_change INT NOT NULL,
    transaction_date DATETIME(6) NOT NULL,
    description VARCHAR(255),
    idempotency_key VARCHAR(100) UNIQUE,
    related_transaction_id VARCHAR(20),
    rule_id INT,
    earning_rate DECIMAL(6,2),
    eligible_spend DECIMAL(12,2),
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id),
    FOREIGN KEY (order_id) REFERENCES orders(order_id),
    FOREIGN KEY (rule_id) REFERENCES rules(rule_id),
    FOREIGN KEY (related_transaction_id)
        REFERENCES points_transactions(transaction_id),
    CHECK (points_change <> 0),
    INDEX idx_points_customer_date (customer_id, transaction_date)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS point_lots (
    lot_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    customer_id INT NOT NULL,
    source_transaction_id VARCHAR(20) NOT NULL UNIQUE,
    points_earned INT NOT NULL,
    points_remaining INT NOT NULL,
    earned_at DATETIME(6) NOT NULL,
    expires_at DATETIME(6),
    lot_status ENUM('ACTIVE','PARTIALLY_USED','EXHAUSTED','EXPIRED','REVERSED')
        NOT NULL DEFAULT 'ACTIVE',
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id),
    FOREIGN KEY (source_transaction_id)
        REFERENCES points_transactions(transaction_id),
    CHECK (points_earned > 0),
    CHECK (points_remaining >= 0),
    CHECK (points_remaining <= points_earned),
    INDEX idx_lots_customer_expiry (customer_id, expires_at, lot_status)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS redemptions (
    redemption_id VARCHAR(20) PRIMARY KEY,
    customer_id INT NOT NULL,
    reward_id INT,
    transaction_id VARCHAR(20) NOT NULL UNIQUE,
    points_redeemed INT NOT NULL,
    redemption_date DATETIME(6) NOT NULL,
    redemption_status ENUM('COMPLETED','CANCELLED','REVERSED')
        NOT NULL DEFAULT 'COMPLETED',
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id),
    FOREIGN KEY (reward_id) REFERENCES rewards(reward_id),
    FOREIGN KEY (transaction_id) REFERENCES points_transactions(transaction_id),
    CHECK (points_redeemed > 0)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS lot_consumptions (
    redemption_id VARCHAR(20) NOT NULL,
    lot_id BIGINT NOT NULL,
    points_used INT NOT NULL,
    PRIMARY KEY (redemption_id, lot_id),
    FOREIGN KEY (redemption_id) REFERENCES redemptions(redemption_id),
    FOREIGN KEY (lot_id) REFERENCES point_lots(lot_id),
    CHECK (points_used > 0)
) ENGINE=InnoDB;

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

CREATE TABLE IF NOT EXISTS order_returns (
    return_id VARCHAR(20) PRIMARY KEY,
    order_id VARCHAR(20) NOT NULL,
    return_date DATETIME(6) NOT NULL,
    refund_amount DECIMAL(12,2) NOT NULL,
    return_status ENUM('REQUESTED','COMPLETED','REJECTED')
        NOT NULL DEFAULT 'REQUESTED',
    FOREIGN KEY (order_id) REFERENCES orders(order_id),
    CHECK (refund_amount >= 0)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS expiry_batches (
    expiry_batch_id VARCHAR(20) PRIMARY KEY,
    expiry_date DATE NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    batch_status ENUM('PENDING','COMPLETED','FAILED') NOT NULL DEFAULT 'PENDING'
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS expiry_batch_details (
    expiry_detail_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    expiry_batch_id VARCHAR(20) NOT NULL,
    lot_id BIGINT NOT NULL,
    transaction_id VARCHAR(20) NOT NULL UNIQUE,
    points_expired INT NOT NULL,
    FOREIGN KEY (expiry_batch_id) REFERENCES expiry_batches(expiry_batch_id),
    FOREIGN KEY (lot_id) REFERENCES point_lots(lot_id),
    FOREIGN KEY (transaction_id) REFERENCES points_transactions(transaction_id),
    UNIQUE (expiry_batch_id, lot_id),
    CHECK (points_expired > 0)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS audit_events (
    audit_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    entity_type VARCHAR(50) NOT NULL,
    entity_id VARCHAR(50) NOT NULL,
    event_type VARCHAR(50) NOT NULL,
    event_details JSON,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    INDEX idx_audit_entity (entity_type, entity_id, created_at)
) ENGINE=InnoDB;
