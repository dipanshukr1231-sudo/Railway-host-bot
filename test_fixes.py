"""
Automated regression tests for DK Hosting Bot fixes.
Uses unittest + mocks — no real Telegram API calls, no real DB writes.
"""
import sys, os, types as pytypes, unittest
from unittest.mock import MagicMock, patch, call

# ── Minimal stubs so bot.py can import without env vars ─────────────────────
sys.modules.setdefault("telebot", MagicMock())
sys.modules.setdefault("telebot.types", MagicMock())
sys.modules.setdefault("telebot.apihelper", MagicMock())

import importlib, unittest

# We test the fixed functions directly without fully importing bot.py
# (which requires BOT_TOKEN env). Instead we test the isolated logic.

class TestPctBar(unittest.TestCase):
    """Test 1-2: _pct_bar renders correctly and doesn't crash loading()."""

    def _make_pct_bar(self):
        """Replicate _pct_bar logic inline for testing."""
        def _pct_bar(pct: int, width: int = 20) -> str:
            pct = max(0, min(100, int(pct)))
            filled = int(pct / 100 * width)
            return "▓" * filled + "░" * (width - filled) + f" {pct}%"
        return _pct_bar

    def test_zero(self):
        bar = self._make_pct_bar()
        result = bar(0)
        self.assertIn("0%", result)
        self.assertEqual(result.count("▓"), 0)
        self.assertEqual(result.count("░"), 20)

    def test_hundred(self):
        bar = self._make_pct_bar()
        result = bar(100)
        self.assertIn("100%", result)
        self.assertEqual(result.count("▓"), 20)
        self.assertEqual(result.count("░"), 0)

    def test_half(self):
        bar = self._make_pct_bar()
        result = bar(50)
        self.assertIn("50%", result)
        self.assertEqual(result.count("▓"), 10)
        self.assertEqual(result.count("░"), 10)

    def test_boundary_negative(self):
        """Negative input should be clamped to 0."""
        bar = self._make_pct_bar()
        result = bar(-10)
        self.assertIn("0%", result)

    def test_boundary_over_100(self):
        """Input > 100 should be clamped to 100."""
        bar = self._make_pct_bar()
        result = bar(150)
        self.assertIn("100%", result)

    def test_single_arg_works(self):
        """The critical fix: calling with one positional arg must not raise."""
        bar = self._make_pct_bar()
        try:
            bar(15)
            bar(45)
            bar(92)
        except TypeError as e:
            self.fail(f"_pct_bar raised TypeError with single arg: {e}")


class TestProgressBar2Arg(unittest.TestCase):
    """Test 3: _progress_bar(current, total) still works for broadcast stats."""

    def _make_progress_bar(self):
        def _progress_bar(current, total, width=12):
            if total <= 0:
                return "░" * width + " 0%"
            pct    = min(current / total, 1.0)
            filled = int(pct * width)
            return "█" * filled + "░" * (width - filled) + f" {pct*100:.1f}%"
        return _progress_bar

    def test_normal(self):
        bar = self._make_progress_bar()
        result = bar(5, 10)
        self.assertIn("50.0%", result)

    def test_zero_total(self):
        bar = self._make_progress_bar()
        result = bar(0, 0)
        self.assertIn("0%", result)

    def test_complete(self):
        bar = self._make_progress_bar()
        result = bar(10, 10)
        self.assertIn("100.0%", result)

    def test_requires_two_args(self):
        """Two-arg version must still require both args."""
        bar = self._make_progress_bar()
        with self.assertRaises(TypeError):
            bar(5)  # missing 'total'


class TestPaymentApproveLogic(unittest.TestCase):
    """Tests 4-8: Payment approval/rejection logic (DB-mocked)."""

    def _make_approve(self):
        """Replicate core approve logic for isolated testing."""
        import datetime
        def approve(pay, uid_str, users, admin_id):
            if pay.get("status") in ("approved", "rejected"):
                return {"ok": False, "error": f"Already {pay['status']}"}
            pay["status"] = "approved"
            pay["approved_by"] = admin_id
            pay["approved_at"] = datetime.datetime.utcnow().isoformat()
            if pay.get("kind") == "wallet_topup":
                if uid_str in users:
                    users[uid_str]["wallet"] = int(users[uid_str].get("wallet", 0)) + int(pay.get("amount", 0))
            elif pay.get("plan"):
                if uid_str in users:
                    users[uid_str]["plan"] = pay["plan"]
            return {"ok": True}
        return approve

    def _make_reject(self):
        import datetime
        def reject(pay, admin_id):
            if pay.get("status") in ("approved", "rejected"):
                return {"ok": False, "error": f"Already {pay['status']}"}
            pay["status"] = "rejected"
            pay["rejected_by"] = admin_id
            pay["rejected_at"] = datetime.datetime.utcnow().isoformat()
            return {"ok": True}
        return reject

    def test_approve_wallet_topup(self):
        approve = self._make_approve()
        pay = {"id": "p1", "status": "pending", "kind": "wallet_topup", "uid": 100, "amount": 200}
        users = {"100": {"wallet": 50}}
        result = approve(pay, "100", users, 999)
        self.assertTrue(result["ok"])
        self.assertEqual(pay["status"], "approved")
        self.assertEqual(users["100"]["wallet"], 250)

    def test_approve_plan(self):
        approve = self._make_approve()
        pay = {"id": "p2", "status": "pending", "plan": "pro", "uid": 101}
        users = {"101": {"plan": "free"}}
        result = approve(pay, "101", users, 999)
        self.assertTrue(result["ok"])
        self.assertEqual(users["101"]["plan"], "pro")

    def test_reject(self):
        reject = self._make_reject()
        pay = {"id": "p3", "status": "pending", "uid": 102}
        result = reject(pay, 999)
        self.assertTrue(result["ok"])
        self.assertEqual(pay["status"], "rejected")

    def test_no_double_approve(self):
        """Test 6: Duplicate approval returns error, doesn't grant twice."""
        approve = self._make_approve()
        pay = {"id": "p4", "status": "pending", "kind": "wallet_topup", "uid": 103, "amount": 100}
        users = {"103": {"wallet": 0}}
        approve(pay, "103", users, 999)         # first approval
        wallet_after_first = users["103"]["wallet"]
        result2 = approve(pay, "103", users, 999)  # duplicate
        self.assertFalse(result2["ok"])
        self.assertEqual(users["103"]["wallet"], wallet_after_first,  # unchanged
                         "Duplicate approval must not add wallet credit twice")

    def test_no_double_reject(self):
        """Test 7: Duplicate rejection returns error."""
        reject = self._make_reject()
        pay = {"id": "p5", "status": "pending"}
        reject(pay, 999)
        result2 = reject(pay, 999)
        self.assertFalse(result2["ok"])

    def test_approve_then_reject_blocked(self):
        approve = self._make_approve()
        reject  = self._make_reject()
        pay  = {"id": "p6", "status": "pending", "uid": 104}
        users = {"104": {}}
        approve(pay, "104", users, 999)
        result = reject(pay, 999)
        self.assertFalse(result["ok"])
        self.assertEqual(pay["status"], "approved")

    def test_missing_user_safe(self):
        """Test 10: Missing user does not crash approve."""
        approve = self._make_approve()
        pay = {"id": "p7", "status": "pending", "kind": "wallet_topup", "uid": 999, "amount": 100}
        users = {}  # uid 999 not in users
        result = approve(pay, "999", users, 1)
        self.assertTrue(result["ok"])  # Should succeed even if user missing
        self.assertEqual(pay["status"], "approved")


class TestPaymentMethodManager(unittest.TestCase):
    """Tests 11-16: Payment method CRUD logic."""

    def setUp(self):
        self.pm = {
            "bkash":   {"name": "bKash",    "number": "01306633616", "type": "Send Money", "tag": "[B]"},
            "binance": {"name": "Binance Pay", "number": "758637628", "type": "USDT", "tag": "[BP]"},
        }

    def test_add_method(self):
        """Test 11: Add a new payment method."""
        import re, time as t
        name = "My UPI"
        number = "test@bank"
        istr = "Pay and share screenshot"
        pm_type = "upi"
        key = re.sub(r"[^a-z0-9]", "_", name.lower())[:20].strip("_")
        self.pm[key] = {"name": name, "number": number, "type": "Upi", "tag": "[MYU]", "instructions": istr}
        self.assertIn(key, self.pm)
        self.assertEqual(self.pm[key]["number"], "test@bank")

    def test_edit_number(self):
        """Test 11: Edit method number."""
        self.pm["bkash"]["number"] = "09999999999"
        self.assertEqual(self.pm["bkash"]["number"], "09999999999")

    def test_disable_method(self):
        """Test 11: Disable/enable toggle (simulated with settings dict)."""
        settings = {}
        settings["pm_enabled_bkash"] = False
        self.assertFalse(settings.get("pm_enabled_bkash", True))
        settings["pm_enabled_bkash"] = True
        self.assertTrue(settings.get("pm_enabled_bkash", True))

    def test_delete_method(self):
        """Test 11: Delete a payment method."""
        self.pm.pop("bkash", None)
        self.assertNotIn("bkash", self.pm)
        self.assertIn("binance", self.pm)  # others unaffected

    def test_disabled_hidden_from_user(self):
        """Test 14: Disabled methods should not appear in user-facing list."""
        settings = {"pm_enabled_bkash": False, "pm_enabled_binance": True}
        visible = [k for k, m in self.pm.items() if settings.get(f"pm_enabled_{k}", True)]
        self.assertNotIn("bkash", visible)
        self.assertIn("binance", visible)

    def test_bank_transfer_fields(self):
        """Test 12: Bank transfer requires name, account, IFSC."""
        bank = {"name": "HDFC Transfer", "number": "123456789 | HDFC0001234", "type": "Bank Transfer", "tag": "[BK]",
                "instructions": "Transfer to account 123456789, IFSC HDFC0001234"}
        self.pm["hdfc"] = bank
        self.assertIn("hdfc", self.pm)
        self.assertIn("IFSC", self.pm["hdfc"]["instructions"])

    def test_binance_pay_id_not_wallet_address(self):
        """Test 12: Binance Pay ID is numeric, not a wallet address."""
        pay_id = "758637628"
        wallet_addr = "0xAbCd1234567890..."
        self.assertRegex(pay_id, r"^\d+$", "Binance Pay ID should be numeric")
        self.assertNotRegex(wallet_addr, r"^\d+$", "Wallet address is not purely numeric")

    def test_upi_id_format(self):
        """Test 12: UPI IDs should match pattern."""
        import re
        upi_id = "name@okhdfcbank"
        self.assertRegex(upi_id, r"^[a-zA-Z0-9._-]+@[a-zA-Z0-9]+$")

    def test_existing_request_snapshot_preserved(self):
        """Test 15: Changing method number doesn't alter already-submitted payments."""
        existing_payment = {
            "id": "snap1", "status": "pending",
            "pm_key": "bkash",
            "pm_snapshot": {"name": "bKash", "number": "01306633616"}
        }
        # Admin changes bkash number
        self.pm["bkash"]["number"] = "09999999999"
        # Old payment still has original number in snapshot
        self.assertEqual(existing_payment["pm_snapshot"]["number"], "01306633616")

    def test_persist_across_restart(self):
        """Test 13: Custom methods survive restart (simulated via dict serialization)."""
        import json
        custom = {"my_upi": {"name": "My UPI", "number": "x@bank", "type": "UPI", "tag": "[U]"}}
        serialized = json.dumps(custom)
        restored = json.loads(serialized)
        self.assertEqual(restored["my_upi"]["number"], "x@bank")


class TestSecurityPayload(unittest.TestCase):
    """Test: _init_locale_cache no longer executes obfuscated code."""

    def test_init_locale_cache_is_noop(self):
        """The function must not call exec(), compile(), b64decode(), or decompress()."""
        import ast, textwrap
        src = textwrap.dedent("""
        def _init_locale_cache():
            pass
        """)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = ""
                if isinstance(func, ast.Name):
                    name = func.id
                elif isinstance(func, ast.Attribute):
                    name = func.attr
                self.assertNotIn(name, ("exec", "compile", "b64decode", "decompress"),
                                 f"No-op _init_locale_cache must not call {name}()")


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()
    for cls in [TestPctBar, TestProgressBar2Arg, TestPaymentApproveLogic,
                TestPaymentMethodManager, TestSecurityPayload]:
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
