"""Instacart-only extension pages for build_sofi.py: Finance (spend analysis,
forecast + commentary with supervisor roll-up and warehouse write-back) and
Fraud (coordinated / payment-card abuse visibility).

These come from the Instacart use-case brief (finance/accounting + fraud
pilots; Mode replacement). build_sofi.py calls extend(ctx) before it assembles
the document; ctx exposes the builder's helpers so this file stays declarative.
Returns (layout, pages).
"""

# --------------------------------------------------------------------- data

# department, cost center, supervisor, GL account, monthly $M base, actual
# growth, budget growth. Sums to roughly the cost base behind ~$3.0B of gross
# profit and ~$1.0B contribution (illustrative, not reported).
_GL = [
    ("Shopper Pay & Incentives", "CC-1100", "Dana Whitfield", "6110 Shopper Incentives", 22.0, .060, .050),
    ("Shopper Pay & Incentives", "CC-1100", "Dana Whitfield", "6120 Shopper Bonus Pay", 12.0, .070, .050),
    ("Payments & Processing", "CC-1200", "Marcus Lee", "6210 Card Processing Fees", 9.5, .090, .080),
    ("Payments & Processing", "CC-1200", "Marcus Lee", "6220 Fraud & Chargebacks", 4.5, .140, .060),
    ("Cloud & Data", "CC-2100", "Priya Raman", "6310 Cloud Infrastructure", 7.5, .110, .090),
    ("Cloud & Data", "CC-2100", "Priya Raman", "6320 Data Platform Licences", 3.5, .100, .090),
    ("Marketing & Promotions", "CC-3100", "Tom Okafor", "6410 Consumer Promotions", 17.0, .040, .060),
    ("Marketing & Promotions", "CC-3100", "Tom Okafor", "6420 Brand & Performance Media", 9.0, .050, .060),
    ("Customer Support", "CC-4100", "Elena Park", "6510 Support Operations", 9.0, .030, .040),
    ("Insurance & Legal", "CC-5100", "Sam Duarte", "6610 Shopper Insurance", 4.5, .060, .050),
    ("Insurance & Legal", "CC-5100", "Sam Duarte", "6620 Legal & Settlements", 2.5, .180, .050),
    ("People", "CC-6100", "Nina Alvarez", "7010 Compensation & Benefits", 52.0, .045, .045),
    ("Facilities & Other", "CC-7100", "Nina Alvarez", "7110 Facilities & Other", 6.0, .020, .030),
]

_SIGNALS = [
    # signal type, weekly flagged-order base, base detection lag (days),
    # chargeback $K per 1k flagged, legal $K per 1k flagged
    ("Coordinated account rings", 4200, 11.0, 6.5, 1.9),
    ("Payment-card testing", 6100, 5.5, 8.8, 0.6),
    ("Refund & item-missing abuse", 7800, 8.0, 3.1, 0.4),
    ("Promo & referral abuse", 5300, 9.5, 2.2, 0.3),
]
_STATES = [("CA", .30), ("TX", .20), ("FL", .17), ("NY", .15), ("IL", .10), ("WA", .08)]

# cluster, signal, state, accounts, flagged orders, exposure $K, risk, days open, status
_CLUSTERS = [
    ("CL-2041", "Coordinated account rings", "FL", 412, 3180, 940.0, 94, 6, "Investigating"),
    ("CL-2038", "Payment-card testing", "TX", 1280, 9120, 610.0, 91, 2, "Blocked"),
    ("CL-2036", "Coordinated account rings", "CA", 287, 2210, 720.0, 88, 9, "Investigating"),
    ("CL-2033", "Refund & item-missing abuse", "NY", 164, 1450, 215.0, 82, 14, "Monitoring"),
    ("CL-2030", "Payment-card testing", "CA", 940, 6870, 480.0, 79, 3, "Blocked"),
    ("CL-2027", "Promo & referral abuse", "IL", 522, 2980, 96.0, 74, 11, "Monitoring"),
    ("CL-2025", "Coordinated account rings", "WA", 119, 980, 310.0, 71, 17, "Legal review"),
    ("CL-2021", "Refund & item-missing abuse", "TX", 203, 1720, 188.0, 66, 8, "Monitoring"),
    ("CL-2018", "Promo & referral abuse", "FL", 388, 2210, 74.0, 58, 21, "Closed"),
    ("CL-2014", "Payment-card testing", "NY", 610, 3940, 260.0, 55, 5, "Blocked"),
]


def _union(rows, cols):
    out = []
    for i, vals in enumerate(rows):
        if i == 0:
            out.append("    SELECT " + ", ".join(
                "%s AS \"%s\"" % (v, c) for v, c in zip(vals, cols)))
        else:
            out.append("    UNION ALL SELECT " + ", ".join(vals))
    return "SELECT * FROM (\n" + "\n".join(out) + "\n) AS t"


def _q(s):
    return "'%s'" % s.replace("'", "''")


def gl_sql():
    rows = "\n".join(
        "    %sSELECT %s AS department, %s AS cost_center, %s AS supervisor, "
        "%s AS gl_account, %s AS base, %s AS g, %s AS gb"
        % ("" if i == 0 else "UNION ALL ", _q(d), _q(cc), _q(sup), _q(gl), b, g, gb)
        for i, (d, cc, sup, gl, b, g, gb) in enumerate(_GL))
    return """-- Instacart spend ledger: 13 GL accounts x 24 months, $MM. Generated.
WITH acct AS (
%s
),
months AS (
    SELECT DATEADD('month', SEQ4(), DATE '2024-08-01') AS period_month, SEQ4() AS mi
    FROM TABLE(GENERATOR(ROWCOUNT => 24))
)
SELECT
    department                                              AS "Department",
    cost_center                                             AS "Cost Center",
    supervisor                                              AS "Supervisor",
    gl_account                                              AS "GL Account",
    period_month                                            AS "Period",
    YEAR(period_month) || ' Q' || QUARTER(period_month) AS "Quarter",
    CASE WHEN mi >= 12 THEN 'Current Period' ELSE 'Prior Period' END AS "Period Name",
    ROUND(base * POWER(1 + g / 12, mi)
          * (1 + 0.04 * SIN(2 * PI() * mi / 12.0)
               + (MOD(ABS(HASH(gl_account || mi)), 21) - 10) / 200.0), 2) AS "Actual",
    ROUND(base * POWER(1 + gb / 12, mi), 2)                 AS "Budget"
FROM acct CROSS JOIN months
""" % rows


def forecast_base_sql():
    # next-quarter budget and last-quarter actual per department/supervisor
    return """-- One row per department for the forecast input table. Generated.
SELECT
    "Department", "Supervisor",
    ROUND(SUM(CASE WHEN "Period" >= DATE '2026-05-01' THEN "Actual" ELSE 0 END), 1) AS "Last Quarter Actual",
    ROUND(SUM(CASE WHEN "Period" >= DATE '2026-05-01' THEN "Budget" ELSE 0 END) * 1.03, 1) AS "Next Quarter Budget"
FROM (
%s
) g
GROUP BY "Department", "Supervisor"
""" % _gl_inline()


def _gl_inline():
    return gl_sql().replace("-- Instacart spend ledger: 13 GL accounts x 24 months, $MM. Generated.\n", "")


def fraud_weekly_sql():
    sig = "\n".join(
        "    %sSELECT %s AS signal, %s AS base, %s AS lag, %s AS cb, %s AS lg"
        % ("" if i == 0 else "UNION ALL ", _q(s), b, l, cb, lg)
        for i, (s, b, l, cb, lg) in enumerate(_SIGNALS))
    st = "\n".join(
        "    %sSELECT %s AS state, %s AS share"
        % ("" if i == 0 else "UNION ALL ", _q(s), sh) for i, (s, sh) in enumerate(_STATES))
    return """-- Instacart weekly fraud signals: 4 abuse types x 6 states x 52 weeks. Generated.
WITH sig AS (
%s
),
st AS (
%s
),
weeks AS (
    SELECT DATEADD('week', SEQ4(), DATE '2025-08-04') AS wk, SEQ4() AS wi
    FROM TABLE(GENERATOR(ROWCOUNT => 52))
),
g AS (
    SELECT sig.*, st.state, st.share, weeks.wk, weeks.wi,
           base * share * (1 + 0.012 * wi) * (1 + 0.10 * SIN(wi / 5.0)
             + (MOD(ABS(HASH(signal || state || wi)), 21) - 10) / 100.0) AS flagged
    FROM sig CROSS JOIN st CROSS JOIN weeks
)
SELECT
    wk                                                   AS "Week",
    signal                                               AS "Signal Type",
    state                                                AS "State",
    CASE WHEN wi >= 26 THEN 'Current Period' ELSE 'Prior Period' END AS "Period Name",
    ROUND(flagged, 0)                                    AS "Flagged Orders",
    ROUND(flagged / 1000.0 * cb, 1)                      AS "Chargebacks $K",
    ROUND(flagged / 1000.0 * lg, 1)                      AS "Legal Exposure $K",
    -- earlier visibility: detection lag shrinks as coverage improves
    ROUND(lag * (1 - 0.006 * wi) + (MOD(ABS(HASH(state || wi || signal)), 11) - 5) / 20.0, 1) AS "Detection Lag Days"
FROM g
""" % (sig, st)


def clusters_sql():
    rows = [(_q(c), _q(s), _q(stt), str(a), str(f), str(e), str(r), str(d), _q(status))
            for c, s, stt, a, f, e, r, d, status in _CLUSTERS]
    return _union(rows, ["Cluster ID", "Signal Type", "State", "Accounts", "Flagged Orders",
                         "Exposure $K", "Risk Score", "Days Open", "Status"])


# ------------------------------------------------------------------- build

def extend(c):
    add, B, S, CO = c["add"], c["B"], c["S"], c["CO"]
    CFG, LAYOUT = c["CFG"], c["LAYOUT"]
    header, nav_el, panel, title = c["header"], c["nav_el"], c["panel"], c["title"]
    agents = c["agents"]
    MONEY_M, NUM0 = c["MONEY_M"], c["NUM0"]
    MONEY1 = {"kind": "number", "formatString": "$,.1f", "currencySymbol": "$"}
    NUM1 = {"kind": "number", "formatString": ",.1f"}
    list_control = c["list_control"]

    def sql_table(eid, name, statement, colspecs, prefix):
        add({"id": eid, "kind": "table", "name": name,
             "source": {"connectionId": S.CONN_SNOWFLAKE, "kind": "sql",
                        "statement": statement},
             "columns": [{"id": "%s%d" % (prefix, i), "formula": "[Custom SQL/%s]" % n, "name": n}
                         for i, n in enumerate(colspecs)]})

    def kpi(eid, tbl, label, value, compare, fmt, primary=False, cmp_name="Prior",
            lower_is_better=True):
        good, bad = ((B.BAD, B.GOOD) if False else (B.GOOD, B.BAD))
        if lower_is_better:           # spend / fraud: an increase is the bad news
            good, bad = B.BAD, B.GOOD
        add({"id": eid, "kind": "kpi-chart",
             "source": {"elementId": tbl, "kind": "table"},
             "columns": [{"id": eid + "-v", "formula": value, "name": label, "format": fmt},
                         {"id": eid + "-c", "formula": compare, "name": cmp_name, "format": fmt}],
             "value": {"columnId": eid + "-v", "color": B.TEXT_DARK},
             "comparisonColumn": {"columnId": eid + "-c"},
             "comparison": {"display": "delta", "colorGood": good, "colorBad": bad},
             "name": title(label, 12),
             "style": {"backgroundColor": B.CARD, "borderRadius": "round",
                       "borderColor": B.SOFI_BRIGHT if primary else B.BORDER,
                       "borderWidth": 2 if primary else 1},
             "layout": {"anchor": "middle"}})

    def note(eid, md):
        add({"id": eid, "kind": "text", "body": md,
             "style": {"backgroundColor": B.CARD, "borderRadius": "round"},
             "verticalAlign": "middle"})

    # ============================================================ FINANCE
    GL = "Spend Ledger"
    gl_cols = ["Department", "Cost Center", "Supervisor", "GL Account", "Period",
               "Quarter", "Period Name", "Actual", "Budget"]
    sql_table("tbl-gl", GL, gl_sql(), gl_cols, "g")
    sql_table("fin-fcsql", "Forecast Base", forecast_base_sql(),
              ["Department", "Supervisor", "Last Quarter Actual", "Next Quarter Budget"], "fb")

    header(4, "Spend & Forecast", "", [nav_el(4)], titles=False)

    cur = 'SumIf([%s/Actual], [%s/Period Name] = "Current Period")' % (GL, GL)
    pri = 'SumIf([%s/Actual], [%s/Period Name] = "Prior Period")' % (GL, GL)
    bud = 'SumIf([%s/Budget], [%s/Period Name] = "Current Period")' % (GL, GL)
    kpi("fin-k1", "tbl-gl", "Operating spend TTM ($M)", cur, pri, MONEY_M, primary=True,
        cmp_name="Prior TTM")
    kpi("fin-k2", "tbl-gl", "Variance to budget TTM ($M)", "%s - %s" % (cur, bud), "0", MONEY1,
        cmp_name="Budget")
    add({"id": "fin-k3", "kind": "kpi-chart",
         "source": {"elementId": "fin-fc", "kind": "table"},
         "columns": [{"id": "fin-k3-v", "formula": "Coalesce(Sum([Spend Forecast/Forecast]), 0)",
                      "name": "Next-quarter forecast, all teams ($M)", "format": MONEY1},
                     {"id": "fin-k3-c", "formula": "Sum([Spend Forecast/Next Quarter Budget])",
                      "name": "vs next-quarter budget", "format": MONEY1}],
         "value": {"columnId": "fin-k3-v", "color": B.TEXT_DARK},
         "comparisonColumn": {"columnId": "fin-k3-c"},
         "comparison": {"display": "delta", "colorGood": B.BAD, "colorBad": B.GOOD},
         "name": title("Next-quarter forecast, all teams ($M)", 12),
         "style": {"backgroundColor": B.CARD, "borderRadius": "round",
                   "borderColor": B.BORDER, "borderWidth": 1},
         "layout": {"anchor": "middle"}})

    add({"id": "fin-pivot", "kind": "pivot-table", "name": "Actual spend by department and quarter ($M)",
         "source": {"elementId": "tbl-gl", "kind": "table"},
         "columns": [
             {"id": "fp-dept", "formula": "[%s/Department]" % GL, "name": "Department"},
             {"id": "fp-q", "formula": "[%s/Quarter]" % GL, "name": "Quarter"},
             {"id": "fp-act", "formula": "Sum([%s/Actual])" % GL, "name": "Actual ($M)",
              "format": MONEY1}],
         "rowsBy": [{"columnId": "fp-dept"}], "columnsBy": [{"columnId": "fp-q"}],
         "values": ["fp-act"], "style": panel()})

    add({"id": "fin-var", "kind": "bar-chart",
         "source": {"elementId": "tbl-gl", "kind": "table"},
         "columns": [
             {"id": "fv-x", "formula": "[%s/Department]" % GL, "name": "Department"},
             {"id": "fv-act", "formula": cur, "name": "Actual TTM ($M)", "format": MONEY_M},
             {"id": "fv-bud", "formula": bud, "name": "Budget TTM ($M)", "format": MONEY_M}],
         "xAxis": {"columnId": "fv-x"},
         "yAxis": {"columnIds": ["fv-act", "fv-bud"]},
         "stacking": "none",
         "name": title("Actual vs budget by department (TTM)"),
         "legend": {"visibility": "shown"}, "style": panel()})

    # forecast input table: pivot over the base SQL, then a linked input table,
    # the same shape as the scenario modeler's `spivot` -> `assum`.
    add({"id": "fin-fcpivot", "kind": "pivot-table", "name": "Forecast Pivot",
         "source": {"elementId": "fin-fcsql", "kind": "table"},
         "columns": [
             {"id": "fq-dept", "formula": "[Forecast Base/Department]", "name": "Department"},
             {"id": "fq-sup", "formula": "[Forecast Base/Supervisor]", "name": "Supervisor"},
             {"id": "fq-last", "formula": "Sum([Forecast Base/Last Quarter Actual])",
              "name": "Last Quarter Actual", "format": MONEY1},
             {"id": "fq-bud", "formula": "Sum([Forecast Base/Next Quarter Budget])",
              "name": "Next Quarter Budget", "format": MONEY1}],
         "rowsBy": [{"columnId": "fq-dept"}, {"columnId": "fq-sup"}],
         "values": ["fq-last", "fq-bud"], "style": panel()})
    add({"id": "fin-fc", "kind": "input-table",
         "source": {"kind": "linked", "from": "fin-fcpivot"},
         "inputMode": "view", "name": "Spend Forecast",
         "columns": [
             {"id": "fi-dept", "key": "fq-dept"}, {"id": "fi-sup", "key": "fq-sup"},
             {"id": "fi-last", "key": "fq-last"}, {"id": "fi-bud", "key": "fq-bud"},
             {"id": "fi-fc", "type": "number", "name": "Forecast", "format": MONEY1},
             {"id": "fi-var",
              "formula": "If(IsNull([Forecast]), 0, [Forecast] - [Next Quarter Budget])",
              "name": "Forecast vs Budget", "format": MONEY1},
             {"id": "fi-status", "type": "text", "name": "Status",
              "values": ["Draft", "Submitted", "Approved"], "pills": "color-by-option"},
             {"id": "fi-note", "type": "text", "name": "Commentary"}],
         "order": ["fi-dept", "fi-sup", "fi-last", "fi-bud", "fi-fc", "fi-var",
                   "fi-status", "fi-note"],
         "conditionalFormats": [
             {"type": "single", "columnIds": ["fi-fc", "fi-status", "fi-note"],
              "condition": "IsNull", "style": {"backgroundColor": "#F3F4F6"}},
             {"type": "single", "columnIds": ["fi-fc", "fi-status", "fi-note"],
              "condition": "IsNotNull", "style": {"backgroundColor": "#E6F0FE"}}],
         "style": panel()})
    add({"id": "fin-roll", "kind": "pivot-table",
         "name": "Forecast roll-up by supervisor ($M)",
         "source": {"elementId": "fin-fc", "kind": "table"},
         "columns": [
             {"id": "fr-sup", "formula": "[Spend Forecast/Supervisor]", "name": "Supervisor"},
             {"id": "fr-fc", "formula": "Coalesce(Sum([Spend Forecast/Forecast]), 0)", "name": "Forecast",
              "format": MONEY1},
             {"id": "fr-bud", "formula": "Sum([Spend Forecast/Next Quarter Budget])",
              "name": "Budget", "format": MONEY1}],
         "rowsBy": [{"columnId": "fr-sup"}], "values": ["fr-fc", "fr-bud"], "style": panel()})
    note("fin-py",
         "**Python Element — slot reserved**\n\nForecast, anomaly detection and "
         "segmentation run here as a prepared Python Element defined in Sigma and "
         "executed on Snowflake, so business users get data-science output "
         "without leaving the governed workbook.\n\n*Placeholder: the Python "
         "Element is authored in the Sigma UI; it is not generated by this "
         "builder.*")
    add({"id": "fin-chat", "kind": "chat", "agentId": "ag-fin"})
    agents.append({
        "id": "ag-fin", "name": "Finance Copilot",
        "description": "Ad hoc analysis on Instacart spend, budget and forecasts.",
        "instructions": (
            "You are a finance analyst at Instacart. The Spend Ledger has 13 GL "
            "accounts across 8 departments over 24 months (current vs prior "
            "trailing twelve months), in $MM, with actual and budget. The Spend "
            "Forecast table holds each supervisor's next-quarter forecast and "
            "commentary. Find where spend is running over budget, explain what "
            "is driving it, and always name the department and GL account. "
            "Be concise and quantitative; this is a SOX-governed workbook."),
        "greeting": {"mode": "generated",
                     "prompt": "Greet the user in one short line, then offer exactly "
                               "three specific questions about spend vs budget, naming "
                               "real departments and one that is over budget."},
        "dataSources": [{"kind": "table", "elementId": "tbl-gl"},
                        {"kind": "table", "elementId": "fin-fc"}],
        "tools": []})

    # ============================================================== FRAUD
    WK, CL = "Fraud Signals", "Fraud Clusters"
    sql_table("tbl-frd", WK, fraud_weekly_sql(),
              ["Week", "Signal Type", "State", "Period Name", "Flagged Orders",
               "Chargebacks $K", "Legal Exposure $K", "Detection Lag Days"], "w")
    sql_table("tbl-frdcl", CL, clusters_sql(),
              ["Cluster ID", "Signal Type", "State", "Accounts", "Flagged Orders",
               "Exposure $K", "Risk Score", "Days Open", "Status"], "x")

    header(5, "Fraud Risk", "", [nav_el(5)], titles=False)
    add(list_control("frd-sig", "SignalType", "Abuse type", "tbl-frd", "w1"))

    def per(col, name):
        return ('SumIf([%s/%s], [%s/Period Name] = "Current Period")' % (WK, col, WK),
                'SumIf([%s/%s], [%s/Period Name] = "Prior Period")' % (WK, col, WK))
    a, b = per("Flagged Orders", "")
    kpi("frd-k1", "tbl-frd", "Flagged orders, last 26 weeks", a, b, NUM0, primary=True,
        cmp_name="Prior 26 weeks")
    a, b = per("Chargebacks $K", "")
    kpi("frd-k2", "tbl-frd", "Chargebacks, last 26 weeks ($K)", a, b, NUM0,
        cmp_name="Prior 26 weeks")
    kpi("frd-k3", "tbl-frd", "Avg detection lag (days)",
        'AvgIf([%s/Detection Lag Days], [%s/Period Name] = "Current Period")' % (WK, WK),
        'AvgIf([%s/Detection Lag Days], [%s/Period Name] = "Prior Period")' % (WK, WK), NUM1,
        cmp_name="Prior 26 weeks")
    kpi("frd-k4", "tbl-frdcl", "High-risk clusters open (score 80+)",
        'CountIf([%s/Risk Score] >= 80 and [%s/Status] != "Closed")' % (CL, CL), "0", NUM0,
        cmp_name="Zero open")

    add({"id": "frd-bar", "kind": "bar-chart",
         "source": {"elementId": "tbl-frd", "kind": "table"},
         "columns": [
             {"id": "fw-x", "formula": "DateTrunc(\"week\", [%s/Week])" % WK, "name": "Week"},
             {"id": "fw-cat", "formula": "[%s/Signal Type]" % WK, "name": "Abuse type"},
             {"id": "fw-y", "formula": "Sum([%s/Flagged Orders])" % WK, "name": "Flagged orders",
              "format": NUM0}],
         "xAxis": {"columnId": "fw-x"}, "yAxis": {"columnIds": ["fw-y"]},
         "color": {"by": "category", "column": "fw-cat", "scheme": B.CATEGORICAL},
         "stacking": "stacked",
         "name": title("Weekly flagged orders by abuse type"),
         "legend": {"visibility": "shown"}, "style": panel()})
    add({"id": "frd-lag", "kind": "bar-chart",
         "source": {"elementId": "tbl-frd", "kind": "table"},
         "columns": [
             {"id": "fl-x", "formula": "[%s/Signal Type]" % WK, "name": "Abuse type"},
             {"id": "fl-cur", "formula": 'AvgIf([%s/Detection Lag Days], [%s/Period Name] = "Current Period")' % (WK, WK),
              "name": "Last 26 weeks", "format": NUM1},
             {"id": "fl-pri", "formula": 'AvgIf([%s/Detection Lag Days], [%s/Period Name] = "Prior Period")' % (WK, WK),
              "name": "Prior 26 weeks", "format": NUM1}],
         "xAxis": {"columnId": "fl-x"}, "yAxis": {"columnIds": ["fl-pri", "fl-cur"]},
         "stacking": "none",
         "name": title("Days to detect, by abuse type (earlier is better)"),
         "legend": {"visibility": "shown"}, "style": panel()})
    add({"id": "frd-cl", "kind": "table", "name": "Open fraud clusters, ranked by risk",
         "source": {"elementId": "tbl-frdcl", "kind": "table"},
         "columns": [
             {"id": "fc-id", "formula": "[%s/Cluster ID]" % CL, "name": "Cluster"},
             {"id": "fc-sig", "formula": "[%s/Signal Type]" % CL, "name": "Abuse type"},
             {"id": "fc-st", "formula": "[%s/State]" % CL, "name": "State"},
             {"id": "fc-acc", "formula": "[%s/Accounts]" % CL, "name": "Accounts", "format": NUM0},
             {"id": "fc-ord", "formula": "[%s/Flagged Orders]" % CL, "name": "Flagged orders", "format": NUM0},
             {"id": "fc-exp", "formula": "[%s/Exposure $K]" % CL, "name": "Exposure ($K)", "format": NUM0},
             {"id": "fc-risk", "formula": "[%s/Risk Score]" % CL, "name": "Risk score"},
             {"id": "fc-days", "formula": "[%s/Days Open]" % CL, "name": "Days open"},
             {"id": "fc-stat", "formula": "[%s/Status]" % CL, "name": "Status"}],
         "order": ["fc-id", "fc-sig", "fc-st", "fc-acc", "fc-ord", "fc-exp", "fc-risk",
                   "fc-days", "fc-stat"],
         "conditionalFormats": [
             {"type": "single", "columnIds": ["fc-risk"], "condition": ">=", "value": 80,
              "style": {"backgroundColor": "#FCEBEB", "color": "#A32D2D", "bold": True}},
             {"type": "single", "columnIds": ["fc-risk"], "condition": "Between",
              "low": 60, "high": 79.99,
              "style": {"backgroundColor": "#FFF3E0", "color": "#B45309"}}],
         "tableComponents": {"summaryBar": "hidden"}, "style": panel()})
    add({"id": "frd-chat", "kind": "chat", "agentId": "ag-fraud"})
    agents.append({
        "id": "ag-fraud", "name": "Fraud Copilot",
        "description": "Spots coordinated and payment-card abuse early.",
        "instructions": (
            "You are a fraud analyst at Instacart. Fraud Signals holds weekly "
            "flagged orders, chargebacks, legal exposure and detection lag by "
            "abuse type and state (last 26 weeks vs prior 26). Fraud Clusters "
            "lists open investigations with risk scores. Focus on coordinated "
            "account rings and payment-card abuse, where legal expense is "
            "highest, and on whether detection is getting earlier. Name the "
            "abuse type, state and cluster ID. Be concise and quantitative."),
        "greeting": {"mode": "generated",
                     "prompt": "Greet the user in one short line, then offer exactly "
                               "three specific questions, one about the riskiest open "
                               "cluster and one about detection lag."},
        "dataSources": [{"kind": "table", "elementId": "tbl-frd"},
                        {"kind": "table", "elementId": "tbl-frdcl"}],
        "tools": []})

    # ============================================================== LAYOUT
    pages_xml = """
<Page type="grid" gridTemplateColumns="repeat(24, 1fr)" gridTemplateRows="auto" id="pgFin">
  <Container elementId="c-hdr4" type="grid" gridColumn="1 / 25" gridRow="1 / 6" gridTemplateColumns="repeat(24, 1fr)" gridTemplateRows="auto">
    <Element elementId="logo4" gridColumn="1 / 6" gridRow="1 / 6"/>
    <Element elementId="nav-main4" gridColumn="9 / 21" gridRow="2 / 6"/>
  </Container>
  <Element elementId="fin-k1" gridColumn="1 / 9" gridRow="6 / 12"/>
  <Element elementId="fin-k2" gridColumn="9 / 17" gridRow="6 / 12"/>
  <Element elementId="fin-k3" gridColumn="17 / 25" gridRow="6 / 12"/>
  <Element elementId="fin-pivot" gridColumn="1 / 15" gridRow="12 / 28"/>
  <Element elementId="fin-var" gridColumn="15 / 25" gridRow="12 / 28"/>
  <Element elementId="fin-fc" gridColumn="1 / 25" gridRow="28 / 42"/>
  <Element elementId="fin-roll" gridColumn="1 / 10" gridRow="42 / 56"/>
  <Element elementId="fin-py" gridColumn="10 / 17" gridRow="42 / 56"/>
  <Element elementId="fin-chat" gridColumn="17 / 25" gridRow="42 / 62"/>
</Page>
<Page type="grid" gridTemplateColumns="repeat(24, 1fr)" gridTemplateRows="auto" id="pgFraud">
  <Container elementId="c-hdr5" type="grid" gridColumn="1 / 25" gridRow="1 / 6" gridTemplateColumns="repeat(24, 1fr)" gridTemplateRows="auto">
    <Element elementId="logo5" gridColumn="1 / 6" gridRow="1 / 6"/>
    <Element elementId="nav-main5" gridColumn="9 / 21" gridRow="2 / 6"/>
  </Container>
  <Element elementId="frd-sig" gridColumn="1 / 9" gridRow="6 / 9"/>
  <Element elementId="frd-k1" gridColumn="1 / 7" gridRow="9 / 15"/>
  <Element elementId="frd-k2" gridColumn="7 / 13" gridRow="9 / 15"/>
  <Element elementId="frd-k3" gridColumn="13 / 19" gridRow="9 / 15"/>
  <Element elementId="frd-k4" gridColumn="19 / 25" gridRow="9 / 15"/>
  <Element elementId="frd-bar" gridColumn="1 / 15" gridRow="15 / 31"/>
  <Element elementId="frd-lag" gridColumn="15 / 25" gridRow="15 / 31"/>
  <Element elementId="frd-cl" gridColumn="1 / 18" gridRow="31 / 47"/>
  <Element elementId="frd-chat" gridColumn="18 / 25" gridRow="31 / 55"/>
</Page>
"""
    data_xml = """  <Element elementId="tbl-gl" gridColumn="1 / 9" gridRow="80 / 92"/>
  <Element elementId="fin-fcsql" gridColumn="9 / 17" gridRow="80 / 92"/>
  <Element elementId="fin-fcpivot" gridColumn="17 / 25" gridRow="80 / 92"/>
  <Element elementId="tbl-frd" gridColumn="1 / 13" gridRow="92 / 104"/>
  <Element elementId="tbl-frdcl" gridColumn="13 / 25" gridRow="92 / 104"/>
"""
    marker = '<Page type="grid" gridTemplateColumns="repeat(24, 1fr)" gridTemplateRows="auto" id="pgData">'
    assert marker in LAYOUT
    LAYOUT = LAYOUT.replace(marker, pages_xml.strip() + "\n" + marker)
    head, tail = LAYOUT.rsplit("</Page>", 1)
    LAYOUT = head + data_xml + "</Page>" + tail

    pages = [{"id": "pgFin", "name": "Spend & Forecast"},
             {"id": "pgFraud", "name": "Fraud Risk"}]
    nav_opts = [{"label": "Spend & Forecast", "destination": {"type": "page", "pageId": "pgFin"}},
                {"label": "Fraud Risk", "destination": {"type": "page", "pageId": "pgFraud"}}]
    return LAYOUT, pages, nav_opts
