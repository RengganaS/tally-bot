from typing import Literal, Optional
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, Field
from src.config import GEMINI_API_KEY
from datetime import datetime
from zoneinfo import ZoneInfo

client = genai.Client(api_key=GEMINI_API_KEY)

MODEL_CHAIN = [
    "gemini-3.5-flash-lite",
    "gemini-3.6-flash",
    "gemini-3.8-flash",
]

class ParsedFinanceIntent(BaseModel):
    intent: Literal[
        "record_transaction",
        "record_transfer",
        "check_balance",
        "check_history",
        "check_summary",
        "create_account",
        "create_category",
        "unknown",
    ] = Field(description="The user's primary intent.")
    tx_type: Optional[Literal["expense", "income"]] = Field(
        default=None,
        description="Set to 'expense' or 'income' when intent is 'record_transaction' or 'create_category'.",
    )
    amount: Optional[float] = Field(
        default=None,
        description="Numeric amount in IDR (or initial balance when creating an account; defaults to 0 if omitted).",
    )
    account: Optional[str] = Field(
        default=None,
        description="Source/primary account name from Available accounts, or new account name when intent is 'create_account'.",
    )
    account_type: Optional[Literal["bank", "ewallet", "cash", "credit"]] = Field(
        default=None,
        description="Account type when intent is 'create_account' (infer 'ewallet' for GoPay/OVO/Dana/ShopeePay, 'bank' for BCA/Mandiri/BRI/BNI/SeaBank/Jago, etc.).",
    )
    to_account: Optional[str] = Field(
        default=None,
        description="Destination account name from Available accounts when intent is 'record_transfer'.",
    )
    category: Optional[str] = Field(
        default=None,
        description="Matched category name from Available categories, or new category name when intent is 'create_category'.",
    )
    note: Optional[str] = Field(
        default=None,
        description="Short clean description (e.g., 'Lunch', 'Topup GoPay', 'ATM withdrawal').",
    )
    limit: Optional[int] = Field(
        default=None,
        description="Number of recent transactions requested when intent is 'check_history' (default 5 if unspecified).",
    )
    year: Optional[int] = Field(
        default=None,
        description="4-digit year when intent is 'check_summary' (leave null to default to current year).",
    )
    month: Optional[int] = Field(
        default=None,
        description="Month number 1-12 when intent is 'check_summary' (e.g., 'Oktober' -> 10; leave null if 'bulan ini' / current month).",
    )
    reply_hint: Optional[str] = Field(
        default=None,
        description="Helpful clarification message if intent is 'unknown' or required info is missing.",
    )

def parse_message(
        user_text: str,
        available_accounts: list[str],
        available_categories: list[str],
) -> ParsedFinanceIntent:
    now_wib = datetime.now(ZoneInfo("Asia/Jakarta"))
    if now_wib.month == 1:
        last_month_year, last_month = now_wib.year - 1, 12
    else:
        last_month_year, last_month = now_wib.year, now_wib.month - 1
    
    system_prompt = f"""
        You are Tally, a personal finance parser. Convert the user's casual English or Indonesian chat message into structured financial data.

        Reference Dates (WIB):
        - Today: {now_wib.strftime("%Y-%m-%d")}
        - "bulan ini" / "this month": year={now_wib.year}, month={now_wib.month}
        - "bulan lalu" / "last month": year={last_month_year}, month={last_month}

        Available accounts: {", ".join(available_accounts)}
        Available categories: {", ".join(available_categories)}

        Rules:
        1. Convert Indonesian/casual currency shorthand accurately: '30k' or '30rb' = 30000, '1.5jt' = 1500000.
        2. Match the user's mentioned bank/wallet to the closest name in Available accounts (case-insensitive). If no account is mentioned when logging a transaction, set intent to 'unknown' and ask which account they used in reply_hint.
        3. Match the transaction to the best category in Available categories (default to 'Other' if unsure).
        4. If the user asks how much money they have, what their balance is, or 'cek saldo', set intent to 'check_balance'.
        5. If the message is unrelated chatter or missing an amount, set intent to 'unknown' and explain briefly in reply_hint.
        6. If the user moves money between two of their own accounts (e.g., 'transfer 500k dari Mandiri ke GoPay', 'topup gopay 200rb dari BCA', 'tarik tunai 100k dari Mandiri ke Cash'), set intent to 'record_transfer', put the source account in 'account', and put the receiving account in 'to_account'. If either account is missing, set intent to 'unknown' and ask in reply_hint.
        7. If the user wants to add/create a new account or wallet (e.g., 'tambah akun Dana saldo 100k', 'add account SeaBank'), set intent to 'create_account', put the new name in 'account', infer 'account_type' ('bank', 'ewallet', 'cash', or 'credit'), and put any starting balance in 'amount' (0 if not mentioned). If the user wants to add/create a new category (e.g., 'tambah kategori Coffee', 'add income category Bonus'), set intent to 'create_category', put the new name in 'category', and set 'tx_type' to 'income' or 'expense' (default 'expense').
        8. If the user asks to see recent transactions, transaction history, or mutasi (e.g., 'cek transaksi terakhir', 'show last 10 transactions', 'riwayat pengeluaran'), set intent to 'check_history' and extract any requested count into 'limit' (default 5).
        9. If the user asks for a monthly spending summary, expense report, or category breakdown (e.g., 'ringkasan bulan ini', 'rekap pengeluaran', 'breakdown kategori Oktober', 'rekap bulan lalu'), set intent to 'check_summary' and ALWAYS populate both 'year' and 'month' (1-12) using the Reference Dates above (for 'bulan lalu' / 'last month', use year={last_month_year}, month={last_month}).
    """

    last_error: Exception | None = None
    for model_name in MODEL_CHAIN:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=user_text,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    response_mime_type="application/json",
                    response_schema=ParsedFinanceIntent,
                    temperature=0.1,
                ),
            )
            if not response.text:
                raise ValueError("Gemini returned an empty response.")
            return ParsedFinanceIntent.model_validate_json(response.text)
        
        except errors.APIError as exc:
            last_error = exc
            if exc.code in (429, 503):
                print(f"[LLM Fallback] {model_name} unavailable ({exc.code}), trying next model...")
                continue
            raise

    raise RuntimeError(f"All Gemini models temporarily unavailable: {last_error}")