"""text-to-SQL tool over the watsonx.data AML demo dataset (iceberg_data.aml).

Pipeline: NL question -> granite generates a single read-only Presto SELECT (schema card +
few-shot) -> guardrail validation (SELECT-only, single statement, enforced LIMIT) -> execute.
On execution error the error is fed back to the LLM once for self-correction.

Returns {sql, columns, rows, error}. Designed to be called from agent.tool_sql and /api/tool/sql.
"""
import re
import rag_common as rc

SCHEMA = "aml"
CATALOG = rc.PRESTO_CATALOG
MAX_ROWS = 200

# Curated schema card (more accurate + token-cheap than live introspection). Keep table/column
# names EXACTLY in sync with seed_aml.py. Data covers calendar year 2025.
SCHEMA_CARD = f"""You write Presto (PrestoDB) SQL for an AML / financial-crime demo lakehouse.
Catalog.schema is {CATALOG}.{SCHEMA}. Always fully-qualify tables as {CATALOG}.{SCHEMA}.<table>.
All data covers calendar year 2025 (date columns are SQL DATE).

Tables:
- {CATALOG}.{SCHEMA}.customers(customer_id, name, segment[retail|sme|corporate|private],
    risk_rating[low|medium|high], country[ISO2], onboarded_date date)
- {CATALOG}.{SCHEMA}.accounts(account_id, customer_id, type[checking|savings|investment],
    status[active|dormant|closed], opened_date date)
- {CATALOG}.{SCHEMA}.transactions(txn_id, account_id, txn_date date, amount double,
    currency[KRW|USD|EUR|JPY], channel[ONLINE|MOBILE|WIRE|ATM|BRANCH],
    counterparty_country[ISO2; KY/VG/PA are high-risk], is_flagged boolean)
- {CATALOG}.{SCHEMA}.str_reports(str_id, customer_id, filed_date date, reason, amount double,
    status[filed|under_review|closed])  -- Suspicious Transaction Reports (특정금융정보법/STR)

Joins: accounts.customer_id = customers.customer_id; transactions.account_id = accounts.account_id;
str_reports.customer_id = customers.customer_id.
"""

FEWSHOT = f"""Examples:
Q: 위험등급이 high인 고객 수는?
SQL: SELECT count(*) AS high_risk_customers FROM {CATALOG}.{SCHEMA}.customers WHERE risk_rating = 'high'

Q: 고위험 국가(KY, VG, PA)로 나간 플래그된 거래 상위 10건의 금액과 상대국가는?
SQL: SELECT txn_id, amount, counterparty_country FROM {CATALOG}.{SCHEMA}.transactions
WHERE is_flagged = true AND counterparty_country IN ('KY','VG','PA')
ORDER BY amount DESC LIMIT 10

Q: 고객 세그먼트별 STR 신고 건수와 신고금액 합계는?
SQL: SELECT c.segment, count(*) AS str_count, sum(s.amount) AS total_amount
FROM {CATALOG}.{SCHEMA}.str_reports s
JOIN {CATALOG}.{SCHEMA}.customers c ON s.customer_id = c.customer_id
GROUP BY c.segment ORDER BY str_count DESC

Q: 위험등급이 high인 고객의 플래그된 거래 총액을 상대국가별로 상위 5개?
SQL: SELECT t.counterparty_country, sum(t.amount) AS total_amount
FROM {CATALOG}.{SCHEMA}.transactions t
JOIN {CATALOG}.{SCHEMA}.accounts a ON t.account_id = a.account_id
JOIN {CATALOG}.{SCHEMA}.customers c ON a.customer_id = c.customer_id
WHERE c.risk_rating = 'high' AND t.is_flagged = true
GROUP BY t.counterparty_country ORDER BY total_amount DESC LIMIT 5
"""

_BANNED = re.compile(r"\b(insert|update|delete|drop|alter|create|truncate|merge|grant|revoke|"
                     r"call|comment|set|use)\b", re.I)


def _strip(sql):
    """Pull SQL out of any code fence / prose; keep the first statement."""
    s = sql.strip()
    m = re.search(r"```(?:sql)?\s*(.*?)```", s, re.S | re.I)
    if m:
        s = m.group(1).strip()
    m = re.search(r"\bselect\b.*", s, re.S | re.I)   # start at first SELECT
    if m:
        s = m.group(0).strip()
    return s.rstrip(";").strip()


def _enforce_limit(sql):
    return sql if re.search(r"\blimit\s+\d+\s*$", sql, re.I) else f"{sql} LIMIT {MAX_ROWS}"


def validate_sql(sql):
    """Return cleaned SQL or raise ValueError. Read-only, single SELECT, bounded."""
    s = _strip(sql)
    if not s:
        raise ValueError("empty SQL")
    if ";" in s:
        raise ValueError("multiple statements not allowed")
    if not re.match(r"(?is)^\s*(select|with)\b", s):
        raise ValueError("only SELECT/WITH queries allowed")
    if _BANNED.search(s):
        raise ValueError("write/DDL keyword not allowed")
    return _enforce_limit(s)


def generate_sql(question, error=None, prev_sql=None):
    sys = (SCHEMA_CARD + "\n" + FEWSHOT +
           "\nReturn ONLY one Presto SELECT statement, no prose, no code fence, no semicolon. "
           "Read-only. Use only the tables/columns above. Add LIMIT when listing rows.")
    user = f"Question: {question}\nSQL:"
    if error and prev_sql:
        user = (f"The previous SQL failed.\nPrevious SQL: {prev_sql}\nError: {error}\n"
                f"Fix it and return ONLY the corrected SELECT.\nQuestion: {question}\nSQL:")
    return rc.wx_chat([{"role": "system", "content": sys},
                       {"role": "user", "content": user}], max_tokens=300)


def run_text2sql(question):
    """NL -> SQL -> validate -> execute, with one self-correction retry on execution error.
    Returns {sql, columns, rows, error}."""
    raw = generate_sql(question)
    try:
        sql = validate_sql(raw)
    except ValueError as e:
        return {"sql": _strip(raw), "columns": [], "rows": [], "error": f"invalid SQL: {e}"}
    try:
        cols, rows = rc.presto_query(sql)
        return {"sql": sql, "columns": cols, "rows": [list(r) for r in rows], "error": None}
    except Exception as e:
        err = str(e)[:300]
    # self-correction: feed the error back once
    try:
        sql2 = validate_sql(generate_sql(question, error=err, prev_sql=sql))
        cols, rows = rc.presto_query(sql2)
        return {"sql": sql2, "columns": cols, "rows": [list(r) for r in rows], "error": None}
    except Exception as e2:
        return {"sql": sql, "columns": [], "rows": [], "error": str(e2)[:300]}


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "위험등급이 high인 고객 수는?"
    r = run_text2sql(q)
    print("SQL:", r["sql"])
    print("ERR:", r["error"])
    print("COLS:", r["columns"])
    for row in r["rows"][:10]:
        print(row)
