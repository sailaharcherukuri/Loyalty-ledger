-- Run from the project root with MySQL LOCAL INFILE enabled.
-- LOAD DATA paths below point to this dataset's output directory.
LOAD DATA LOCAL INFILE 'data/generated_small/tiers.csv'
INTO TABLE `tiers` CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' ENCLOSED BY '"' ESCAPED BY '"'
LINES TERMINATED BY '\n' IGNORE 1 LINES
(@v_tier_id, @v_tier_name, @v_minimum_spend, @v_points_per_100)
SET `tier_id` = NULLIF(@v_tier_id, ''), `tier_name` = NULLIF(@v_tier_name, ''), `minimum_spend` = NULLIF(@v_minimum_spend, ''), `points_per_100` = NULLIF(@v_points_per_100, '');
LOAD DATA LOCAL INFILE 'data/generated_small/customers.csv'
INTO TABLE `customers` CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' ENCLOSED BY '"' ESCAPED BY '"'
LINES TERMINATED BY '\n' IGNORE 1 LINES
(@v_customer_id, @v_customer_code, @v_full_name, @v_email, @v_created_at)
SET `customer_id` = NULLIF(@v_customer_id, ''), `customer_code` = NULLIF(@v_customer_code, ''), `full_name` = NULLIF(@v_full_name, ''), `email` = NULLIF(@v_email, ''), `created_at` = NULLIF(@v_created_at, '');
LOAD DATA LOCAL INFILE 'data/generated_small/rewards.csv'
INTO TABLE `rewards` CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' ENCLOSED BY '"' ESCAPED BY '"'
LINES TERMINATED BY '\n' IGNORE 1 LINES
(@v_reward_id, @v_reward_name, @v_points_cost, @v_is_active, @v_effective_from, @v_effective_to)
SET `reward_id` = NULLIF(@v_reward_id, ''), `reward_name` = NULLIF(@v_reward_name, ''), `points_cost` = NULLIF(@v_points_cost, ''), `is_active` = NULLIF(@v_is_active, ''), `effective_from` = NULLIF(@v_effective_from, ''), `effective_to` = NULLIF(@v_effective_to, '');
LOAD DATA LOCAL INFILE 'data/generated_small/rules.csv'
INTO TABLE `rules` CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' ENCLOSED BY '"' ESCAPED BY '"'
LINES TERMINATED BY '\n' IGNORE 1 LINES
(@v_rule_id, @v_tier_id, @v_rule_name, @v_points_per_100, @v_effective_from, @v_effective_to, @v_is_active)
SET `rule_id` = NULLIF(@v_rule_id, ''), `tier_id` = NULLIF(@v_tier_id, ''), `rule_name` = NULLIF(@v_rule_name, ''), `points_per_100` = NULLIF(@v_points_per_100, ''), `effective_from` = NULLIF(@v_effective_from, ''), `effective_to` = NULLIF(@v_effective_to, ''), `is_active` = NULLIF(@v_is_active, '');
LOAD DATA LOCAL INFILE 'data/generated_small/loyalty_accounts.csv'
INTO TABLE `loyalty_accounts` CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' ENCLOSED BY '"' ESCAPED BY '"'
LINES TERMINATED BY '\n' IGNORE 1 LINES
(@v_account_id, @v_customer_id, @v_tier_id, @v_current_points, @v_qualifying_spend, @v_created_at)
SET `account_id` = NULLIF(@v_account_id, ''), `customer_id` = NULLIF(@v_customer_id, ''), `tier_id` = NULLIF(@v_tier_id, ''), `current_points` = NULLIF(@v_current_points, ''), `qualifying_spend` = NULLIF(@v_qualifying_spend, ''), `created_at` = NULLIF(@v_created_at, '');
LOAD DATA LOCAL INFILE 'data/generated_small/orders.csv'
INTO TABLE `orders` CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' ENCLOSED BY '"' ESCAPED BY '"'
LINES TERMINATED BY '\n' IGNORE 1 LINES
(@v_order_id, @v_customer_id, @v_order_date, @v_total_amount, @v_order_status)
SET `order_id` = NULLIF(@v_order_id, ''), `customer_id` = NULLIF(@v_customer_id, ''), `order_date` = NULLIF(@v_order_date, ''), `total_amount` = NULLIF(@v_total_amount, ''), `order_status` = NULLIF(@v_order_status, '');
LOAD DATA LOCAL INFILE 'data/generated_small/order_lines.csv'
INTO TABLE `order_lines` CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' ENCLOSED BY '"' ESCAPED BY '"'
LINES TERMINATED BY '\n' IGNORE 1 LINES
(@v_order_line_id, @v_order_id, @v_sku, @v_quantity, @v_unit_price, @v_discount_amount, @v_is_eligible)
SET `order_line_id` = NULLIF(@v_order_line_id, ''), `order_id` = NULLIF(@v_order_id, ''), `sku` = NULLIF(@v_sku, ''), `quantity` = NULLIF(@v_quantity, ''), `unit_price` = NULLIF(@v_unit_price, ''), `discount_amount` = NULLIF(@v_discount_amount, ''), `is_eligible` = NULLIF(@v_is_eligible, '');
LOAD DATA LOCAL INFILE 'data/generated_small/points_transactions.csv'
INTO TABLE `points_transactions` CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' ENCLOSED BY '"' ESCAPED BY '"'
LINES TERMINATED BY '\n' IGNORE 1 LINES
(@v_transaction_id, @v_customer_id, @v_order_id, @v_transaction_type, @v_points_change, @v_transaction_date, @v_description, @v_idempotency_key, @v_related_transaction_id, @v_rule_id, @v_earning_rate, @v_eligible_spend)
SET `transaction_id` = NULLIF(@v_transaction_id, ''), `customer_id` = NULLIF(@v_customer_id, ''), `order_id` = NULLIF(@v_order_id, ''), `transaction_type` = NULLIF(@v_transaction_type, ''), `points_change` = NULLIF(@v_points_change, ''), `transaction_date` = NULLIF(@v_transaction_date, ''), `description` = NULLIF(@v_description, ''), `idempotency_key` = NULLIF(@v_idempotency_key, ''), `related_transaction_id` = NULLIF(@v_related_transaction_id, ''), `rule_id` = NULLIF(@v_rule_id, ''), `earning_rate` = NULLIF(@v_earning_rate, ''), `eligible_spend` = NULLIF(@v_eligible_spend, '');
LOAD DATA LOCAL INFILE 'data/generated_small/point_lots.csv'
INTO TABLE `point_lots` CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' ENCLOSED BY '"' ESCAPED BY '"'
LINES TERMINATED BY '\n' IGNORE 1 LINES
(@v_lot_id, @v_customer_id, @v_source_transaction_id, @v_points_earned, @v_points_remaining, @v_earned_at, @v_expires_at, @v_lot_status)
SET `lot_id` = NULLIF(@v_lot_id, ''), `customer_id` = NULLIF(@v_customer_id, ''), `source_transaction_id` = NULLIF(@v_source_transaction_id, ''), `points_earned` = NULLIF(@v_points_earned, ''), `points_remaining` = NULLIF(@v_points_remaining, ''), `earned_at` = NULLIF(@v_earned_at, ''), `expires_at` = NULLIF(@v_expires_at, ''), `lot_status` = NULLIF(@v_lot_status, '');
LOAD DATA LOCAL INFILE 'data/generated_small/redemptions.csv'
INTO TABLE `redemptions` CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' ENCLOSED BY '"' ESCAPED BY '"'
LINES TERMINATED BY '\n' IGNORE 1 LINES
(@v_redemption_id, @v_customer_id, @v_reward_id, @v_transaction_id, @v_points_redeemed, @v_redemption_date, @v_redemption_status)
SET `redemption_id` = NULLIF(@v_redemption_id, ''), `customer_id` = NULLIF(@v_customer_id, ''), `reward_id` = NULLIF(@v_reward_id, ''), `transaction_id` = NULLIF(@v_transaction_id, ''), `points_redeemed` = NULLIF(@v_points_redeemed, ''), `redemption_date` = NULLIF(@v_redemption_date, ''), `redemption_status` = NULLIF(@v_redemption_status, '');
LOAD DATA LOCAL INFILE 'data/generated_small/lot_consumptions.csv'
INTO TABLE `lot_consumptions` CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' ENCLOSED BY '"' ESCAPED BY '"'
LINES TERMINATED BY '\n' IGNORE 1 LINES
(@v_redemption_id, @v_lot_id, @v_points_used)
SET `redemption_id` = NULLIF(@v_redemption_id, ''), `lot_id` = NULLIF(@v_lot_id, ''), `points_used` = NULLIF(@v_points_used, '');
LOAD DATA LOCAL INFILE 'data/generated_small/reversal_lot_allocations.csv'
INTO TABLE `reversal_lot_allocations` CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' ENCLOSED BY '"' ESCAPED BY '"'
LINES TERMINATED BY '\n' IGNORE 1 LINES
(@v_reversal_transaction_id, @v_lot_id, @v_points_removed)
SET `reversal_transaction_id` = NULLIF(@v_reversal_transaction_id, ''), `lot_id` = NULLIF(@v_lot_id, ''), `points_removed` = NULLIF(@v_points_removed, '');
LOAD DATA LOCAL INFILE 'data/generated_small/order_returns.csv'
INTO TABLE `order_returns` CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' ENCLOSED BY '"' ESCAPED BY '"'
LINES TERMINATED BY '\n' IGNORE 1 LINES
(@v_return_id, @v_order_id, @v_return_date, @v_refund_amount, @v_return_status)
SET `return_id` = NULLIF(@v_return_id, ''), `order_id` = NULLIF(@v_order_id, ''), `return_date` = NULLIF(@v_return_date, ''), `refund_amount` = NULLIF(@v_refund_amount, ''), `return_status` = NULLIF(@v_return_status, '');
LOAD DATA LOCAL INFILE 'data/generated_small/expiry_batches.csv'
INTO TABLE `expiry_batches` CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' ENCLOSED BY '"' ESCAPED BY '"'
LINES TERMINATED BY '\n' IGNORE 1 LINES
(@v_expiry_batch_id, @v_expiry_date, @v_created_at, @v_batch_status)
SET `expiry_batch_id` = NULLIF(@v_expiry_batch_id, ''), `expiry_date` = NULLIF(@v_expiry_date, ''), `created_at` = NULLIF(@v_created_at, ''), `batch_status` = NULLIF(@v_batch_status, '');
LOAD DATA LOCAL INFILE 'data/generated_small/expiry_batch_details.csv'
INTO TABLE `expiry_batch_details` CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' ENCLOSED BY '"' ESCAPED BY '"'
LINES TERMINATED BY '\n' IGNORE 1 LINES
(@v_expiry_detail_id, @v_expiry_batch_id, @v_lot_id, @v_transaction_id, @v_points_expired)
SET `expiry_detail_id` = NULLIF(@v_expiry_detail_id, ''), `expiry_batch_id` = NULLIF(@v_expiry_batch_id, ''), `lot_id` = NULLIF(@v_lot_id, ''), `transaction_id` = NULLIF(@v_transaction_id, ''), `points_expired` = NULLIF(@v_points_expired, '');
LOAD DATA LOCAL INFILE 'data/generated_small/account_tier_history.csv'
INTO TABLE `account_tier_history` CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' ENCLOSED BY '"' ESCAPED BY '"'
LINES TERMINATED BY '\n' IGNORE 1 LINES
(@v_history_id, @v_customer_id, @v_old_tier_id, @v_new_tier_id, @v_qualifying_spend, @v_changed_at)
SET `history_id` = NULLIF(@v_history_id, ''), `customer_id` = NULLIF(@v_customer_id, ''), `old_tier_id` = NULLIF(@v_old_tier_id, ''), `new_tier_id` = NULLIF(@v_new_tier_id, ''), `qualifying_spend` = NULLIF(@v_qualifying_spend, ''), `changed_at` = NULLIF(@v_changed_at, '');
LOAD DATA LOCAL INFILE 'data/generated_small/audit_events.csv'
INTO TABLE `audit_events` CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' ENCLOSED BY '"' ESCAPED BY '"'
LINES TERMINATED BY '\n' IGNORE 1 LINES
(@v_entity_type, @v_entity_id, @v_event_type, @v_event_details, @v_created_at)
SET `entity_type` = NULLIF(@v_entity_type, ''), `entity_id` = NULLIF(@v_entity_id, ''), `event_type` = NULLIF(@v_event_type, ''), `event_details` = NULLIF(@v_event_details, ''), `created_at` = NULLIF(@v_created_at, '');
LOAD DATA LOCAL INFILE 'data/generated_small/id_sequences.csv'
INTO TABLE `id_sequences` CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' ENCLOSED BY '"' ESCAPED BY '"'
LINES TERMINATED BY '\n' IGNORE 1 LINES
(@v_sequence_name, @v_sequence_value)
SET `sequence_name` = NULLIF(@v_sequence_name, ''), `sequence_value` = NULLIF(@v_sequence_value, '');
