"""Generate a deterministic, synthetic Loyalty Ledger dataset and labelled cases."""

import argparse
import csv
import hashlib
import json
import random
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SEED = 20261022
AS_OF = date(2026, 10, 28)
START_DATE = datetime(2024, 1, 1)

TIERS = [
    {"tier_id": 1, "tier_name": "Basic", "minimum_spend": "0.00", "points_per_100": "1.00"},
    {"tier_id": 2, "tier_name": "Silver", "minimum_spend": "25000.00", "points_per_100": "1.25"},
    {"tier_id": 3, "tier_name": "Gold", "minimum_spend": "75000.00", "points_per_100": "1.50"},
]

FIELDS = {
    "tiers": ["tier_id", "tier_name", "minimum_spend", "points_per_100"],
    "customers": ["customer_id", "customer_code", "full_name", "email", "created_at"],
    "loyalty_accounts": ["account_id", "customer_id", "tier_id", "current_points", "qualifying_spend", "created_at"],
    "account_tier_history": ["history_id", "customer_id", "old_tier_id", "new_tier_id", "qualifying_spend", "changed_at"],
    "rules": ["rule_id", "rule_name", "points_per_100", "effective_from", "effective_to", "is_active"],
    "orders": ["order_id", "customer_id", "order_date", "total_amount", "order_status"],
    "order_lines": ["order_line_id", "order_id", "sku", "quantity", "unit_price", "discount_amount", "is_eligible"],
    "rewards": ["reward_id", "reward_name", "points_cost", "is_active", "effective_from", "effective_to"],
    "id_sequences": ["sequence_name", "sequence_value"],
    "points_transactions": ["transaction_id", "customer_id", "order_id", "transaction_type", "points_change", "transaction_date", "description", "idempotency_key", "related_transaction_id"],
    "point_lots": ["lot_id", "customer_id", "source_transaction_id", "points_earned", "points_remaining", "earned_at", "expires_at", "lot_status"],
    "redemptions": ["redemption_id", "customer_id", "reward_id", "transaction_id", "points_redeemed", "redemption_date", "redemption_status"],
    "lot_consumptions": ["redemption_id", "lot_id", "points_used"],
    "order_returns": ["return_id", "order_id", "return_date", "refund_amount", "return_status"],
    "expiry_batches": ["expiry_batch_id", "expiry_date", "created_at", "batch_status"],
    "expiry_batch_details": ["expiry_detail_id", "expiry_batch_id", "lot_id", "transaction_id", "points_expired"],
    "audit_events": ["entity_type", "entity_id", "event_type", "event_details", "created_at"],
}

LOAD_ORDER = [
    "tiers", "customers", "rewards", "rules", "loyalty_accounts",
    "orders", "order_lines", "points_transactions", "point_lots",
    "redemptions", "lot_consumptions", "order_returns", "expiry_batches",
    "expiry_batch_details", "account_tier_history", "audit_events", "id_sequences",
]

FIRST_NAMES = ["Aarav", "Aditi", "Anaya", "Arjun", "Diya", "Isha", "Kabir", "Meera", "Neel", "Riya", "Sara", "Vivaan"]
LAST_NAMES = ["Bose", "Das", "Iyer", "Kapoor", "Mehta", "Nair", "Patel", "Rao", "Shah", "Singh", "Verma"]
SKUS = ["COFFEE-01", "TEA-02", "SNACK-03", "HOME-04", "BOOK-05", "CARE-06", "SPORT-07"]


def money(value):
    return Decimal(value).quantize(Decimal("0.01"))


def business_id(prefix, number, width):
    return f"{prefix}-2026-{number:0{width}d}"


def tier_for_spend(spend):
    eligible = [tier for tier in TIERS if Decimal(tier["minimum_spend"]) <= spend]
    return max(eligible, key=lambda item: Decimal(item["minimum_spend"]))["tier_id"]


def tier_rate(tier_id):
    return Decimal(next(tier["points_per_100"] for tier in TIERS if tier["tier_id"] == tier_id))


def make_ground_truth():
    """Return small, separate decision fixtures; these never contaminate baseline CSVs."""
    cases = [
        {
            "case_id": "EDGE-001",
            "issue_type": "BALANCE_LEDGER_MISMATCH",
            "ground_truth_label": "RECONCILIATION_REQUIRED",
            "input": {"ledger_sum": 9, "account_balance": 10},
            "expected_decision": "Flag the account for reconciliation; do not silently overwrite either value.",
        },
        {
            "case_id": "EDGE-002",
            "issue_type": "REDEMPTION_EXCEEDS_AVAILABLE",
            "ground_truth_label": "REJECT_WITHOUT_WRITES",
            "input": {"available_points": 5, "requested_points": 8},
            "expected_decision": "Reject the redemption and write no redemption or ledger event.",
        },
        {
            "case_id": "EDGE-003",
            "issue_type": "RETURN_WITHOUT_REVERSAL",
            "ground_truth_label": "MISSING_REVERSAL",
            "input": {"earned_points": 10, "refund_amount": "100.00", "order_total": "100.00", "reversed_points": 0},
            "expected_decision": "Flag the missing reversal; the expected reversal is 10 points.",
        },
        {
            "case_id": "EDGE-004",
            "issue_type": "DUPLICATE_EXPIRY",
            "ground_truth_label": "IDEMPOTENT_NOOP_ON_REPEAT",
            "input": {"lot_id": 42, "points_remaining": 6, "expiry_events": [-6, -6]},
            "expected_decision": "Accept one expiry of 6 points; make the repeated expiry a no-op.",
        },
        {
            "case_id": "EDGE-005",
            "issue_type": "TIER_USES_GROSS_SPEND",
            "ground_truth_label": "BASIC_TIER",
            "input": {"gross_spend": "6000.00", "qualifying_net_spend": "4000.00", "silver_threshold": "5000.00"},
            "expected_decision": "Keep Basic tier because qualifying net spend is below the Silver threshold.",
        },
        {
            "case_id": "EDGE-006",
            "issue_type": "CONCURRENT_REDEMPTION_ATTEMPTS",
            "ground_truth_label": "ONE_ACCEPT_ONE_REJECT",
            "input": {"available_points": 10, "simultaneous_requests": [8, 8]},
            "expected_decision": "Serialize account updates; accept at most one request and reject the other.",
        },
    ]
    return cases


def write_csv(path, fields, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore",
                                lineterminator="\n", doublequote=True)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: "" if row.get(key) is None else row.get(key, "") for key in fields})


def write_loader_sql(output_dir):
    output_path = Path(output_dir).resolve()
    try:
        data_directory = output_path.relative_to(ROOT).as_posix()
    except ValueError:
        data_directory = output_path.as_posix()
    data_directory = data_directory.replace("'", "''")
    lines = [
        "-- Run from the project root with MySQL LOCAL INFILE enabled.",
        "-- LOAD DATA paths below point to this dataset's output directory.",
    ]
    for table in LOAD_ORDER:
        variables = ", ".join(f"@v_{column}" for column in FIELDS[table])
        set_columns = ", ".join(
            f"`{column}` = NULLIF(@v_{column}, '')" for column in FIELDS[table]
        )
        path = f"{data_directory}/{table}.csv"
        lines.extend([
            f"LOAD DATA LOCAL INFILE '{path}'",
            f"INTO TABLE `{table}` CHARACTER SET utf8mb4",
            "FIELDS TERMINATED BY ',' ENCLOSED BY '\"' ESCAPED BY '\"'",
            "LINES TERMINATED BY '\\n' IGNORE 1 LINES",
            f"({variables})",
            f"SET {set_columns};",
        ])
    (output_dir / "load_generated_data.sql").write_text("\n".join(lines) + "\n", encoding="utf-8")


def generate(customers_count=20_000, orders_count=100_000, seed=DEFAULT_SEED, output_dir=None):
    if customers_count < 1:
        raise ValueError("customers must be at least 1")
    if orders_count < 4 or orders_count % 2:
        raise ValueError("orders must be an even integer of at least 4")

    rng = random.Random(seed)
    output_dir = Path(output_dir) if output_dir else ROOT / "data" / "generated"
    output_dir.mkdir(parents=True, exist_ok=True)

    customers = []
    states = {}
    tier_history = []
    for customer_id in range(1, customers_count + 1):
        customers.append({
            "customer_id": customer_id,
            "customer_code": f"LOY-{customer_id:06d}",
            "full_name": f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}",
            "email": f"customer{customer_id:06d}@example.test",
            "created_at": "2024-01-01 00:00:00",
        })
        states[customer_id] = {"spend": Decimal("0"), "balance": 0,
                               "tier_id": 1, "lots": []}
        tier_history.append({
            "history_id": len(tier_history) + 1,
            "customer_id": customer_id,
            "old_tier_id": None,
            "new_tier_id": 1,
            "qualifying_spend": "0.00",
            "changed_at": "2024-01-01 00:00:00",
        })

    orders = []
    order_lines = []
    transactions = []
    lots = []
    redemptions = []
    lot_consumptions = []
    returns = []
    expiry_batches = []
    expiry_details = []
    audit_events = []
    next_transaction = 1

    def append_transaction(customer_id, order_id, transaction_type, change,
                           event_time, description, idempotency_key, related=None):
        nonlocal next_transaction
        transaction_id = business_id("PTS", next_transaction, 8)
        next_transaction += 1
        transactions.append({
            "transaction_id": transaction_id,
            "customer_id": customer_id,
            "order_id": order_id,
            "transaction_type": transaction_type,
            "points_change": change,
            "transaction_date": event_time.strftime("%Y-%m-%d %H:%M:%S"),
            "description": description,
            "idempotency_key": idempotency_key,
            "related_transaction_id": related,
        })
        states[customer_id]["balance"] += change
        return transaction_id

    def consume_lots(customer_id, amount, preferred_lot_id=None):
        customer_lots = states[customer_id]["lots"]
        if preferred_lot_id is not None:
            customer_lots = sorted(customer_lots,
                                   key=lambda lot: lot["lot_id"] != preferred_lot_id)
        remaining = amount
        allocations = []
        for lot in customer_lots:
            if remaining == 0:
                break
            if lot["points_remaining"] <= 0:
                continue
            used = min(lot["points_remaining"], remaining)
            lot["points_remaining"] -= used
            if lot["points_remaining"] == 0:
                lot["lot_status"] = "REVERSED" if preferred_lot_id is not None else "EXHAUSTED"
            else:
                lot["lot_status"] = "PARTIALLY_USED"
            allocations.append((lot["lot_id"], used))
            remaining -= used
        return allocations

    order_facts = []
    return_count = orders_count // 2
    full_return_count = return_count // 2
    due_order_count = orders_count * 3 // 4
    expiry_cutoff = AS_OF - timedelta(days=365)
    due_max_day = (expiry_cutoff - START_DATE.date()).days
    final_day = (AS_OF - START_DATE.date()).days
    for index in range(1, orders_count + 1):
        customer_id = rng.randint(1, customers_count)
        state = states[customer_id]
        order_id = business_id("ORD", index, 6)
        if index <= due_order_count:
            day_offset = ((index - 1) * due_max_day
                          // max(1, due_order_count - 1))
        else:
            future_order_count = orders_count - due_order_count
            first_future_day = due_max_day + 1
            day_offset = first_future_day + (
                (index - due_order_count - 1)
                * max(0, final_day - first_future_day)
                // max(1, future_order_count - 1)
            )
        order_time = START_DATE + timedelta(days=day_offset,
                                            seconds=rng.randrange(86_400))
        net_cents = rng.randrange(500_000, 1_200_001)
        discount_cents = rng.randrange(0, 10_001)
        net_amount = Decimal(net_cents) / 100
        discount = Decimal(discount_cents) / 100
        gross_amount = net_amount + discount
        tier_before = state["tier_id"]
        points = int((net_amount * tier_rate(tier_before) / 100)
                     .quantize(Decimal("1"), rounding="ROUND_FLOOR"))

        orders.append({"order_id": order_id, "customer_id": customer_id,
                       "order_date": order_time.strftime("%Y-%m-%d %H:%M:%S"),
                       "total_amount": f"{net_amount:.2f}", "order_status": "COMPLETED"})
        order_lines.append({"order_line_id": index, "order_id": order_id,
                            "sku": rng.choice(SKUS), "quantity": 1,
                            "unit_price": f"{gross_amount:.2f}",
                            "discount_amount": f"{discount:.2f}", "is_eligible": 1})

        accrual_id = append_transaction(
            customer_id, order_id, "ACCRUAL", points, order_time,
            f"Eligible net spend {net_amount:.2f}", f"ACCRUAL:{order_id}")
        audit_events.append({
            "entity_type": "points_transaction", "entity_id": accrual_id,
            "event_type": "ACCRUED",
            "event_details": json.dumps({
                "customer_id": customer_id, "order_id": order_id,
                "points": points, "eligible_spend": f"{net_amount:.2f}",
            }, sort_keys=True),
            "created_at": order_time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        lot_id = index
        expires_at = order_time + timedelta(days=365)
        lot = {"lot_id": lot_id, "customer_id": customer_id,
               "source_transaction_id": accrual_id, "points_earned": points,
               "points_remaining": points,
               "earned_at": order_time.strftime("%Y-%m-%d %H:%M:%S"),
               "expires_at": expires_at.strftime("%Y-%m-%d %H:%M:%S"),
               "lot_status": "ACTIVE", "order_index": index}
        lots.append(lot)
        state["lots"].append(lot)
        state["spend"] += net_amount
        state["tier_id"] = tier_for_spend(state["spend"])

        redemption_id = business_id("RDM", index, 6)
        redemption_time = order_time + timedelta(seconds=1)
        redemption_tx = append_transaction(
            customer_id, None, "REDEMPTION", -1, redemption_time,
            f"Synthetic one-point redemption {redemption_id}",
            f"REDEMPTION:{redemption_id}")
        audit_events.append({
            "entity_type": "redemption", "entity_id": redemption_id,
            "event_type": "REDEEMED",
            "event_details": json.dumps({
                "customer_id": customer_id, "points": 1,
                "reward_id": None, "transaction_id": redemption_tx,
            }, sort_keys=True),
            "created_at": redemption_time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        allocations = consume_lots(customer_id, 1)
        if sum(amount for _, amount in allocations) != 1:
            raise RuntimeError("Generator could not allocate a valid redemption lot.")
        lot_consumptions.extend({"redemption_id": redemption_id,
                                 "lot_id": lot_id, "points_used": used}
                                for lot_id, used in allocations)
        redemptions.append({"redemption_id": redemption_id, "customer_id": customer_id,
                            "reward_id": None, "transaction_id": redemption_tx,
                            "points_redeemed": 1,
                            "redemption_date": redemption_time.strftime("%Y-%m-%d %H:%M:%S"),
                            "redemption_status": "COMPLETED"})

        if state["tier_id"] != tier_before:
            audit_events.append({
                "entity_type": "loyalty_account", "entity_id": str(customer_id),
                "event_type": "TIER_CHANGED",
                "event_details": json.dumps({
                    "old_tier_id": tier_before,
                    "new_tier_id": state["tier_id"],
                    "qualifying_spend": f"{state['spend']:.2f}",
                }, sort_keys=True),
                "created_at": order_time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            tier_history.append({"history_id": len(tier_history) + 1,
                                 "customer_id": customer_id,
                                 "old_tier_id": tier_before,
                                 "new_tier_id": state["tier_id"],
                                 "qualifying_spend": f"{state['spend']:.2f}",
                                 "changed_at": order_time.strftime("%Y-%m-%d %H:%M:%S")})
        order_facts.append({"order_id": order_id, "customer_id": customer_id,
                            "order_total": net_amount, "points_earned": points,
                            "accrual_id": accrual_id, "lot_id": lot_id,
                            "order_time": order_time, "index": index})

    for offset, fact in enumerate(order_facts[:return_count], start=1):
        is_full_return = offset <= full_return_count
        refund = fact["order_total"] if is_full_return else money(fact["order_total"] / 2)
        points_to_reverse = int((Decimal(fact["points_earned"]) * refund
                                 / fact["order_total"])
                                .quantize(Decimal("1"), rounding="ROUND_FLOOR"))
        return_id = business_id("RET", offset, 6)
        event_time = fact["order_time"] + timedelta(days=30, seconds=offset)
        returns.append({"return_id": return_id, "order_id": fact["order_id"],
                        "return_date": event_time.strftime("%Y-%m-%d %H:%M:%S"),
                        "refund_amount": f"{refund:.2f}", "return_status": "COMPLETED"})
        reversal_id = append_transaction(
            fact["customer_id"], fact["order_id"], "REVERSAL",
            -points_to_reverse, event_time,
            f"Return {return_id} reversed {points_to_reverse} points",
            f"REVERSAL:{return_id}", fact["accrual_id"])
        consume_lots(fact["customer_id"], points_to_reverse, fact["lot_id"])
        if is_full_return:
            orders[fact["index"] - 1]["order_status"] = "RETURNED"
        state = states[fact["customer_id"]]
        state["spend"] = max(Decimal("0"), state["spend"] - refund)
        audit_events.append({"entity_type": "order_return", "entity_id": return_id,
                             "event_type": "RETURN_COMPLETED",
                             "event_details": json.dumps({"order_id": fact["order_id"],
                                 "refund_amount": f"{refund:.2f}",
                                 "points_reversed": points_to_reverse,
                                 "transaction_id": reversal_id}, sort_keys=True),
                             "created_at": event_time.strftime("%Y-%m-%d %H:%M:%S")})

    expiry_batch_id = business_id("EXP", 1, 6)
    expiry_batches.append({"expiry_batch_id": expiry_batch_id,
                           "expiry_date": AS_OF.isoformat(),
                           "created_at": f"{AS_OF.isoformat()} 00:00:00",
                           "batch_status": "COMPLETED"})
    expiry_candidates = [lot for lot in lots
                         if lot["order_index"] > full_return_count
                         and lot["points_remaining"] > 0
                         and date.fromisoformat(lot["expires_at"][:10]) <= AS_OF]
    if len(expiry_candidates) < orders_count // 2:
        raise RuntimeError("Not enough open lots for the planned expiry scenarios.")

    expiry_time = datetime.combine(AS_OF, time.min)
    for detail_number, lot in enumerate(expiry_candidates[:orders_count // 2], start=1):
        points = lot["points_remaining"]
        tx_id = append_transaction(lot["customer_id"], None, "EXPIRY", -points,
                                   expiry_time, f"Expired lot {lot['lot_id']}",
                                   f"EXPIRY:{lot['lot_id']}",
                                   lot["source_transaction_id"])
        states[lot["customer_id"]]["spend"] = max(
            Decimal("0"), states[lot["customer_id"]]["spend"]
        )
        lot["points_remaining"] = 0
        lot["lot_status"] = "EXPIRED"
        expiry_details.append({"expiry_detail_id": detail_number,
                               "expiry_batch_id": expiry_batch_id,
                               "lot_id": lot["lot_id"], "transaction_id": tx_id,
                               "points_expired": points})
        audit_events.append({
            "entity_type": "point_lot", "entity_id": str(lot["lot_id"]),
            "event_type": "EXPIRED",
            "event_details": json.dumps({
                "batch_id": expiry_batch_id, "points_expired": points,
                "transaction_id": tx_id,
            }, sort_keys=True),
            "created_at": expiry_time.strftime("%Y-%m-%d %H:%M:%S"),
        })

    if any(lot["points_remaining"] > 0
           and date.fromisoformat(lot["expires_at"][:10]) <= AS_OF
           for lot in lots):
        raise RuntimeError("Generated an expired lot with points still remaining.")

    accounts = []
    for customer_id, state in states.items():
        final_tier = tier_for_spend(state["spend"])
        if final_tier != state["tier_id"]:
            audit_events.append({
                "entity_type": "loyalty_account", "entity_id": str(customer_id),
                "event_type": "TIER_CHANGED",
                "event_details": json.dumps({
                    "old_tier_id": state["tier_id"],
                    "new_tier_id": final_tier,
                    "qualifying_spend": f"{state['spend']:.2f}",
                }, sort_keys=True),
                "created_at": f"{AS_OF.isoformat()} 00:00:00",
            })
            tier_history.append({"history_id": len(tier_history) + 1,
                                 "customer_id": customer_id,
                                 "old_tier_id": state["tier_id"],
                                 "new_tier_id": final_tier,
                                 "qualifying_spend": f"{state['spend']:.2f}",
                                 "changed_at": f"{AS_OF.isoformat()} 00:00:00"})
        state["tier_id"] = final_tier
        accounts.append({"account_id": customer_id, "customer_id": customer_id,
                         "tier_id": final_tier, "current_points": state["balance"],
                         "qualifying_spend": f"{state['spend']:.2f}",
                         "created_at": "2024-01-01 00:00:00"})

    rewards = [
        {"reward_id": 1, "reward_name": "₹50 store voucher", "points_cost": 500,
         "is_active": 1, "effective_from": "2026-01-01", "effective_to": None},
        {"reward_id": 2, "reward_name": "₹100 store voucher", "points_cost": 900,
         "is_active": 1, "effective_from": "2026-01-01", "effective_to": None},
    ]
    rules = [
        {"rule_id": tier["tier_id"], "rule_name": f"{tier['tier_name']} earning rate",
         "points_per_100": tier["points_per_100"], "effective_from": "2024-01-01",
         "effective_to": None, "is_active": 1}
        for tier in TIERS
    ]
    id_sequences = [
        {"sequence_name": "points", "sequence_value": len(transactions)},
        {"sequence_name": "redemptions", "sequence_value": len(redemptions)},
        {"sequence_name": "expiry_batches", "sequence_value": len(expiry_batches)},
    ]
    table_rows = {
        "tiers": TIERS,
        "customers": customers,
        "loyalty_accounts": accounts,
        "account_tier_history": tier_history,
        "rules": rules,
        "orders": orders,
        "order_lines": order_lines,
        "rewards": rewards,
        "id_sequences": id_sequences,
        "points_transactions": transactions,
        "point_lots": lots,
        "redemptions": redemptions,
        "lot_consumptions": lot_consumptions,
        "order_returns": returns,
        "expiry_batches": expiry_batches,
        "expiry_batch_details": expiry_details,
        "audit_events": audit_events,
    }

    # Reconcile the data in memory before writing any output.
    ledger_totals = {customer_id: 0 for customer_id in states}
    for transaction in transactions:
        ledger_totals[transaction["customer_id"]] += transaction["points_change"]
    if any(ledger_totals[cid] != states[cid]["balance"] for cid in states):
        raise RuntimeError("Generated account balances do not reconcile to the ledger.")
    if len(transactions) != orders_count * 3:
        raise RuntimeError("Unexpected point transaction count.")
    audit_counts = {}
    for event in audit_events:
        audit_counts[event["event_type"]] = audit_counts.get(event["event_type"], 0) + 1
    expected_audit_counts = {
        "ACCRUED": orders_count,
        "REDEEMED": orders_count,
        "RETURN_COMPLETED": return_count,
        "EXPIRED": orders_count // 2,
    }
    if any(audit_counts.get(event_type, 0) != expected_count
           for event_type, expected_count in expected_audit_counts.items()):
        raise RuntimeError("Generated audit events do not cover all lifecycle events.")
    lot_totals = {customer_id: 0 for customer_id in states}
    for lot in lots:
        lot_totals[lot["customer_id"]] += lot["points_remaining"]
    if any(lot_totals[cid] != max(0, states[cid]["balance"]) for cid in states):
        raise RuntimeError("Generated available point lots do not reconcile to account balances.")

    transaction_ids = {row["transaction_id"] for row in transactions}
    order_ids = {row["order_id"] for row in orders}
    redemption_ids = {row["redemption_id"] for row in redemptions}
    lot_ids = {row["lot_id"] for row in lots}
    customer_ids = {row["customer_id"] for row in customers}
    idempotency_keys = [row["idempotency_key"] for row in transactions]
    if len(transaction_ids) != len(transactions):
        raise RuntimeError("Generated duplicate transaction IDs.")
    if len({row["customer_code"] for row in customers}) != customers_count:
        raise RuntimeError("Generated duplicate customer business IDs.")
    if len({row["order_id"] for row in orders}) != orders_count:
        raise RuntimeError("Generated duplicate order IDs.")
    if len(redemption_ids) != orders_count:
        raise RuntimeError("Generated duplicate redemption IDs.")
    if len(set(idempotency_keys)) != len(idempotency_keys):
        raise RuntimeError("Generated duplicate idempotency keys.")
    if any(row["customer_id"] not in customer_ids for row in orders):
        raise RuntimeError("Generated an order without its customer.")
    if any(row["order_id"] not in order_ids for row in order_lines):
        raise RuntimeError("Generated an order line without its parent order.")
    if any(row["customer_id"] not in customer_ids for row in transactions):
        raise RuntimeError("Generated a points transaction without its customer.")
    if any(row["order_id"] is not None and row["order_id"] not in order_ids
           for row in transactions):
        raise RuntimeError("Generated a points transaction without its order.")
    if any(row["transaction_id"] not in transaction_ids for row in redemptions):
        raise RuntimeError("Generated a redemption without its ledger transaction.")
    if any(row["source_transaction_id"] not in transaction_ids for row in lots):
        raise RuntimeError("Generated a point lot without its accrual transaction.")
    if any(row["lot_id"] not in lot_ids or row["redemption_id"] not in redemption_ids
           for row in lot_consumptions):
        raise RuntimeError("Generated a lot consumption with a missing parent record.")
    if any(row["transaction_id"] not in transaction_ids or row["lot_id"] not in lot_ids
           for row in expiry_details):
        raise RuntimeError("Generated an expiry detail with a missing parent record.")
    order_totals = {row["order_id"]: Decimal(row["total_amount"]) for row in orders}
    if any(Decimal(row["unit_price"]) * row["quantity"]
           - Decimal(row["discount_amount"]) != order_totals[row["order_id"]]
           for row in order_lines):
        raise RuntimeError("Generated order line arithmetic does not match its order total.")
    order_dates = {row["order_id"]: datetime.strptime(row["order_date"], "%Y-%m-%d %H:%M:%S")
                   for row in orders}
    if any(datetime.strptime(row["return_date"], "%Y-%m-%d %H:%M:%S")
           <= order_dates[row["order_id"]] for row in returns):
        raise RuntimeError("Generated a return earlier than its order.")
    if any(datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S")
           <= datetime.strptime(row["earned_at"], "%Y-%m-%d %H:%M:%S") for row in lots):
        raise RuntimeError("Generated a lot expiry earlier than its accrual.")
    refunds_by_order = {}
    for row in returns:
        refunds_by_order[row["order_id"]] = refunds_by_order.get(row["order_id"], Decimal("0")) + Decimal(row["refund_amount"])
    if any(amount > order_totals[order_id] for order_id, amount in refunds_by_order.items()):
        raise RuntimeError("Generated refunds exceed their order total.")

    file_manifest = {}
    for table, rows in table_rows.items():
        path = output_dir / f"{table}.csv"
        write_csv(path, FIELDS[table], rows)
        file_manifest[path.name] = {
            "rows": len(rows),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    cases = make_ground_truth()
    (output_dir / "edge_cases.json").write_text(
        json.dumps(cases, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    write_csv(output_dir / "ground_truth.csv",
              ["case_id", "issue_type", "ground_truth_label", "expected_decision"], cases)
    write_loader_sql(output_dir)
    for extra in ("edge_cases.json", "ground_truth.csv", "load_generated_data.sql"):
        path = output_dir / extra
        file_manifest[path.name] = {
            "rows": len(cases) if extra != "load_generated_data.sql" else None,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    validation_summary = {
        "passed": True,
        "checks": {
            "account_balances_equal_ledger_sums": {"passed": True, "customers_checked": customers_count},
            "available_points_equal_open_lot_sums": {"passed": True, "customers_checked": customers_count},
            "transaction_ids_unique": {"passed": True, "rows_checked": len(transactions)},
            "business_ids_and_idempotency_keys_unique": {"passed": True},
            "foreign_key_parent_references_present": {"passed": True},
            "arithmetic_and_date_ordering_valid": {"passed": True},
            "expired_lots_closed": {"passed": True},
            "lifecycle_events_audited": {"passed": True,
                                         "event_counts": expected_audit_counts},
            "point_transaction_volume": {"passed": True, "expected": orders_count * 3,
                                         "actual": len(transactions)},
        },
    }
    validation_path = output_dir / "validation_summary.json"
    validation_path.write_text(json.dumps(validation_summary, indent=2, sort_keys=True) + "\n",
                               encoding="utf-8")
    file_manifest[validation_path.name] = {
        "rows": None,
        "sha256": hashlib.sha256(validation_path.read_bytes()).hexdigest(),
    }

    manifest = {
        "dataset_name": "loyalty-ledger-synthetic",
        "seed": seed,
        "as_of_date": AS_OF.isoformat(),
        "customers_requested": customers_count,
        "orders_requested": orders_count,
        "point_transactions_expected": orders_count * 3,
        "generation_notes": [
            "All baseline data is synthetic and uses example.test email addresses.",
            "Returns and expiry events are derived from generated source records.",
            "Accrual, redemption, return, expiry, and tier events are recorded in audit_events.",
            "Edge-case fixtures and ground-truth labels are separate from baseline tables.",
        ],
        "files": file_manifest,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--customers", type=int, default=20_000)
    parser.add_argument("--orders", type=int, default=100_000,
                        help="Must be even; output has three point events per order.")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--output", default=str(ROOT / "data" / "generated"))
    args = parser.parse_args()
    manifest = generate(args.customers, args.orders, args.seed, args.output)
    print(f"Generated {manifest['customers_requested']:,} customers, "
          f"{manifest['orders_requested']:,} orders, and "
          f"{manifest['point_transactions_expected']:,} points transactions.")
    print(f"Output: {Path(args.output).resolve()}")
    print(f"Seed: {args.seed}")


if __name__ == "__main__":
    main()
