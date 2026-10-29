-- Minimal, synthetic pre-v2 schema fixture for the migration integration test.
-- Contains no data copied from a user database.
CREATE TABLE tiers (
    tier_id INT PRIMARY KEY,
    tier_name VARCHAR(30) NOT NULL UNIQUE,
    minimum_spend DECIMAL(12,2) NOT NULL DEFAULT 0,
    points_per_100 DECIMAL(5,2) NOT NULL,
    CHECK (minimum_spend >= 0),
    CHECK (points_per_100 >= 0)
) ENGINE=InnoDB;

CREATE TABLE customers (
    customer_id INT PRIMARY KEY,
    full_name VARCHAR(100) NOT NULL,
    email VARCHAR(150) NOT NULL UNIQUE,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;

CREATE TABLE loyalty_accounts (
    account_id INT PRIMARY KEY,
    customer_id INT NOT NULL UNIQUE,
    current_points INT NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id),
    CHECK (current_points >= 0)
) ENGINE=InnoDB;

CREATE TABLE rules (
    rule_id INT PRIMARY KEY,
    rule_name VARCHAR(100) NOT NULL,
    points_per_100 DECIMAL(5,2) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    CHECK (points_per_100 >= 0)
) ENGINE=InnoDB;

CREATE TABLE orders (
    order_id VARCHAR(20) PRIMARY KEY,
    customer_id INT NOT NULL,
    order_date DATETIME NOT NULL,
    total_amount DECIMAL(12,2) NOT NULL,
    order_status ENUM('PLACED','COMPLETED','RETURNED','CANCELLED') NOT NULL DEFAULT 'PLACED',
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id),
    CHECK (total_amount >= 0)
) ENGINE=InnoDB;

CREATE TABLE points_transactions (
    transaction_id VARCHAR(20) PRIMARY KEY,
    customer_id INT NOT NULL,
    order_id VARCHAR(20),
    transaction_type ENUM('ACCRUAL','REDEMPTION','EXPIRY','REVERSAL','ADJUSTMENT') NOT NULL,
    points_change INT NOT NULL,
    transaction_date DATETIME NOT NULL,
    description VARCHAR(255),
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id),
    FOREIGN KEY (order_id) REFERENCES orders(order_id),
    CHECK (points_change <> 0)
) ENGINE=InnoDB;

CREATE TABLE point_lots (
    lot_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    customer_id INT NOT NULL,
    source_transaction_id VARCHAR(20) NOT NULL,
    points_earned INT NOT NULL,
    points_remaining INT NOT NULL,
    earned_at DATETIME NOT NULL,
    expires_at DATETIME,
    lot_status ENUM('ACTIVE','PARTIALLY_USED','EXHAUSTED','EXPIRED') NOT NULL DEFAULT 'ACTIVE',
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id),
    FOREIGN KEY (source_transaction_id) REFERENCES points_transactions(transaction_id),
    CHECK (points_earned > 0),
    CHECK (points_remaining >= 0),
    CHECK (points_remaining <= points_earned)
) ENGINE=InnoDB;

CREATE TABLE order_returns (
    return_id VARCHAR(20) PRIMARY KEY,
    order_id VARCHAR(20) NOT NULL,
    return_date DATETIME NOT NULL,
    refund_amount DECIMAL(12,2) NOT NULL,
    return_status ENUM('REQUESTED','COMPLETED','REJECTED') NOT NULL DEFAULT 'REQUESTED',
    FOREIGN KEY (order_id) REFERENCES orders(order_id),
    CHECK (refund_amount >= 0)
) ENGINE=InnoDB;

CREATE TABLE expiry_batches (
    expiry_batch_id VARCHAR(20) PRIMARY KEY,
    expiry_date DATE NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    batch_status ENUM('PENDING','COMPLETED','FAILED') NOT NULL DEFAULT 'PENDING'
) ENGINE=InnoDB;

CREATE TABLE expiry_batch_details (
    expiry_detail_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    expiry_batch_id VARCHAR(20) NOT NULL,
    lot_id BIGINT NOT NULL,
    points_expired INT NOT NULL,
    FOREIGN KEY (expiry_batch_id) REFERENCES expiry_batches(expiry_batch_id),
    FOREIGN KEY (lot_id) REFERENCES point_lots(lot_id),
    UNIQUE (expiry_batch_id, lot_id),
    CHECK (points_expired > 0)
) ENGINE=InnoDB;

CREATE TABLE redemptions (
    redemption_id VARCHAR(20) PRIMARY KEY,
    customer_id INT NOT NULL,
    points_redeemed INT NOT NULL,
    redemption_date DATETIME NOT NULL,
    redemption_status ENUM('COMPLETED','CANCELLED','REVERSED') NOT NULL DEFAULT 'COMPLETED',
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id),
    CHECK (points_redeemed > 0)
) ENGINE=InnoDB;

CREATE TABLE audit_events (
    audit_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    entity_type VARCHAR(50) NOT NULL,
    entity_id VARCHAR(50) NOT NULL,
    event_type VARCHAR(50) NOT NULL,
    event_details JSON,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;

INSERT INTO tiers (tier_id, tier_name, minimum_spend, points_per_100) VALUES
    (1, 'Basic', 0, 1.00),
    (2, 'Silver', 5000, 1.50),
    (3, 'Gold', 15000, 2.00);
INSERT INTO customers (customer_id, full_name, email)
VALUES (1, 'Legacy Test Customer', 'legacy@example.test');
INSERT INTO loyalty_accounts (account_id, customer_id, current_points)
VALUES (1, 1, 505);
INSERT INTO rules (rule_id, rule_name, points_per_100, is_active)
VALUES (1, 'Legacy base rule', 1.00, TRUE);
INSERT INTO orders (order_id, customer_id, order_date, total_amount, order_status)
VALUES ('ORD-2026-000001', 1, '2026-01-10 10:00:00', 500.00, 'COMPLETED');
INSERT INTO points_transactions
    (transaction_id, customer_id, order_id, transaction_type, points_change, transaction_date, description)
VALUES
    ('PTS-2026-00000001', 1, 'ORD-2026-000001', 'ACCRUAL', 5,
     '2026-01-10 10:00:00', 'Legacy accrual');
