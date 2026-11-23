from typing import Literal, Optional
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, Field
from src.config import GEMINI_API_KEY

client = genai.Client(api_key=GEMINI_API_KEY)

MODEL_CHAIN = [
    "gemini-3.5-flash-lite",
    "gemini-3.6-flash",
    "gemini-3.8-flash",
]

class ParsedFinanceIntent(BaseModel):
    intent: Literal["record_transaction", "check_balance", "unknown"] = Field(
        description="The user's primary intent."
    )
    tx_type: Optional[Literal["expense", "income"]] = Field(
        default=None,
        description="Set to 'expense' or 'income' when intent is 'record_transaction'.",
    )
    amount: Optional[float] = Field(
        default=None,
        description="Numeric amount in IDR (convert shorthand like '30k' -> 30000, '1.5jt' -> 1500000).",
    )
    account: Optional[str] = Field(
        default=None,
        description="Matched account name from the allowed accounts list.",
    )
    category: Optional[str] = Field(
        default=None,
        description="Matched category name from the allowed categories list.",
    )
    note: Optional[str] = Field(
        default=None,
        description="Short clean description of the transaction (e.g., 'Lunch', 'Freelance payout').",
    )
    reply_hint: Optional[str] = Field(
        default=None,
        description="Helpful clarification message if intent is 'unknown' or required info like amount/account is missing.",
    )

def parse_message(
        user_text: str,
        available_accounts: list[str],
        available_categories: list[str],
) -> ParsedFinanceIntent:
    system_prompt = f"""
        You are Tally, a personal finance parser. Convert the user's casual English or Indonesia chat message into structured financial data.

        Available accounts: {", ".join(available_accounts)}
        Available categories: {", ".join(available_categories)}

        Rules:
        1. Convert Indonesian/casual currency shorthand accurately: '30k' or '30rb' = 30000, '1.5jt' = 1500000.
        2. Match the user's mentioned bank/wallet to the closest name in Available accounts (case-insensitive). If no account is mentioned when logging a transaction, set intent to 'unknown' and ask which account they used in reply_hint.
        3. Match the transaction to the best category in Available categories (default to 'Other' if unsure).
        4. If the user asks how much money they have, what their balance is, or 'cek saldo', set intent to 'check_balance'.
        5. If the message is unrelated chatter or missing an amount, set intent to 'unknown' and explain briefly in reply_hint.
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
            if exc.code in (429, 503):
                print(f"[LLM Fallback] {model_name} unavailable ({exc.code}), trying next model...")
                continue
            raise

    raise RuntimeError(f"All Gemini models temporarily unavailable: {last_error}")