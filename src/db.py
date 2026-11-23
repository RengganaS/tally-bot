from decimal import Decimal
from typing import Any
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from src.config import DATABASE_URL

pool = ConnectionPool(
    conninfo=DATABASE_URL,
    min_size=1,
    max_size=5,
    timeout=10.0,
    kwargs={"prepare_threshold": None, "row_factory": dict_row}
)

def get_reference_data() -> tuple[list[str], list[str]]:
    """Fetch current account and category names to ground the LLM prompt."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM accounts ORDER BY id;")
            accounts = [row["name"] for row in cur.fetchall()]

            cur.execute("SELECT name FROM categories ORDER BY id;")
            categories = [row["name"] for row in cur.fetchall()]

    return accounts, categories

def get_all_balances() -> list[dict[str, Any]]:
    """Return all accounts and their current balances."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT name, account_type, balance FROM accounts ORDER BY id;")
            return cur.fetchall()

def record_transaction(
        tx_type: str,
        amount: Decimal,
        account_name: str,
        category_name: str,
        note: str,
        raw_message: str,
) -> dict[str, Any]:
    """Atomically insert a transaction and update the corresponding account balance."""
    with pool.connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, name, balance FROM accounts WHERE LOWER(name) = LOWER(%s);",
                    (account_name,),
                )
                account = cur.fetchone()
                if not account:
                    raise ValueError(f"Unknown account: '{account_name}'")

                cur.execute(
                    "SELECT id, name FROM categories WHERE LOWER(name) = LOWER(%s);",
                    (category_name,),
                )
                category = cur.fetchone()
                if not category:
                    cur.execute("SELECT id, name FROM categories WHERE LOWER(name) = 'other';")
                    category = cur.fetchone()

                delta = amount if tx_type == "income" else -amount
                new_balance = account["balance"] + delta

                cur.execute(
                    """
                    INSERT INTO transactions (type, amount, account_id, category_id, note, raw_message)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id, created_at;
                    """,
                    (
                        tx_type,
                        amount,
                        account["id"],
                        category["id"] if category else None,
                        note,
                        raw_message,
                    ),
                )
                tx = cur.fetchone()
                assert tx is not None

                cur.execute(
                    "UPDATE accounts SET balance = %s WHERE id = %s;",
                    (new_balance, account["id"]),
                )

                return {
                    "transaction_id": tx["id"],
                    "type": tx_type,
                    "amount": amount,
                    "account": account["name"],
                    "category": category["name"] if category else "Other",
                    "note": note,
                    "new_balance": new_balance,
                }

def delete_transaction(transaction_id: int) -> dict[str, Any] | None:
    """Undo a transaction by reverting its balance impact and deleting the row."""
    with pool.connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT t.id, t.type, t.amount, t.account_id, a.name AS account_name
                    FROM transactions t
                    JOIN accounts a ON t.account_id = a.id
                    WHERE t.id = %s;
                    """,
                    (transaction_id,),
                )
                tx = cur.fetchone()
                if not tx:
                    return None

                revert_delta = -tx["amount"] if tx["type"] == "income" else tx["amount"]
                cur.execute(
                    "UPDATE accounts SET balance = balance + %s WHERE id = %s RETURNING balance;",
                    (revert_delta, tx["account_id"]),
                )
                updated_acc = cur.fetchone()
                assert updated_acc is not None

                cur.execute("DELETE FROM transactions WHERE id = %s;", (transaction_id,))

                return {
                    "account": tx["account_name"],
                    "amount": tx["amount"],
                    "type": tx["type"],
                    "restored_balance": updated_acc["balance"],
                }