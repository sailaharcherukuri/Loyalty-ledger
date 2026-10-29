# Loyalty Ledger data dictionary

All monetary amounts are INR with two decimal places. Dates are local business dates; timestamps use `DATETIME`. IDs are immutable. Generated emails use the reserved `example.test` domain. MySQL foreign keys and checks enforce relational and arithmetic constraints where possible.

## Core entities

### Customer (`customers`)

| Field | Type | Req. | Rules |
|---|---|---:|---|
| `customer_id` | INT | Yes | Primary key; immutable positive identifier. |
| `customer_code` | VARCHAR(20) | Yes | Unique business ID; format `LOY-000001`. |
| `full_name` | VARCHAR(100) | Yes | Display name; non-empty. |
| `email` | VARCHAR(150) | Yes | Unique contact key; synthetic data uses `example.test`. |
| `created_at` | DATETIME | Yes | Defaults to creation time. |

### Loyalty account (`loyalty_accounts`)

| Field | Type | Req. | Rules |
|---|---|---:|---|
| `account_id` | INT | Yes | Primary key. |
| `customer_id` | INT | Yes | Unique foreign key to one customer; enforces 1:1. |
| `tier_id` | INT | No | Foreign key to the currently earned tier. |
| `current_points` | INT | Yes | Cached net balance; equals the sum of ledger `points_change`. Can be negative when a return reverses points already spent. |
| `qualifying_spend` | DECIMAL(12,2) | Yes | INR net qualifying spend after completed refunds; cannot be negative. |
| `created_at` | DATETIME | Yes | Account creation time. |

### Tier (`tiers`)

| Field | Type | Req. | Rules |
|---|---|---:|---|
| `tier_id` | INT | Yes | Primary key. |
| `tier_name` | VARCHAR(30) | Yes | Unique name, such as Basic, Silver, or Gold. |
| `minimum_spend` | DECIMAL(12,2) | Yes | INR spend threshold; at least zero. |
| `points_per_100` | DECIMAL(6,2) | Yes | Whole points earned per ₹100 of eligible net spend; at least zero. |

### Earning rule (`rules`)

| Field | Type | Req. | Rules |
|---|---|---:|---|
| `rule_id` | INT | Yes | Primary key. |
| `rule_name` | VARCHAR(100) | Yes | Human-readable rule name. |
| `points_per_100` | DECIMAL(6,2) | Yes | Non-negative earning rate. |
| `effective_from` | DATE | Yes | First date the rule applies. |
| `effective_to` | DATE | No | Last applicable date; not earlier than `effective_from`. |
| `is_active` | BOOLEAN | Yes | Operational enable/disable flag. |

### Order (`orders`)

| Field | Type | Req. | Rules |
|---|---|---:|---|
| `order_id` | VARCHAR(20) | Yes | Primary key; format `ORD-2026-000001`. |
| `customer_id` | INT | Yes | Foreign key to the buyer. |
| `order_date` | DATETIME | Yes | Must precede associated returns and accrual events. |
| `total_amount` | DECIMAL(12,2) | Yes | INR; at least zero. |
| `order_status` | ENUM | Yes | `PLACED`, `COMPLETED`, `RETURNED`, or `CANCELLED`; only completed orders earn points. |

### Order line (`order_lines`)

| Field | Type | Req. | Rules |
|---|---|---:|---|
| `order_line_id` | BIGINT | Yes | Auto-increment primary key. |
| `order_id` | VARCHAR(20) | Yes | Foreign key; one order has one or more lines. |
| `sku` | VARCHAR(40) | Yes | Product identifier. |
| `quantity` | INT | Yes | Positive units. |
| `unit_price` | DECIMAL(12,2) | Yes | INR per unit; non-negative. |
| `discount_amount` | DECIMAL(12,2) | Yes | INR line discount; between zero and line gross amount. |
| `is_eligible` | BOOLEAN | Yes | Only eligible net line spend earns points. |

### Points transaction (`points_transactions`)

| Field | Type | Req. | Rules |
|---|---|---:|---|
| `transaction_id` | VARCHAR(20) | Yes | Primary key; format `PTS-2026-00000001`. |
| `customer_id` | INT | Yes | Foreign key to the account owner. |
| `order_id` | VARCHAR(20) | No | Optional foreign key for order accruals and return reversals. |
| `transaction_type` | ENUM | Yes | `ACCRUAL`, `REDEMPTION`, `EXPIRY`, `REVERSAL`, or `ADJUSTMENT`. |
| `points_change` | INT | Yes | Signed; positive adds points, negative removes points, never zero. |
| `transaction_date` | DATETIME | Yes | Time the event was committed. |
| `description` | VARCHAR(255) | No | Human-readable explanation. |
| `idempotency_key` | VARCHAR(100) | No | Unique event key; prevents duplicate operations. |
| `related_transaction_id` | VARCHAR(20) | No | Self-reference to an accrual for its reversal or expiry. |

### Reward (`rewards`)

| Field | Type | Req. | Rules |
|---|---|---:|---|
| `reward_id` | INT | Yes | Primary key. |
| `reward_name` | VARCHAR(100) | Yes | Display name. |
| `points_cost` | INT | Yes | Positive whole-point price. |
| `is_active` | BOOLEAN | Yes | Must be active to redeem. |
| `effective_from` | DATE | Yes | First available date. |
| `effective_to` | DATE | No | Optional final availability date, not before start. |

### Redemption (`redemptions`)

| Field | Type | Req. | Rules |
|---|---|---:|---|
| `redemption_id` | VARCHAR(20) | Yes | Primary key; format `RDM-2026-000001`. |
| `customer_id` | INT | Yes | Foreign key to the account. |
| `reward_id` | INT | No | Optional foreign key when a named reward is selected. |
| `transaction_id` | VARCHAR(20) | Yes | Unique foreign key to its negative ledger transaction. |
| `points_redeemed` | INT | Yes | Positive amount; the ledger entry is its negative. |
| `redemption_date` | DATETIME | Yes | Commit time. |
| `redemption_status` | ENUM | Yes | `COMPLETED`, `CANCELLED`, or `REVERSED`. |

### Expiry batch (`expiry_batches`)

| Field | Type | Req. | Rules |
|---|---|---:|---|
| `expiry_batch_id` | VARCHAR(20) | Yes | Primary key; format `EXP-2026-000001`. |
| `expiry_date` | DATE | Yes | Business date for the run. |
| `created_at` | DATETIME | Yes | Batch creation time. |
| `batch_status` | ENUM | Yes | `PENDING`, `COMPLETED`, or `FAILED`. |

## Supporting event and bridge tables

- `point_lots`: one lot per accrual; `points_earned` is the original amount, `points_remaining` is the unspent amount, and `expires_at` controls eligibility. States are `ACTIVE`, `PARTIALLY_USED`, `EXHAUSTED`, `EXPIRED`, and `REVERSED`.
- `lot_consumptions`: many-to-many bridge between redemptions and point lots; `points_used` is positive. A redemption may consume several lots; a lot may fund several redemptions.
- `order_returns`: each return references an order and has a non-negative refund plus `REQUESTED`, `COMPLETED`, or `REJECTED` state. Multiple partial returns are allowed while their completed sum stays within the order total.
- `expiry_batch_details`: one row per lot expired in a batch, unique by `(expiry_batch_id, lot_id)`, linked to its negative ledger transaction.
- `account_tier_history`: append-only tier transitions with the qualifying spend used for the decision.
- `audit_events`: timestamped event details for operational review; it does not replace the points ledger.
- `id_sequences`: atomic counters used to allocate collision-safe formatted business IDs.

## Relationships and lifecycle

- Customer to loyalty account: 1:1. Customer to orders, account history, and points transactions: 1:N.
- Order to order lines and returns: 1:N. Completed order to accrual: 0:1. Return reversals link to the original accrual transaction.
- Accrual transaction to point lot: 1:1. Redemption to point lots: N:M through `lot_consumptions`.
- Expiry batch to point lots: 1:N through `expiry_batch_details`; each lot can be expired only once because its remaining points become zero.
- Business rows use foreign keys; transaction operations use one database transaction so the ledger, cached balance, lot state, and audit record commit or roll back together.
