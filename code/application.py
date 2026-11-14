import os
import re
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from google import genai

load_dotenv()


def build_db_uri() -> str:
    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "3306")
    user = os.getenv("DB_USER", "root")
    password = os.getenv("DB_PASSWORD", "Stella24_!")  # Change to your password
    db_name = os.getenv("DB_NAME", "school_db")
    return f"mysql+pymysql://{user}:{password}@{host}:{port}/{db_name}"


def get_gemini_client():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY is not set in .env")
    return genai.Client(api_key=api_key)


def get_model_name() -> str:
    return os.getenv("GEMINI_MODEL", "gemini-3.5-flash")


def get_schema_text(engine) -> str:
    schema_sql = """
    SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
    ORDER BY TABLE_NAME, ORDINAL_POSITION
    """
    with engine.connect() as conn:
        rows = conn.execute(text(schema_sql)).fetchall()

    grouped = {}
    for table_name, column_name, data_type in rows:
        grouped.setdefault(table_name, []).append(f"{column_name} ({data_type})")

    parts = []
    for table, cols in grouped.items():
        parts.append(f"Table: {table}\nColumns: " + ", ".join(cols))
    return "\n\n".join(parts)


def extract_sql(raw_text: str) -> str:
    raw_text = raw_text.strip()

    sql_block = re.search(r"```sql\s*(.*?)```", raw_text, re.IGNORECASE | re.DOTALL)
    if sql_block:
        return sql_block.group(1).strip()

    code_block = re.search(r"```\s*(.*?)```", raw_text, re.DOTALL)
    if code_block:
        return code_block.group(1).strip()

    return raw_text


def is_safe_select_query(sql: str) -> bool:
    sql_clean = sql.strip().lower()

    if not sql_clean.startswith("select"):
        return False

    blocked_keywords = [
        "insert", "update", "delete", "drop", "alter",
        "truncate", "create", "replace", "rename", "grant", "revoke"
    ]

    for keyword in blocked_keywords:
        if re.search(rf"\b{keyword}\b", sql_clean):
            return False

    return True


def generate_sql(question: str, schema_text: str, client, model_name: str) -> str:
    prompt = f"""
You are a MySQL SQL assistant.

Generate exactly ONE valid MySQL SELECT query for the user question.

Rules:
1. Only output SQL.
2. Only generate a read-only SELECT statement.
3. Never generate INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE, CREATE, or other destructive statements.
4. Use only the tables and columns that appear in the schema below.
5. If the question cannot be answered using this schema, output exactly:
NOT_ANSWERABLE

Database schema:
{schema_text}

User question:
{question}
"""
    response = client.models.generate_content(
        model=model_name,
        contents=prompt
    )
    return extract_sql(response.text)


def run_sql(engine, sql: str):
    with engine.connect() as conn:
        result = conn.execute(text(sql))
        rows = result.fetchall()
        columns = result.keys()
        return columns, rows


def summarize_answer(question: str, sql: str, columns, rows, client, model_name: str) -> str:
    prompt = f"""
You are a helpful database course teaching assistant.

Given the user question, SQL, and SQL result, write a short plain-English answer.

Question:
{question}

SQL:
{sql}

Columns:
{list(columns)}

Rows:
{rows}
"""
    response = client.models.generate_content(
        model=model_name,
        contents=prompt
    )
    return response.text.strip()


def main():
    engine = create_engine(build_db_uri())
    client = get_gemini_client()
    model_name = get_model_name()

    print("=" * 60)
    print("NL2SQL Starter (Gemini + MySQL)")
    print("Type 'exit' to quit.")
    print("=" * 60)

    schema_text = get_schema_text(engine)

    while True:
        question = input("\nEnter your question: ").strip()

        if question.lower() == "exit":
            print("Bye.")
            break

        if not question:
            print("Please enter a question.")
            continue

        try:
            sql = generate_sql(question, schema_text, client, model_name)

            if sql == "NOT_ANSWERABLE":
                print("\n[System] This question cannot be answered from the current schema.")
                continue

            print("\n[Generated SQL]")
            print(sql)

            if not is_safe_select_query(sql):
                print("\n[Blocked] Unsafe or non-SELECT SQL detected. Query not executed.")
                continue

            columns, rows = run_sql(engine, sql)

            print("\n[Query Result]")
            print("Columns:", list(columns))
            for row in rows:
                print(row)

            final_answer = summarize_answer(question, sql, columns, rows, client, model_name)
            print("\n[Final Answer]")
            print(final_answer)

        except SQLAlchemyError as e:
            print("\n[Database Error]")
            print(str(e))
        except Exception as e:
            print("\n[System Error]")
            print(str(e))


if __name__ == "__main__":
    main()