"""Seed a small, realistic AML/finance demo dataset into watsonx.data (Iceberg) for the
text-to-SQL tool. Deterministic (fixed RNG seed) so re-runs reproduce identical rows.

Schema: iceberg_data.aml.{customers, accounts, transactions, str_reports}
The data is themed to match the RAG corpus (특정금융정보법/STR/마이데이터) so the SQL,
vector and KG tools cross-reference the same domain. Dates span calendar year 2025.

Run: python seed_aml.py            (idempotent: DROP + CREATE + INSERT)
Needs the same env as ingest.py (PRESTO_HOST/PRESTO_USER/WXD_API_KEY ...).
"""
import random
from datetime import date, timedelta
import rag_common as rc

SCHEMA = "aml"
CATALOG = rc.PRESTO_CATALOG  # iceberg_data
FQ = lambda t: f"{CATALOG}.{SCHEMA}.{t}"

# ---- deterministic synthetic data ----------------------------------------------------
random.seed(42)
DAY0 = date(2025, 1, 1)

SEGMENTS   = ["retail", "sme", "corporate", "private"]
RISKS      = ["low", "low", "low", "medium", "medium", "high"]      # skewed to low
ACCT_TYPES = ["checking", "savings", "investment"]
ACCT_STAT  = ["active", "active", "active", "dormant", "closed"]
CHANNELS   = ["ONLINE", "MOBILE", "WIRE", "ATM", "BRANCH"]
CCY        = ["KRW", "KRW", "KRW", "USD", "USD", "EUR", "JPY"]
# AML flavor: KY=Cayman, VG=BVI, PA=Panama are "high-risk" jurisdictions
COUNTRIES  = ["KR", "KR", "KR", "US", "CN", "JP", "HK", "SG", "KY", "VG", "PA"]
HIGH_RISK_CC = {"KY", "VG", "PA"}
STR_REASONS = ["구조화(분할입금)", "고위험국 송금", "대규모 현금거래",
               "차명계좌 의심", "급격한 거래패턴 변화", "정치적 주요인물(PEP)"]
STR_STATUS  = ["filed", "filed", "under_review", "closed"]

KO_SUR = ["김", "이", "박", "최", "정", "강", "조", "윤", "장", "임"]
KO_GIV = ["민준", "서연", "도윤", "지우", "하준", "서윤", "지호", "예준", "수아", "지안",
          "현우", "유진", "준서", "다은", "시우", "지민", "예린", "건우", "채원", "윤서"]
CORP_NM = ["한빛", "대한", "미래", "선광", "동방", "세움", "가온", "누리", "한솔", "新星"]
CORP_SF = ["상사", "물산", "테크", "캐피탈", "인베스트", "트레이딩", "글로벌", "파트너스"]


def _name(seg):
    if seg in ("corporate", "sme"):
        return f"{random.choice(CORP_NM)}{random.choice(CORP_SF)}"
    return random.choice(KO_SUR) + random.choice(KO_GIV)


def _d(offset):
    return (DAY0 + timedelta(days=offset)).isoformat()


def build():
    customers, accounts, transactions, str_reports = [], [], [], []
    for ci in range(1, 61):                                  # 60 customers
        seg = random.choice(SEGMENTS)
        risk = random.choice(RISKS)
        cust = {
            "customer_id": f"C{ci:04d}", "name": _name(seg), "segment": seg,
            "risk_rating": risk, "country": random.choice(COUNTRIES),
            "onboarded_date": _d(random.randint(-1400, 300)),
        }
        customers.append(cust)
        for ai in range(random.randint(1, 2)):              # 1-2 accounts each
            acc_id = f"A{ci:04d}{ai}"
            accounts.append({
                "account_id": acc_id, "customer_id": cust["customer_id"],
                "type": random.choice(ACCT_TYPES), "status": random.choice(ACCT_STAT),
                "opened_date": _d(random.randint(0, 360)),
            })
            n_tx = random.randint(8, 30) + (12 if risk == "high" else 0)
            for ti in range(n_tx):                          # transactions per account
                cc = random.choice(COUNTRIES)
                base = random.choice([1, 1, 1, 5, 10, 50])  # KRW 10k units
                amt = round(base * random.uniform(1, 9) * 100000, 0)
                # high-risk customers skew to large cross-border flagged flows
                if risk == "high" and random.random() < 0.35:
                    cc = random.choice(list(HIGH_RISK_CC))
                    amt = round(random.uniform(20, 90) * 1000000, 0)
                flagged = (cc in HIGH_RISK_CC and amt > 15000000) or (amt > 50000000)
                transactions.append({
                    "txn_id": f"T{ci:04d}{ai}{ti:03d}", "account_id": acc_id,
                    "txn_date": _d(random.randint(0, 364)), "amount": amt,
                    "currency": random.choice(CCY), "channel": random.choice(CHANNELS),
                    "counterparty_country": cc, "is_flagged": bool(flagged),
                })
        # STRs are filed mostly for high-risk customers, occasionally for medium-risk
        str_prob = 0.85 if risk == "high" else (0.18 if risk == "medium" else 0.0)
        for _ in range(2 if risk == "high" else 1):
            if random.random() < str_prob:
                str_reports.append({
                    "str_id": f"S{len(str_reports)+1:04d}", "customer_id": cust["customer_id"],
                    "filed_date": _d(random.randint(30, 364)), "reason": random.choice(STR_REASONS),
                    "amount": round(random.uniform(20, 120) * 1000000, 0),
                    "status": random.choice(STR_STATUS),
                })
    return customers, accounts, transactions, str_reports


# ---- DDL + batched INSERT (Presto / Iceberg) -----------------------------------------
DDL = {
    "customers": "customer_id varchar, name varchar, segment varchar, risk_rating varchar, "
                 "country varchar, onboarded_date date",
    "accounts": "account_id varchar, customer_id varchar, type varchar, status varchar, "
                "opened_date date",
    "transactions": "txn_id varchar, account_id varchar, txn_date date, amount double, "
                    "currency varchar, channel varchar, counterparty_country varchar, is_flagged boolean",
    "str_reports": "str_id varchar, customer_id varchar, filed_date date, reason varchar, "
                   "amount double, status varchar",
}
COLS = {
    "customers": ["customer_id", "name", "segment", "risk_rating", "country", "onboarded_date"],
    "accounts": ["account_id", "customer_id", "type", "status", "opened_date"],
    "transactions": ["txn_id", "account_id", "txn_date", "amount", "currency",
                     "channel", "counterparty_country", "is_flagged"],
    "str_reports": ["str_id", "customer_id", "filed_date", "reason", "amount", "status"],
}
DATE_COLS = {"onboarded_date", "opened_date", "txn_date", "filed_date"}


def _lit(col, v):
    if col in DATE_COLS:
        return f"DATE '{v}'"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def _row(table, r):
    return "(" + ",".join(_lit(c, r[c]) for c in COLS[table]) + ")"


def load_table(table, rows, batch=200):
    t = FQ(table)
    rc.presto_exec(f"DROP TABLE IF EXISTS {t}")
    rc.presto_exec(f"CREATE TABLE {t} ({DDL[table]}) WITH (format='PARQUET')")
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        rc.presto_exec(f"INSERT INTO {t} VALUES " + ",".join(_row(table, r) for r in chunk))
    print(f"[aml] {table}: {len(rows)} rows")


def main():
    if not rc.PRESTO_HOST:
        raise SystemExit("PRESTO_HOST not set")
    rc.presto_exec(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA}")
    customers, accounts, transactions, str_reports = build()
    load_table("customers", customers)
    load_table("accounts", accounts)
    load_table("transactions", transactions)
    load_table("str_reports", str_reports)
    print("[aml] seed complete")


if __name__ == "__main__":
    main()
