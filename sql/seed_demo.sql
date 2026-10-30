USE loyalty_ledger;

INSERT INTO tiers (tier_id, tier_name, minimum_spend, points_per_100) VALUES
    (1, 'Basic', 0, 1.00),
    (2, 'Silver', 5000, 1.50),
    (3, 'Gold', 15000, 2.00)
ON DUPLICATE KEY UPDATE
    tier_name = VALUES(tier_name),
    minimum_spend = VALUES(minimum_spend),
    points_per_100 = VALUES(points_per_100);

INSERT INTO rules
    (rule_id, tier_id, rule_name, points_per_100, effective_from, is_active)
VALUES
    (1, 1, 'Basic earning rule', 1.00, '2026-01-01', TRUE),
    (2, 2, 'Silver earning rule', 1.50, '2026-01-01', TRUE),
    (3, 3, 'Gold earning rule', 2.00, '2026-01-01', TRUE)
ON DUPLICATE KEY UPDATE
    tier_id = VALUES(tier_id),
    rule_name = VALUES(rule_name),
    points_per_100 = VALUES(points_per_100),
    effective_from = VALUES(effective_from),
    is_active = VALUES(is_active);

INSERT INTO rewards
    (reward_id, reward_name, points_cost, is_active, effective_from)
VALUES
    (1, '₹50 store voucher', 500, TRUE, '2026-01-01'),
    (2, '₹100 store voucher', 900, TRUE, '2026-01-01')
ON DUPLICATE KEY UPDATE
    reward_name = VALUES(reward_name),
    points_cost = VALUES(points_cost),
    is_active = VALUES(is_active),
    effective_from = VALUES(effective_from);

INSERT INTO customers (customer_id, customer_code, full_name, email) VALUES
    (1, 'LOY-000001', 'Aarav Mehta', 'aarav@example.test'),
    (2, 'LOY-000002', 'Isha Rao', 'isha@example.test'),
    (3, 'LOY-000003', 'Kabir Shah', 'kabir@example.test')
ON DUPLICATE KEY UPDATE
    customer_code = VALUES(customer_code),
    full_name = VALUES(full_name), email = VALUES(email);

INSERT INTO loyalty_accounts (account_id, customer_id, tier_id) VALUES
    (1, 1, 1), (2, 2, 1), (3, 3, 1)
ON DUPLICATE KEY UPDATE customer_id = VALUES(customer_id);

INSERT INTO orders (order_id, customer_id, order_date, total_amount, order_status) VALUES
    ('ORD-2026-000001', 1, '2026-01-10 10:00:00', 500.00, 'COMPLETED'),
    ('ORD-2026-000002', 2, '2026-01-11 11:00:00', 1200.00, 'COMPLETED'),
    ('ORD-2026-000003', 3, '2026-01-12 12:00:00', 250.00, 'CANCELLED')
ON DUPLICATE KEY UPDATE
    customer_id = VALUES(customer_id), total_amount = VALUES(total_amount),
    order_status = VALUES(order_status);

INSERT INTO order_lines
    (order_id, sku, quantity, unit_price, discount_amount, is_eligible)
SELECT 'ORD-2026-000001', 'DEMO-COFFEE', 1, 500.00, 0, TRUE
WHERE NOT EXISTS (
    SELECT 1 FROM order_lines WHERE order_id = 'ORD-2026-000001'
);
INSERT INTO order_lines
    (order_id, sku, quantity, unit_price, discount_amount, is_eligible)
SELECT 'ORD-2026-000002', 'DEMO-TEA', 2, 600.00, 0, TRUE
WHERE NOT EXISTS (
    SELECT 1 FROM order_lines WHERE order_id = 'ORD-2026-000002'
);
INSERT INTO order_lines
    (order_id, sku, quantity, unit_price, discount_amount, is_eligible)
SELECT 'ORD-2026-000003', 'DEMO-COOKIE', 1, 250.00, 0, TRUE
WHERE NOT EXISTS (
    SELECT 1 FROM order_lines WHERE order_id = 'ORD-2026-000003'
);
