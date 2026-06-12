import json
import os
import uuid
from datetime import datetime, timezone

from app.fintrack_models import DashboardSummary, ParsedTransaction, TransactionRecord, Wallet
from app.redis_client import get_redis


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FinTrackStore:
    def __init__(self, data_dir: str, seed_dir: str | None = None):
        self.data_dir = data_dir
        self.seed_dir = seed_dir
        self.wallets_file = os.path.join(data_dir, "wallets.json")
        self.transactions_file = os.path.join(data_dir, "transactions.json")
        self._ensure_seed_data("demo-user")

    def _ensure_seed_data(self, user_id: str) -> None:
        seed_wallets = os.path.join(self.seed_dir, "wallets.json") if self.seed_dir else None
        seed_transactions = os.path.join(self.seed_dir, "transactions.json") if self.seed_dir else None
        if seed_wallets and os.path.exists(seed_wallets):
            with open(seed_wallets, "r", encoding="utf-8") as f:
                wallets = [w for w in json.load(f) if w.get("user_id") == user_id]
        else:
            wallets = [
                {
                    "id": "wal_daily",
                    "user_id": user_id,
                    "name": "vi hang ngay",
                    "balance": 2_500_000.0,
                    "currency": "VND",
                    "updated_at": _utc_now(),
                },
                {
                    "id": "wal_bank",
                    "user_id": user_id,
                    "name": "vi ngan hang",
                    "balance": 12_000_000.0,
                    "currency": "VND",
                    "updated_at": _utc_now(),
                },
            ]

        if seed_transactions and os.path.exists(seed_transactions):
            with open(seed_transactions, "r", encoding="utf-8") as f:
                txs = [t for t in json.load(f) if t.get("user_id") == user_id]
        else:
            txs = []

        r = get_redis()
        wallets_key = self._wallets_key(user_id)
        tx_key = self._transactions_key(user_id)
        if not r.exists(wallets_key):
            r.set(wallets_key, json.dumps(wallets, ensure_ascii=False))
        if not r.exists(tx_key):
            r.set(tx_key, json.dumps(txs, ensure_ascii=False))

    def _wallets_key(self, user_id: str) -> str:
        return f"fintrack:wallets:{user_id}"

    def _transactions_key(self, user_id: str) -> str:
        return f"fintrack:transactions:{user_id}"

    def _history_key(self, user_id: str) -> str:
        return f"fintrack:history:{user_id}"

    def _read_wallets(self, user_id: str) -> list[dict]:
        r = get_redis()
        raw = r.get(self._wallets_key(user_id))
        if not raw:
            self._ensure_seed_data(user_id)
            raw = r.get(self._wallets_key(user_id))
        return json.loads(raw) if raw else []

    def _write_wallets(self, user_id: str, wallets: list[dict]) -> None:
        get_redis().set(self._wallets_key(user_id), json.dumps(wallets, ensure_ascii=False))

    def _read_transactions(self, user_id: str) -> list[dict]:
        r = get_redis()
        raw = r.get(self._transactions_key(user_id))
        if not raw:
            self._ensure_seed_data(user_id)
            raw = r.get(self._transactions_key(user_id))
        return json.loads(raw) if raw else []

    def _write_transactions(self, user_id: str, txs: list[dict]) -> None:
        get_redis().set(self._transactions_key(user_id), json.dumps(txs, ensure_ascii=False))

    def get_wallets(self, user_id: str) -> list[Wallet]:
        rows = self._read_wallets(user_id)
        return [Wallet(**row) for row in rows if row.get("user_id") == user_id]

    def get_transactions(self, user_id: str, limit: int = 50) -> list[TransactionRecord]:
        rows = [t for t in self._read_transactions(user_id) if t.get("user_id") == user_id]
        rows.sort(key=lambda t: t["created_at"], reverse=True)
        return [TransactionRecord(**row) for row in rows[:limit]]

    def _match_wallet(self, user_id: str, wallet_name: str | None) -> Wallet:
        wallets = self.get_wallets(user_id)
        if not wallets:
            raise ValueError("User has no wallets configured.")
        if not wallet_name:
            return wallets[0]

        wanted = wallet_name.strip().lower()
        for wallet in wallets:
            if wanted == wallet.name.lower() or wanted in wallet.name.lower():
                return wallet
        raise ValueError(f"Wallet '{wallet_name}' is not valid for user '{user_id}'.")

    def create_transaction(self, user_id: str, parsed: ParsedTransaction) -> tuple[TransactionRecord, Wallet]:
        if parsed.meta.needs_clarification:
            raise ValueError("Cannot create transaction because parser needs clarification.")

        wallet = self._match_wallet(user_id, parsed.wallet_name)
        if parsed.amount <= 0:
            raise ValueError("Amount must be greater than zero.")

        if parsed.type == "expense" and wallet.balance < parsed.amount:
            raise ValueError(f"Insufficient balance in wallet '{wallet.name}'.")

        tx_id = f"tx_{uuid.uuid4().hex[:10]}"
        tx = TransactionRecord(
            id=tx_id,
            user_id=user_id,
            type=parsed.type,
            amount=parsed.amount,
            currency=parsed.currency,
            wallet_id=wallet.id,
            wallet_name=wallet.name,
            category=parsed.category,
            description=parsed.description,
            raw_text=parsed.raw_text,
            occurred_at=parsed.occurred_at,
        )

        transactions = self._read_transactions(user_id)
        transactions.append(tx.model_dump())
        self._write_transactions(user_id, transactions)

        wallets = self._read_wallets(user_id)
        new_balance = wallet.balance + parsed.amount if parsed.type == "income" else wallet.balance - parsed.amount
        for item in wallets:
            if item["id"] == wallet.id and item["user_id"] == user_id:
                item["balance"] = round(new_balance, 2)
                item["updated_at"] = _utc_now()
                wallet = Wallet(**item)
                break
        self._write_wallets(user_id, wallets)
        self.record_history(
            user_id=user_id,
            question=parsed.raw_text,
            answer=f"Recorded {parsed.type} {parsed.amount:.0f} {parsed.currency} in {wallet.name}",
        )
        return tx, wallet

    def build_dashboard(self, user_id: str) -> DashboardSummary:
        wallets = self.get_wallets(user_id)
        transactions = self.get_transactions(user_id, limit=5_000)

        by_category: dict[str, float] = {}
        by_wallet: dict[str, float] = {}
        total_income = 0.0
        total_expense = 0.0

        for tx in transactions:
            sign_amount = tx.amount if tx.type == "income" else -tx.amount
            by_category[tx.category] = round(by_category.get(tx.category, 0.0) + sign_amount, 2)
            by_wallet[tx.wallet_name] = round(by_wallet.get(tx.wallet_name, 0.0) + sign_amount, 2)
            if tx.type == "income":
                total_income += tx.amount
            else:
                total_expense += tx.amount

        total_balance = sum(w.balance for w in wallets)
        return DashboardSummary(
            user_id=user_id,
            currency=wallets[0].currency if wallets else "VND",
            total_balance=round(total_balance, 2),
            total_income=round(total_income, 2),
            total_expense=round(total_expense, 2),
            net=round(total_income - total_expense, 2),
            by_category=by_category,
            by_wallet=by_wallet,
            transaction_count=len(transactions),
        )

    def record_history(self, user_id: str, question: str, answer: str) -> None:
        r = get_redis()
        payload = {
            "question": question,
            "answer": answer,
            "timestamp": _utc_now(),
        }
        key = self._history_key(user_id)
        r.rpush(key, json.dumps(payload, ensure_ascii=False))
        r.ltrim(key, -50, -1)

    def get_history(self, user_id: str, limit: int = 20) -> list[dict]:
        r = get_redis()
        key = self._history_key(user_id)
        rows = r.lrange(key, -max(1, limit), -1)
        return [json.loads(x) for x in rows]

