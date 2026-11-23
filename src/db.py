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

VALID_ACCOUNT_TYPES = {"bank", "ewallet", "cash", "credit"}
VALID_CATEGORY_TYPES = {"income", "expense"}

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
    """Undo an income, expense, or transfer by reverting account balance(s) and deleting the row."""
    with pool.connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT t.id, t.type, t.amount, t.account_id, t.to_account_id,
                           a.name AS account_name,
                           a2.name AS to_account_name
                    FROM transactions t
                    JOIN accounts a ON t.account_id = a.id
                    LEFT JOIN accounts a2 ON t.to_account_id = a2.id
                    WHERE t.id = %s;
                    """,
                    (transaction_id,),
                )
                tx = cur.fetchone()
                if not tx:
                    return None

                if tx["type"] == "transfer":
                    cur.execute(
                        "UPDATE accounts SET balance = balance + %s WHERE id = %s RETURNING balance;",
                        (tx["amount"], tx["account_id"]),
                    )
                    restored_src = cur.fetchone()
                    assert restored_src is not None

                    cur.execute(
                        "UPDATE accounts SET balance = balance - %s WHERE id = %s RETURNING balance;",
                        (tx["amount"], tx["to_account_id"]),
                    )
                    restored_dst = cur.fetchone()
                    assert restored_dst is not None

                    cur.execute("DELETE FROM transactions WHERE id = %s;", (transaction_id,))
                    return {
                        "type": "transfer",
                        "amount": tx["amount"],
                        "account": tx["account_name"],
                        "to_account": tx["to_account_name"],
                        "restored_balance": restored_src["balance"],
                        "restored_to_balance": restored_dst["balance"],
                    }

                revert_delta = -tx["amount"] if tx["type"] == "income" else tx["amount"]
                cur.execute(
                    "UPDATE accounts SET balance = balance + %s WHERE id = %s RETURNING balance;",
                    (revert_delta, tx["account_id"]),
                )
                updated_acc = cur.fetchone()
                assert updated_acc is not None

                cur.execute("DELETE FROM transactions WHERE id = %s;", (transaction_id,))

                return {
                    "type": tx["type"],
                    "account": tx["account_name"],
                    "amount": tx["amount"],
                    "restored_balance": updated_acc["balance"],
                }

def record_transfer(
        amount: Decimal,
        from_account_name: str,
        to_account_name: str,
        note: str,
        raw_message: str,
) -> dict[str, Any]:
    """Atomically move funds between two accounts without counting as income/expense."""
    if from_account_name.strip().lower() == to_account_name.strip().lower():
        raise ValueError("Source and destination accounts must be different.")

    with pool.connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, name, balance FROM accounts WHERE LOWER(name) = LOWER(%s);",
                    (from_account_name,),
                )
                src = cur.fetchone()
                if not src:
                    raise ValueError(f"Unknown source account: '{from_account_name}'")

                cur.execute(
                    "SELECT id, name, balance FROM accounts WHERE LOWER(name) = LOWER(%s);",
                    (to_account_name,),
                )
                dst = cur.fetchone()
                if not dst:
                    raise ValueError(f"Unknown destinations account: '{to_account_name}'")

                new_src_balance = src["balance"] - amount
                new_dst_balance = dst["balance"] + amount

                cur.execute(
                    """
                    INSERT INTO transactions (type, amount, account_id, to_account_id, note, raw_message)
                    VALUES ('transfer', %s, %s, %s, %s, %s)
                    RETURNING id, created_at;
                    """,
                    (amount, src["id"], dst["id"], note, raw_message),
                )
                tx = cur.fetchone()
                assert tx is not None

                cur.execute(
                    "UPDATE accounts SET balance = %s WHERE id = %s;",
                    (new_src_balance, src["id"]),
                )
                cur.execute(
                    "UPDATE accounts SET balance = %s WHERE id = %s;", 
                    (new_dst_balance, dst["id"]),
                )

                return {
                    "transaction_id": tx["id"],
                    "amount": amount,
                    "from_account": src["name"],
                    "to_account": dst["name"],
                    "new_from_balance": new_src_balance,
                    "new_to_balance": new_dst_balance,
                    "note": note,
                }

def create_account(
        name: str,
        account_type: str = "bank",
        initial_balance: Decimal = Decimal("0"),
) -> dict[str, Any]:
    """Create a new financial account if it does not already exists."""
    clean_name = name.strip()
    clean_type = account_type.strip().lower()
    if clean_type not in VALID_ACCOUNT_TYPES:
        clean_type = "bank"

    with pool.connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, name FROM accounts WHERE LOWER(name) = LOWER(%s);",
                    (clean_name,),
                )
                existing = cur.fetchone()
                if existing:
                    raise ValueError(f"Account '{existing['name']}' already exists.")

                cur.execute(
                    """
                    INSERT INTO accounts (name, account_type, balance)
                    VALUES (%s, %s, %s)
                    RETURNING id, name, account_type, balance;
                    """,
                    (clean_name, clean_type, initial_balance),
                )
                acc = cur.fetchone()
                assert acc is not None
                return acc

def create_category(
        name: str, 
        category_type: str = "expense",
) -> dict[str, Any]:
    """Create a new income or expense category if it does not already exists."""
    clean_name = name.strip()
    clean_type = category_type.strip().lower()
    if clean_type not in VALID_CATEGORY_TYPES:
        clean_type = "expense"

    with pool.connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, name, type FROM categories WHERE LOWER(name) = LOWER(%s);",
                    (clean_name,),
                )
                existing = cur.fetchone()
                if existing:
                    raise ValueError(
                        f"Category '{existing['name']}' ({existing['type']}) already exists."
                    )

                cur.execute(
                    """
                    INSERT INTO categories (name, type)
                    VALUES (%s, %s)
                    RETURNING id, name, type;
                    """,
                    (clean_name, clean_type)
                )
                cat = cur.fetchone()
                assert cat is not None
                return cat