"""Unit, synthetic-data, and isolated-MySQL workflow tests."""

import csv
from collections import Counter
import json
import os
import re
import sys
import subprocess
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import mysql.connector


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from db import get_connection  # noqa: E402
from expiry_service import expire_points  # noqa: E402
from generate_data import generate, make_ground_truth  # noqa: E402
from loyalty_service import award_points, calculate_points, complete_return  # noqa: E402
from redeem_points import redeem_points  # noqa: E402
from validate_data import validate_data  # noqa: E402


def _connection_settings(database=None):
    settings = {
        "host": os.getenv("LOYALTY_DB_HOST", "localhost"),
        "port": int(os.getenv("LOYALTY_DB_PORT", "3306")),
        "user": os.environ["LOYALTY_DB_USER"],
        "password": os.environ["LOYALTY_DB_PASSWORD"],
    }
    if database:
        settings["database"] = database
    return settings


def _execute_script(connection, script, database_name):
    cursor = connection.cursor()
    final_rows = None
    try:
        for statement in script.split(";"):
            statement = statement.strip()
            if not statement:
                continue
            if statement.upper().startswith("CREATE DATABASE"):
                continue
            statement = statement.replace(
                "USE loyalty_ledger", f"USE `{database_name}`"
            )
            cursor.execute(statement)
            if cursor.with_rows:
                final_rows = cursor.fetchall()
    finally:
        cursor.close()
    return final_rows


class LoyaltyUnitTests(unittest.TestCase):
    def test_points_round_down_and_reject_negative_values(self):
        self.assertEqual(calculate_points("199.99", "1.5"), 2)
        self.assertEqual(calculate_points("99.99", 1), 0)
        with self.assertRaises(ValueError):
            calculate_points(-1, 1)
        with self.assertRaises(ValueError):
            calculate_points(100, -1)

    def test_redemption_rejects_nonpositive_and_noninteger_values(self):
        for value in (0, -1, 1.5, True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    redeem_points(1, value)


class SyntheticDataTests(unittest.TestCase):
    def test_all_required_edge_cases_have_separate_ground_truth(self):
        cases = make_ground_truth()
        self.assertEqual(len(cases), 6)
        self.assertEqual(len({case["case_id"] for case in cases}), 6)
        self.assertEqual(len({case["issue_type"] for case in cases}), 6)
        self.assertTrue(all(case["ground_truth_label"] for case in cases))
        self.assertTrue(all(case["expected_decision"] for case in cases))
        for case in cases:
            with self.subTest(case_id=case["case_id"]):
                self.assertTrue(hasattr(
                    MySQLWorkflowTests, case["verification_test"]
                ))

    def test_same_seed_produces_identical_small_dataset(self):
        with tempfile.TemporaryDirectory(prefix="loyalty-ledger-repro-") as folder:
            output_dir = Path(folder) / "dataset"
            manifests = []
            for _run in range(2):
                result = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "src" / "generate_data.py"),
                        "--customers", "20",
                        "--orders", "100",
                        "--seed", "20261022",
                        "--output", str(output_dir),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                manifests.append(json.loads(
                    (output_dir / "manifest.json").read_text(encoding="utf-8")
                ))
            self.assertEqual(manifests[0], manifests[1])
            self.assertTrue(manifests[0]["files"])
            loader_sql = (output_dir / "load_generated_data.sql").read_text(
                encoding="utf-8"
            )
            self.assertIn((output_dir / "tiers.csv").resolve().as_posix(), loader_sql)
            self.assertNotIn("FOREIGN_KEY_CHECKS = 0", loader_sql)
            with (output_dir / "audit_events.csv").open(
                encoding="utf-8", newline=""
            ) as stream:
                audit_types = Counter(row["event_type"] for row in csv.DictReader(stream))
            self.assertEqual(audit_types["ACCRUED"], 100)
            self.assertEqual(audit_types["REDEEMED"], 100)
            self.assertEqual(audit_types["RETURN_COMPLETED"], 50)
            self.assertEqual(audit_types["EXPIRED"], 50)
            id_formats = {
                "customers.csv": ("customer_code", r"LOY-\d{6}"),
                "orders.csv": ("order_id", r"ORD-2026-\d{6}"),
                "points_transactions.csv": ("transaction_id", r"PTS-2026-\d{8}"),
                "redemptions.csv": ("redemption_id", r"RDM-2026-\d{6}"),
                "expiry_batches.csv": ("expiry_batch_id", r"EXP-2026-\d{6}"),
            }
            for filename, (field, pattern) in id_formats.items():
                with (output_dir / filename).open(encoding="utf-8", newline="") as stream:
                    values = [row[field] for row in csv.DictReader(stream)]
                self.assertTrue(values, filename)
                self.assertTrue(
                    all(re.fullmatch(pattern, value) for value in values),
                    f"Incorrect ID format in {filename}",
                )

    def test_loader_paths_follow_each_output_directory(self):
        with tempfile.TemporaryDirectory(prefix="loyalty-ledger-paths-") as folder:
            root = Path(folder)
            outputs = [root / "first", root / "second"]
            manifests = []
            for output_dir in outputs:
                manifests.append(generate(2, 4, 42, output_dir))
            shared_files = set(manifests[0]["files"]) & set(manifests[1]["files"])
            shared_files.discard("load_generated_data.sql")
            shared_files.discard("manifest.json")
            for name in shared_files:
                self.assertEqual(manifests[0]["files"][name]["sha256"],
                                 manifests[1]["files"][name]["sha256"])
            for output_dir in outputs:
                loader = (output_dir / "load_generated_data.sql").read_text(
                    encoding="utf-8"
                )
                self.assertIn((output_dir / "customers.csv").resolve().as_posix(), loader)


class MigrationTests(unittest.TestCase):
    """Exercise the one-time upgrade and repeat-safe balance repair on fake data."""

    @classmethod
    def setUpClass(cls):
        cls.original_database = os.environ.get("LOYALTY_DB_NAME")
        cls.test_database = "loyalty_migration_test_" + uuid.uuid4().hex[:10]
        cls.admin = mysql.connector.connect(**_connection_settings())
        cursor = cls.admin.cursor()
        try:
            cursor.execute(
                f"CREATE DATABASE `{cls.test_database}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
            )
        except Exception:
            cursor.close()
            cls.admin.close()
            raise
        cursor.close()
        os.environ["LOYALTY_DB_NAME"] = cls.test_database

        connection = None
        try:
            connection = mysql.connector.connect(
                **_connection_settings(cls.test_database)
            )
            _execute_script(
                connection,
                (ROOT / "tests" / "fixtures" / "legacy_schema.sql").read_text(
                    encoding="utf-8"
                ),
                cls.test_database,
            )
            _execute_script(
                connection,
                (ROOT / "sql" / "migrate_v2.sql").read_text(encoding="utf-8"),
                cls.test_database,
            )
            _execute_script(
                connection,
                (ROOT / "sql" / "migrate_v3.sql").read_text(encoding="utf-8"),
                cls.test_database,
            )
            _execute_script(
                connection,
                (ROOT / "sql" / "migrate_v3.sql").read_text(encoding="utf-8"),
                cls.test_database,
            )
            repair_rows = _execute_script(
                connection,
                (ROOT / "sql" / "reconcile_opening_balances.sql").read_text(
                    encoding="utf-8"
                ),
                cls.test_database,
            )
            connection.commit()
            if repair_rows:
                raise AssertionError(
                    f"Opening-balance repair left mismatches: {repair_rows[:3]}"
                )
        except Exception:
            if connection is not None:
                connection.close()
            cls._drop_test_database()
            raise
        finally:
            if connection is not None and connection.is_connected():
                connection.close()

    @classmethod
    def tearDownClass(cls):
        try:
            cls._drop_test_database()
        finally:
            if cls.original_database is None:
                os.environ.pop("LOYALTY_DB_NAME", None)
            else:
                os.environ["LOYALTY_DB_NAME"] = cls.original_database

    @classmethod
    def _drop_test_database(cls):
        if getattr(cls, "admin", None) and cls.admin.is_connected():
            cursor = cls.admin.cursor()
            try:
                cursor.execute(f"DROP DATABASE IF EXISTS `{cls.test_database}`")
            finally:
                cursor.close()
                cls.admin.close()

    def test_upgrade_adds_v3_schema_and_backfills_legacy_accrual_lots(self):
        connection = get_connection()
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                "SELECT customer_code FROM customers WHERE customer_id = 1"
            )
            self.assertEqual(cursor.fetchone()["customer_code"], "LOY-000001")
            cursor.execute(
                "SELECT points_earned, points_remaining FROM point_lots "
                "WHERE source_transaction_id = 'PTS-2026-00000001'"
            )
            accrual_lot = cursor.fetchone()
            self.assertEqual(accrual_lot["points_earned"], 5)
            self.assertEqual(accrual_lot["points_remaining"], 5)
            cursor.execute(
                "SELECT current_points FROM loyalty_accounts WHERE customer_id = 1"
            )
            self.assertEqual(cursor.fetchone()["current_points"], 505)
            cursor.execute(
                "SELECT SUM(points_change) AS ledger_points FROM points_transactions "
                "WHERE customer_id = 1"
            )
            self.assertEqual(cursor.fetchone()["ledger_points"], 505)
            cursor.execute(
                "SELECT SUM(points_remaining) AS lot_points FROM point_lots "
                "WHERE customer_id = 1"
            )
            self.assertEqual(cursor.fetchone()["lot_points"], 505)
            cursor.execute(
                "SELECT COUNT(*) AS adjustment_count FROM points_transactions "
                "WHERE idempotency_key = 'OPENING-BALANCE-CUSTOMER-1'"
            )
            self.assertEqual(cursor.fetchone()["adjustment_count"], 1)
            cursor.execute(
                "SELECT COUNT(*) AS tier_rule_count FROM rules WHERE tier_id IS NOT NULL"
            )
            self.assertEqual(cursor.fetchone()["tier_rule_count"], 3)
            cursor.execute(
                """SELECT COUNT(*) AS metadata_columns
                   FROM information_schema.COLUMNS
                   WHERE TABLE_SCHEMA = DATABASE()
                     AND TABLE_NAME = 'points_transactions'
                     AND COLUMN_NAME IN ('rule_id', 'earning_rate', 'eligible_spend')"""
            )
            self.assertEqual(cursor.fetchone()["metadata_columns"], 3)
            cursor.execute(
                """SELECT COUNT(*) AS allocation_table_count
                   FROM information_schema.TABLES
                   WHERE TABLE_SCHEMA = DATABASE()
                     AND TABLE_NAME = 'reversal_lot_allocations'"""
            )
            self.assertEqual(cursor.fetchone()["allocation_table_count"], 1)
            cursor.execute(
                """SELECT DATETIME_PRECISION FROM information_schema.COLUMNS
                   WHERE TABLE_SCHEMA = DATABASE()
                     AND TABLE_NAME = 'points_transactions'
                     AND COLUMN_NAME = 'transaction_date'"""
            )
            self.assertEqual(cursor.fetchone()["DATETIME_PRECISION"], 6)
        finally:
            cursor.close()
            connection.close()

    def test_opening_balance_repair_can_be_repeated_without_duplicate_writes(self):
        connection = get_connection()
        try:
            rows = _execute_script(
                connection,
                (ROOT / "sql" / "reconcile_opening_balances.sql").read_text(
                    encoding="utf-8"
                ),
                self.test_database,
            )
            connection.commit()
            self.assertEqual(rows, [])
        finally:
            connection.close()

        connection = get_connection()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT COUNT(*) FROM points_transactions "
                "WHERE idempotency_key = 'OPENING-BALANCE-CUSTOMER-1'"
            )
            self.assertEqual(cursor.fetchone()[0], 1)
        finally:
            cursor.close()
            connection.close()


class MySQLWorkflowTests(unittest.TestCase):
    """Run against a uniquely named throwaway schema, never the user's database."""

    _customer_ids = iter(range(910001, 919999))

    @classmethod
    def setUpClass(cls):
        if not os.getenv("LOYALTY_DB_USER") or not os.getenv("LOYALTY_DB_PASSWORD"):
            raise RuntimeError("Set LOYALTY_DB_USER and LOYALTY_DB_PASSWORD in .env first.")

        cls.original_database = os.environ.get("LOYALTY_DB_NAME")
        cls.test_database = "loyalty_test_" + uuid.uuid4().hex[:12]
        cls.admin = mysql.connector.connect(**_connection_settings())
        cursor = cls.admin.cursor()
        try:
            cursor.execute(
                f"CREATE DATABASE `{cls.test_database}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
            )
        except Exception:
            cursor.close()
            cls.admin.close()
            raise
        cursor.close()
        os.environ["LOYALTY_DB_NAME"] = cls.test_database

        try:
            connection = mysql.connector.connect(
                **_connection_settings(cls.test_database)
            )
            try:
                _execute_script(
                    connection,
                    (ROOT / "sql" / "schema.sql").read_text(encoding="utf-8"),
                    cls.test_database,
                )
                _execute_script(
                    connection,
                    (ROOT / "sql" / "seed_demo.sql").read_text(encoding="utf-8"),
                    cls.test_database,
                )
                connection.commit()
            finally:
                connection.close()
        except Exception:
            cls._drop_test_database()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            cls._drop_test_database()
        finally:
            if cls.original_database is None:
                os.environ.pop("LOYALTY_DB_NAME", None)
            else:
                os.environ["LOYALTY_DB_NAME"] = cls.original_database

    @classmethod
    def _drop_test_database(cls):
        if getattr(cls, "admin", None) and cls.admin.is_connected():
            cursor = cls.admin.cursor()
            try:
                cursor.execute(f"DROP DATABASE IF EXISTS `{cls.test_database}`")
            finally:
                cursor.close()
                cls.admin.close()

    def setUp(self):
        self.customer_id = next(self._customer_ids)
        self.connection = get_connection()
        self.cursor = self.connection.cursor()
        self.cursor.execute(
            "INSERT INTO customers (customer_id, customer_code, full_name, email) "
            "VALUES (%s, %s, %s, %s)",
            (self.customer_id, f"LOY-{self.customer_id:06d}", "Test Customer",
             f"test-{self.customer_id}@example.test"),
        )
        self.cursor.execute(
            "INSERT INTO loyalty_accounts (account_id, customer_id, tier_id) "
            "VALUES (%s, %s, 1)",
            (self.customer_id, self.customer_id),
        )
        self.connection.commit()
        self.cursor.close()
        self.connection.close()

    def _create_order(self, total, *, status="COMPLETED", line=None, suffix="A"):
        order_id = f"ORD-T-{self.customer_id}-{suffix}"
        connection = get_connection()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "INSERT INTO orders (order_id, customer_id, order_date, total_amount, order_status) "
                "VALUES (%s, %s, %s, %s, %s)",
                (order_id, self.customer_id, datetime.now() - timedelta(days=1),
                 Decimal(str(total)), status),
            )
            if line:
                cursor.execute(
                    "INSERT INTO order_lines "
                    "(order_id, sku, quantity, unit_price, discount_amount, is_eligible) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (order_id, f"SKU-{suffix}", line[0], Decimal(str(line[1])),
                     Decimal(str(line[2])), line[3]),
                )
            connection.commit()
        finally:
            cursor.close()
            connection.close()
        return order_id

    def _add_points_lot(self, points, *, expires_at, suffix="OPEN"):
        transaction_id = f"FIX-{self.customer_id}-{suffix}"
        connection = get_connection()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "INSERT INTO points_transactions "
                "(transaction_id, customer_id, transaction_type, points_change, "
                "transaction_date, description, idempotency_key) "
                "VALUES (%s, %s, 'ADJUSTMENT', %s, NOW(6), 'Test fixture', %s)",
                (transaction_id, self.customer_id, points,
                 f"TEST:{self.customer_id}:{suffix}"),
            )
            cursor.execute(
                "INSERT INTO point_lots "
                "(customer_id, source_transaction_id, points_earned, points_remaining, "
                "earned_at, expires_at, lot_status) "
                "VALUES (%s, %s, %s, %s, NOW(6), %s, 'ACTIVE')",
                (self.customer_id, transaction_id, points, points, expires_at),
            )
            cursor.execute(
                "UPDATE loyalty_accounts SET current_points = current_points + %s "
                "WHERE customer_id = %s",
                (points, self.customer_id),
            )
            connection.commit()
        finally:
            cursor.close()
            connection.close()
        return transaction_id

    def _query(self, sql, params=()):
        connection = get_connection()
        cursor = connection.cursor()
        try:
            cursor.execute(sql, params)
            return cursor.fetchall()
        finally:
            cursor.close()
            connection.close()

    def test_award_uses_eligible_net_spend_and_rejects_duplicate(self):
        order_id = self._create_order(1000, line=(2, 500, 100, True))
        result = award_points(order_id)
        self.assertEqual(result["points"], 9)
        applied_rule = self._query(
            "SELECT rule_id, earning_rate, eligible_spend FROM points_transactions "
            "WHERE transaction_id = %s", (result["transaction_id"],),
        )[0]
        self.assertEqual(applied_rule, (1, Decimal("1.00"), Decimal("900.00")))
        self.assertEqual(self._query(
            "SELECT current_points FROM loyalty_accounts WHERE customer_id = %s",
            (self.customer_id,),
        )[0][0], 9)
        with self.assertRaisesRegex(ValueError, "already been awarded"):
            award_points(order_id)
        self.assertEqual(self._query(
            "SELECT COUNT(*) FROM points_transactions WHERE order_id = %s "
            "AND transaction_type = 'ACCRUAL'", (order_id,),
        )[0][0], 1)

    def test_award_redeem_return_and_expiry_command_line_entrypoints(self):
        environment = os.environ.copy()
        order_id = self._create_order(500)

        award = subprocess.run(
            [sys.executable, str(ROOT / "src" / "award_points.py"), order_id],
            capture_output=True, text=True, env=environment, timeout=30,
        )
        self.assertEqual(award.returncode, 0, award.stderr)
        self.assertIn("Awarded 5 points", award.stdout)

        redemption = subprocess.run(
            [sys.executable, str(ROOT / "src" / "redeem_points.py")],
            input=f"{self.customer_id}\n5\n", capture_output=True, text=True,
            env=environment, timeout=30,
        )
        self.assertEqual(redemption.returncode, 0, redemption.stderr)
        self.assertIn("Redemption successful", redemption.stdout)

        return_id = f"RET-T-{self.customer_id}-A"
        connection = get_connection()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "INSERT INTO order_returns "
                "(return_id, order_id, return_date, refund_amount, return_status) "
                "VALUES (%s, %s, NOW(6), 500, 'REQUESTED')",
                (return_id, order_id),
            )
            connection.commit()
        finally:
            cursor.close()
            connection.close()

        returned = subprocess.run(
            [sys.executable, str(ROOT / "src" / "return_points.py"), return_id],
            capture_output=True, text=True, env=environment, timeout=30,
        )
        self.assertEqual(returned.returncode, 0, returned.stderr)
        self.assertIn("Points reversed: 5", returned.stdout)

        self._add_points_lot(
            4, expires_at=datetime.now() - timedelta(days=1), suffix="CLIEXP"
        )
        expiry = subprocess.run(
            [sys.executable, str(ROOT / "src" / "expiry_service.py")],
            capture_output=True, text=True, env=environment, timeout=30,
        )
        self.assertEqual(expiry.returncode, 0, expiry.stderr)
        self.assertIn("Lots expired: 1", expiry.stdout)
        self.assertIn("Points expired: 4", expiry.stdout)

    def test_cli_commands_return_failure_status_for_rejected_requests(self):
        environment = os.environ.copy()
        award = subprocess.run(
            [sys.executable, str(ROOT / "src" / "award_points.py"), "MISSING-ORDER"],
            capture_output=True, text=True, env=environment, timeout=30,
        )
        self.assertNotEqual(award.returncode, 0)
        self.assertIn("Order not found", award.stdout)

        redemption = subprocess.run(
            [sys.executable, str(ROOT / "src" / "redeem_points.py")],
            input=f"{self.customer_id}\n0\n", capture_output=True, text=True,
            env=environment, timeout=30,
        )
        self.assertNotEqual(redemption.returncode, 0)
        self.assertIn("positive whole number", redemption.stdout)

    def test_only_completed_orders_can_accrue(self):
        order_id = self._create_order(500, status="CANCELLED")
        with self.assertRaisesRegex(ValueError, "Only completed orders"):
            award_points(order_id)
        self.assertEqual(self._query(
            "SELECT COUNT(*) FROM points_transactions WHERE order_id = %s",
            (order_id,),
        )[0][0], 0)

    def test_no_line_orders_use_total_and_zero_point_orders_are_idempotent(self):
        fallback_order = self._create_order(500, suffix="A")
        self.assertEqual(award_points(fallback_order)["points"], 5)

        small_order = self._create_order(99, suffix="B")
        result = award_points(small_order)
        self.assertEqual(result["points"], 0)
        self.assertIsNone(result["transaction_id"])
        with self.assertRaisesRegex(ValueError, "qualifying spend was already processed"):
            award_points(small_order)
        self.assertEqual(self._query(
            "SELECT COUNT(*) FROM points_transactions WHERE order_id = %s",
            (small_order,),
        )[0][0], 0)

    def test_ineligible_order_lines_do_not_fall_back_to_order_total(self):
        order_id = self._create_order(
            500, line=(1, 500, 0, False)
        )
        result = award_points(order_id)
        self.assertEqual(result["points"], 0)
        self.assertEqual(self._query(
            "SELECT current_points FROM loyalty_accounts WHERE customer_id = %s",
            (self.customer_id,),
        )[0][0], 0)

    def test_reward_must_be_active_and_match_its_cost(self):
        self._add_points_lot(
            600, expires_at=datetime.now() + timedelta(days=30)
        )
        with self.assertRaisesRegex(ValueError, "must match the reward"):
            redeem_points(self.customer_id, 499, reward_id=1)

        connection = get_connection()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "INSERT INTO rewards "
                "(reward_id, reward_name, points_cost, is_active, effective_from) "
                "VALUES (99, 'Inactive test reward', 100, FALSE, %s)",
                (date.today() - timedelta(days=1),),
            )
            connection.commit()
        finally:
            cursor.close()
            connection.close()
        with self.assertRaisesRegex(ValueError, "not currently available"):
            redeem_points(self.customer_id, 100, reward_id=99)

        connection = get_connection()
        cursor = connection.cursor()
        try:
            today = date.today()
            cursor.executemany(
                "INSERT INTO rewards "
                "(reward_id, reward_name, points_cost, is_active, effective_from, effective_to) "
                "VALUES (%s, %s, 100, TRUE, %s, %s)",
                [(97, "Expired test reward", today - timedelta(days=100),
                  today - timedelta(days=1)),
                 (98, "Future test reward", today + timedelta(days=1), None)],
            )
            connection.commit()
        finally:
            cursor.close()
            connection.close()
        for reward_id in (97, 98):
            with self.subTest(reward_id=reward_id):
                with self.assertRaisesRegex(ValueError, "not currently available"):
                    redeem_points(self.customer_id, 100, reward_id=reward_id)

        result = redeem_points(self.customer_id, 500, reward_id=1)
        self.assertEqual(result["remaining_points"], 100)
        self.assertEqual(self._query(
            "SELECT reward_id FROM redemptions WHERE redemption_id = %s",
            (result["redemption_id"],),
        )[0][0], 1)

    def test_redemption_consumes_earliest_expiry_lots_first(self):
        first = self._create_order(5000, suffix="A")
        award_points(first)
        second = self._create_order(5000, suffix="B")
        award_points(second)
        lots = self._query(
            "SELECT lot_id, points_remaining FROM point_lots "
            "WHERE customer_id = %s ORDER BY earned_at, lot_id",
            (self.customer_id,),
        )
        self.assertEqual([row[1] for row in lots], [50, 75])

        result = redeem_points(self.customer_id, 70)
        self.assertEqual(result["remaining_points"], 55)
        uses = self._query(
            "SELECT lot_id, points_used FROM lot_consumptions "
            "WHERE redemption_id = %s ORDER BY lot_id",
            (result["redemption_id"],),
        )
        self.assertEqual(uses, [(lots[0][0], 50), (lots[1][0], 20)])

    def test_redemption_over_balance_is_rejected_without_writes(self):
        self._add_points_lot(
            5, expires_at=datetime.now() + timedelta(days=30)
        )
        with self.assertRaisesRegex(ValueError, "Not enough points"):
            redeem_points(self.customer_id, 8)
        self.assertEqual(self._query(
            "SELECT current_points FROM loyalty_accounts WHERE customer_id = %s",
            (self.customer_id,),
        )[0][0], 5)
        self.assertEqual(self._query(
            "SELECT COUNT(*) FROM redemptions WHERE customer_id = %s",
            (self.customer_id,),
        )[0][0], 0)

    def test_partial_returns_reverse_once_and_link_to_original_accrual(self):
        order_id = self._create_order(500)
        accrual = award_points(order_id)["transaction_id"]
        first_return = f"RET-T-{self.customer_id}-A"
        second_return = f"RET-T-{self.customer_id}-B"
        connection = get_connection()
        cursor = connection.cursor()
        try:
            cursor.executemany(
                "INSERT INTO order_returns "
                "(return_id, order_id, return_date, refund_amount, return_status) "
                "VALUES (%s, %s, NOW(6), %s, 'REQUESTED')",
                [(first_return, order_id, 200), (second_return, order_id, 300)],
            )
            connection.commit()
        finally:
            cursor.close()
            connection.close()

        first_result = complete_return(first_return)
        second_result = complete_return(second_return)
        self.assertEqual(first_result["points_reversed"], 2)
        self.assertEqual(second_result["points_reversed"], 3)
        reversals = self._query(
            "SELECT points_change, related_transaction_id FROM points_transactions "
            "WHERE order_id = %s AND transaction_type = 'REVERSAL' ORDER BY transaction_id",
            (order_id,),
        )
        self.assertEqual(sorted(row[0] for row in reversals), [-3, -2])
        self.assertTrue(all(row[1] == accrual for row in reversals))
        with self.assertRaisesRegex(ValueError, "Only a requested return"):
            complete_return(first_return)

    def test_return_before_accrual_is_netted_from_earned_points(self):
        order_id = self._create_order(500)
        return_id = f"RET-T-{self.customer_id}-PRE"
        connection = get_connection()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "INSERT INTO order_returns "
                "(return_id, order_id, return_date, refund_amount, return_status) "
                "VALUES (%s, %s, NOW(6), 200, 'REQUESTED')",
                (return_id, order_id),
            )
            connection.commit()
        finally:
            cursor.close()
            connection.close()

        result = complete_return(return_id)
        self.assertEqual(result["points_reversed"], 0)
        accrual = award_points(order_id)
        self.assertEqual(accrual["points"], 3)
        self.assertEqual(self._query(
            "SELECT eligible_spend FROM points_transactions "
            "WHERE transaction_id = %s", (accrual["transaction_id"],),
        )[0][0], Decimal("300.00"))
        self.assertEqual(self._query(
            "SELECT COUNT(*) FROM points_transactions "
            "WHERE order_id = %s AND transaction_type = 'REVERSAL'", (order_id,),
        )[0][0], 0)

    def test_full_return_before_accrual_cannot_earn_points(self):
        order_id = self._create_order(500)
        return_id = f"RET-T-{self.customer_id}-FULLPRE"
        connection = get_connection()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "INSERT INTO order_returns "
                "(return_id, order_id, return_date, refund_amount, return_status) "
                "VALUES (%s, %s, NOW(6), 500, 'REQUESTED')",
                (return_id, order_id),
            )
            connection.commit()
        finally:
            cursor.close()
            connection.close()
        complete_return(return_id)
        with self.assertRaisesRegex(ValueError, "Only completed orders"):
            award_points(order_id)
        self.assertEqual(self._query(
            "SELECT COUNT(*) FROM points_transactions WHERE order_id = %s", (order_id,),
        )[0][0], 0)

    def test_completed_refunds_cannot_exceed_order_total(self):
        order_id = self._create_order(500)
        award_points(order_id)
        connection = get_connection()
        cursor = connection.cursor()
        try:
            cursor.executemany(
                "INSERT INTO order_returns "
                "(return_id, order_id, return_date, refund_amount, return_status) "
                "VALUES (%s, %s, NOW(6), %s, 'REQUESTED')",
                [(f"RET-T-{self.customer_id}-A", order_id, 300),
                 (f"RET-T-{self.customer_id}-B", order_id, 300)],
            )
            connection.commit()
        finally:
            cursor.close()
            connection.close()
        complete_return(f"RET-T-{self.customer_id}-A")
        with self.assertRaisesRegex(ValueError, "cannot exceed the order total"):
            complete_return(f"RET-T-{self.customer_id}-B")
        self.assertEqual(self._query(
            "SELECT return_status FROM order_returns WHERE return_id = %s",
            (f"RET-T-{self.customer_id}-B",),
        )[0][0], "REQUESTED")

    def test_return_after_redemption_can_create_audited_negative_balance(self):
        order_id = self._create_order(500)
        accrual_id = award_points(order_id)["transaction_id"]
        redemption = redeem_points(self.customer_id, 5)
        return_id = f"RET-T-{self.customer_id}-A"
        connection = get_connection()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "INSERT INTO order_returns "
                "(return_id, order_id, return_date, refund_amount, return_status) "
                "VALUES (%s, %s, NOW(6), 500, 'REQUESTED')",
                (return_id, order_id),
            )
            connection.commit()
        finally:
            cursor.close()
            connection.close()

        result = complete_return(return_id)
        self.assertEqual(result["points_reversed"], 5)
        self.assertEqual(self._query(
            "SELECT current_points FROM loyalty_accounts WHERE customer_id = %s",
            (self.customer_id,),
        )[0][0], -5)
        self.assertEqual(self._query(
            "SELECT related_transaction_id FROM points_transactions "
            "WHERE transaction_id = %s",
            (result["transaction_id"],),
        )[0][0], accrual_id)
        self.assertEqual(self._query(
            "SELECT redemption_status FROM redemptions WHERE redemption_id = %s",
            (redemption["redemption_id"],),
        )[0][0], "COMPLETED")

    def test_zero_value_order_cannot_be_returned(self):
        order_id = self._create_order(0)
        return_id = f"RET-T-{self.customer_id}-A"
        connection = get_connection()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "INSERT INTO order_returns "
                "(return_id, order_id, return_date, refund_amount, return_status) "
                "VALUES (%s, %s, NOW(6), 0, 'REQUESTED')",
                (return_id, order_id),
            )
            connection.commit()
        finally:
            cursor.close()
            connection.close()
        with self.assertRaisesRegex(ValueError, "zero-value order"):
            complete_return(return_id)
        self.assertEqual(self._query(
            "SELECT return_status FROM order_returns WHERE return_id = %s",
            (return_id,),
        )[0][0], "REQUESTED")

    def test_expiry_is_idempotent_for_the_same_lot(self):
        self._add_points_lot(
            20, expires_at=datetime.now() - timedelta(days=1)
        )
        first = expire_points(as_of=date.today())
        second = expire_points(as_of=date.today())
        self.assertEqual((first["lots_expired"], first["points_expired"]), (1, 20))
        self.assertEqual((second["lots_expired"], second["points_expired"]), (0, 0))
        self.assertEqual(self._query(
            "SELECT COUNT(*) FROM points_transactions WHERE customer_id = %s "
            "AND transaction_type = 'EXPIRY'",
            (self.customer_id,),
        )[0][0], 1)

    def test_expiry_processes_multiple_due_lots_but_not_future_lots(self):
        today_midnight = datetime.combine(date.today(), datetime.min.time())
        self._add_points_lot(5, expires_at=today_midnight, suffix="DUE1")
        self._add_points_lot(7, expires_at=today_midnight - timedelta(days=1), suffix="DUE2")
        self._add_points_lot(
            11, expires_at=today_midnight + timedelta(days=1), suffix="FUTURE"
        )

        result = expire_points(as_of=date.today())
        self.assertEqual((result["lots_expired"], result["points_expired"]), (2, 12))
        self.assertEqual(self._query(
            "SELECT current_points FROM loyalty_accounts WHERE customer_id = %s",
            (self.customer_id,),
        )[0][0], 11)
        self.assertEqual(self._query(
            "SELECT COUNT(*) FROM point_lots WHERE customer_id = %s "
            "AND points_remaining > 0", (self.customer_id,),
        )[0][0], 1)

    def test_expiry_date_excludes_next_midnight_until_exact_instant(self):
        boundary = datetime(2030, 1, 2, 0, 0, 0)
        transaction_id = self._add_points_lot(
            4, expires_at=boundary, suffix="BOUNDARY"
        )
        expire_points(as_of=date(2030, 1, 1))  # Other customers may also be overdue.
        lot_id = self._query(
            "SELECT lot_id FROM point_lots WHERE source_transaction_id = %s",
            (transaction_id,),
        )[0][0]
        self.assertEqual(self._query(
            "SELECT points_remaining FROM point_lots WHERE lot_id = %s", (lot_id,),
        )[0][0], 4)
        expire_points(as_of=boundary)
        self.assertEqual(self._query(
            "SELECT points_remaining FROM point_lots WHERE lot_id = %s", (lot_id,),
        )[0][0], 0)

    def test_tier_uses_qualifying_net_spend_then_progresses(self):
        discounted = self._create_order(
            6000, line=(1, 6000, 2000, True), suffix="A"
        )
        award_points(discounted)
        account = self._query(
            "SELECT tier_id, qualifying_spend FROM loyalty_accounts WHERE customer_id = %s",
            (self.customer_id,),
        )[0]
        self.assertEqual(account, (1, Decimal("4000.00")))

        threshold_order = self._create_order(1000, suffix="B")
        award_points(threshold_order)
        self.assertEqual(self._query(
            "SELECT tier_id FROM loyalty_accounts WHERE customer_id = %s",
            (self.customer_id,),
        )[0][0], 2)

        silver_order = self._create_order(200, suffix="C")
        result = award_points(silver_order)
        self.assertEqual(result["points"], 3)
        history = self._query(
            "SELECT old_tier_id, new_tier_id, qualifying_spend "
            "FROM account_tier_history WHERE customer_id = %s",
            (self.customer_id,),
        )
        self.assertIn((1, 2, Decimal("5000.00")), history)
        self.assertEqual(self._query(
            "SELECT COUNT(*) FROM audit_events WHERE entity_type = 'loyalty_account' "
            "AND entity_id = %s AND event_type = 'TIER_CHANGED'",
            (str(self.customer_id),),
        )[0][0], 1)

    def test_concurrent_redemptions_allow_at_most_one_to_spend_same_balance(self):
        self._add_points_lot(
            10, expires_at=datetime.now() + timedelta(days=30)
        )
        barrier = threading.Barrier(2)

        def attempt():
            barrier.wait(timeout=10)
            try:
                redeem_points(self.customer_id, 8)
                return "accepted"
            except ValueError:
                return "rejected"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _index: attempt(), range(2)))
        self.assertCountEqual(outcomes, ["accepted", "rejected"])
        self.assertEqual(self._query(
            "SELECT current_points FROM loyalty_accounts WHERE customer_id = %s",
            (self.customer_id,),
        )[0][0], 2)
        self.assertEqual(self._query(
            "SELECT COUNT(*) FROM redemptions WHERE customer_id = %s",
            (self.customer_id,),
        )[0][0], 1)

    def test_concurrent_awards_serialize_tier_rule_selection_per_customer(self):
        baseline = self._create_order(4000, suffix="A")
        award_points(baseline)
        first = self._create_order(1000, suffix="B")
        second = self._create_order(1000, suffix="C")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(award_points, [first, second]))
        self.assertEqual(sum(result["points"] for result in results), 25)
        rates = self._query(
            "SELECT earning_rate FROM points_transactions "
            "WHERE customer_id = %s AND transaction_type = 'ACCRUAL' "
            "ORDER BY transaction_date, transaction_id",
            (self.customer_id,),
        )
        self.assertEqual(rates, [(Decimal("1.00"),), (Decimal("1.00"),),
                                 (Decimal("1.50"),)])
        account = self._query(
            "SELECT tier_id, qualifying_spend, current_points "
            "FROM loyalty_accounts WHERE customer_id = %s", (self.customer_id,),
        )[0]
        self.assertEqual(account, (2, Decimal("6000.00"), 65))

    def test_validator_detects_a_balance_ledger_mismatch(self):
        connection = get_connection()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "UPDATE loyalty_accounts SET current_points = 1 WHERE customer_id = %s",
                (self.customer_id,),
            )
            connection.commit()
        finally:
            cursor.close()
            connection.close()

        try:
            report = validate_data()
            self.assertFalse(report["passed"])
            mismatch = report["checks"]["balance_ledger_mismatches"]
            self.assertIn(
                self.customer_id,
                [row["customer_id"] for row in mismatch["sample"]],
            )
        finally:
            connection = get_connection()
            cursor = connection.cursor()
            try:
                cursor.execute(
                    "UPDATE loyalty_accounts SET current_points = 0 "
                    "WHERE customer_id = %s",
                    (self.customer_id,),
                )
                connection.commit()
            finally:
                cursor.close()
                connection.close()

    def test_validator_detects_missing_return_reversal(self):
        order_id = self._create_order(500)
        award_points(order_id)
        return_id = f"RET-T-{self.customer_id}-MISSING"
        connection = get_connection()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "INSERT INTO order_returns "
                "(return_id, order_id, return_date, refund_amount, return_status) "
                "VALUES (%s, %s, NOW(6), 500, 'REQUESTED')",
                (return_id, order_id),
            )
            connection.commit()
        finally:
            cursor.close()
            connection.close()
        reversal = complete_return(return_id)

        connection = get_connection()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "DELETE FROM reversal_lot_allocations "
                "WHERE reversal_transaction_id = %s", (reversal["transaction_id"],),
            )
            cursor.execute(
                "UPDATE point_lots SET points_remaining = 5, lot_status = 'ACTIVE' "
                "WHERE source_transaction_id = (SELECT transaction_id "
                "FROM points_transactions WHERE order_id = %s "
                "AND transaction_type = 'ACCRUAL')", (order_id,),
            )
            cursor.execute(
                "DELETE FROM points_transactions WHERE transaction_id = %s",
                (reversal["transaction_id"],),
            )
            cursor.execute(
                "UPDATE loyalty_accounts SET current_points = 5 WHERE customer_id = %s",
                (self.customer_id,),
            )
            connection.commit()
        finally:
            cursor.close()
            connection.close()

        report = validate_data()
        self.assertFalse(report["passed"])
        check = report["checks"]["completed_returns_missing_or_wrong_reversals"]
        self.assertTrue(any(row["return_id"] == return_id for row in check["sample"]))

    def test_database_rejects_invalid_order_status_and_orphan_customer(self):
        connection = get_connection()
        cursor = connection.cursor()
        try:
            with self.assertRaises(mysql.connector.Error):
                cursor.execute(
                    "INSERT INTO orders "
                    "(order_id, customer_id, order_date, total_amount, order_status) "
                    "VALUES (%s, %s, NOW(6), 1, 'NOT_A_STATUS')",
                    (f"ORD-T-{self.customer_id}-BAD", self.customer_id),
                )
            connection.rollback()
            with self.assertRaises(mysql.connector.IntegrityError):
                cursor.execute(
                    "INSERT INTO orders "
                    "(order_id, customer_id, order_date, total_amount, order_status) "
                    "VALUES (%s, 99999999, NOW(6), 1, 'COMPLETED')",
                    (f"ORD-T-{self.customer_id}-ORPHAN",),
                )
            connection.rollback()
        finally:
            cursor.close()
            connection.close()

    def test_generated_csv_loader_loads_into_a_fresh_empty_schema(self):
        original_database = os.environ.get("LOYALTY_DB_NAME")
        load_database = "loyalty_load_test_" + uuid.uuid4().hex[:10]
        admin = mysql.connector.connect(**_connection_settings())
        admin_cursor = admin.cursor()
        schema_connection = None
        load_connection = None
        original_local_infile = None
        changed_local_infile = False
        try:
            admin_cursor.execute("SHOW GLOBAL VARIABLES LIKE 'local_infile'")
            local_infile_setting = admin_cursor.fetchone()
            if local_infile_setting is None:
                self.skipTest("This MySQL server does not expose local_infile.")
            original_local_infile = str(local_infile_setting[1]).upper()
            if original_local_infile in ("OFF", "0", "FALSE"):
                try:
                    admin_cursor.execute("SET GLOBAL local_infile = ON")
                    changed_local_infile = True
                except mysql.connector.Error as error:
                    self.skipTest(
                        "CSV loader integration needs local_infile enabled on the "
                        f"server; this account cannot enable it: {error.msg}"
                    )
            admin_cursor.execute(
                f"CREATE DATABASE `{load_database}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
            )
            os.environ["LOYALTY_DB_NAME"] = load_database
            schema_connection = mysql.connector.connect(
                **_connection_settings(load_database)
            )
            _execute_script(
                schema_connection,
                (ROOT / "sql" / "schema.sql").read_text(encoding="utf-8"),
                load_database,
            )
            schema_connection.commit()
            schema_connection.close()
            schema_connection = None

            with tempfile.TemporaryDirectory(prefix="loyalty-ledger-load-") as folder:
                output_dir = Path(folder) / "dataset"
                generate_manifest = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "src" / "generate_data.py"),
                        "--customers", "20",
                        "--orders", "100",
                        "--seed", "20261022",
                        "--output", str(output_dir),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(generate_manifest.returncode, 0, generate_manifest.stderr)
                load_connection = mysql.connector.connect(
                    **_connection_settings(load_database), allow_local_infile=True
                )
                _execute_script(
                    load_connection,
                    (output_dir / "load_generated_data.sql").read_text(encoding="utf-8"),
                    load_database,
                )
                load_connection.commit()
                load_connection.close()
                load_connection = None

                generated_manifest = json.loads(
                    (output_dir / "manifest.json").read_text(encoding="utf-8")
                )
                report = validate_data(
                    as_of=date.fromisoformat(generated_manifest["as_of_date"])
                )
                self.assertTrue(report["passed"], report["checks"])
                connection = get_connection()
                cursor = connection.cursor()
                try:
                    expected = {
                        "customers": 20,
                        "orders": 100,
                        "points_transactions": 300,
                    }
                    for table, count in expected.items():
                        cursor.execute(f"SELECT COUNT(*) FROM `{table}`")
                        self.assertEqual(cursor.fetchone()[0], count, table)
                finally:
                    cursor.close()
                    connection.close()
        finally:
            if schema_connection is not None:
                schema_connection.close()
            if load_connection is not None:
                load_connection.close()
            if admin_cursor:
                if changed_local_infile and original_local_infile is not None:
                    admin_cursor.execute("SET GLOBAL local_infile = OFF")
                admin_cursor.execute(f"DROP DATABASE IF EXISTS `{load_database}`")
                admin_cursor.close()
            admin.close()
            if original_database is None:
                os.environ.pop("LOYALTY_DB_NAME", None)
            else:
                os.environ["LOYALTY_DB_NAME"] = original_database


if __name__ == "__main__":
    unittest.main(verbosity=2)
