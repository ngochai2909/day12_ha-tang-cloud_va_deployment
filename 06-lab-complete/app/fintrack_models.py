from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


TransactionType = Literal["expense", "income"]


class ParseMeta(BaseModel):
    confidence: float = Field(ge=0.0, le=1.0)
    needs_clarification: bool = False
    notes: list[str] = Field(default_factory=list)


class ParsedTransaction(BaseModel):
    type: TransactionType
    amount: float = Field(gt=0)
    currency: str = "VND"
    wallet_name: Optional[str] = None
    category: str
    description: str
    raw_text: str
    occurred_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    meta: ParseMeta


class Wallet(BaseModel):
    id: str
    user_id: str
    name: str
    balance: float
    currency: str = "VND"
    updated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")


class TransactionRecord(BaseModel):
    id: str
    user_id: str
    type: TransactionType
    amount: float
    currency: str = "VND"
    wallet_id: str
    wallet_name: str
    category: str
    description: str
    raw_text: str
    occurred_at: str
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")


class DashboardSummary(BaseModel):
    user_id: str
    currency: str = "VND"
    total_balance: float
    total_income: float
    total_expense: float
    net: float
    by_category: dict[str, float]
    by_wallet: dict[str, float]
    transaction_count: int

