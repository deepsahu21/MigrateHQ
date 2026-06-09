import pandas as pd
from valentine import valentine_match
from valentine.algorithms import JaccardDistanceMatcher

df_a = pd.read_csv("data/raw/olist_orders_dataset.csv").sample(n=1000, random_state=42)
df_b = pd.read_csv("data/manufactured/olist_orders_stage1_manufactured.csv", header=1).sample(n=1000, random_state=42)

matcher = JaccardDistanceMatcher()
matches = valentine_match(df_a, df_b, matcher)

# Keep best match per source column
best = {}
for pair, score in matches.items():
    src = pair[0][1]
    tgt = pair[1][1]
    if src not in best or score > best[src][1]:
        best[src] = (tgt, score)

ground_truth = {
    "order_id": "ord_number",
    "customer_id": "cust_ID",
    "order_status": "order_state",
    "order_purchase_timestamp": "purchase_ts",
    "order_approved_at": "approved_at",
    "order_delivered_carrier_date": "carrier_delivery_date",
    "order_delivered_customer_date": "customer_delivery_date",
    "order_estimated_delivery_date": "estimated_delivery_date",
}

correct = 0
for src, expected_tgt in ground_truth.items():
    if src in best:
        predicted_tgt, score = best[src]
        hit = predicted_tgt == expected_tgt
        correct += hit
        status = "✅" if hit else "❌"
        print(f"{status} {src} -> {predicted_tgt} (expected: {expected_tgt}, score: {score:.4f})")
    else:
        print(f"❌ {src} -> NO MATCH (expected: {expected_tgt})")

print(f"\nAccuracy: {correct}/{len(ground_truth)}")
