# WMS Schema Matching Context

## Domain: Warehouse/Order Management Systems
Columns describe the lifecycle of an order from placement through delivery.
Common renames across WMS platforms:
- order identifiers: order_id, ord_num, transaction_ref, order_ref, txn_id
- customer identifiers: customer_id, buyer_key, client_ref, cust_num
- order state: order_status, fulfillment_stage, shipment_state, order_state
- creation timestamp: order_purchase_timestamp, created_at, placed_at, order_date
- approval timestamp: order_approved_at, confirmed_time, approved_ts
- carrier handoff date: order_delivered_carrier_date, shipped_date, carrier_date, dispatch_date
- customer receipt date: order_delivered_customer_date, received_date, delivered_date, delivery_date
- estimated delivery: order_estimated_delivery_date, promised_date, eta_date, expected_delivery

## Few-Shot Matching Examples

Example 1:
Source: order_id (samples: ["abc123","def456"])
Target: transaction_ref (samples: ["abc123","def456"])
Result: {"source":"order_id","target":"transaction_ref","confidence":0.97,"reasoning":"Both are unique order identifiers with identical UUIDs."}

Example 2:
Source: customer_id (samples: ["9ef432eb","d2b0e27f"])
Target: buyer_key (samples: ["9ef432eb","d2b0e27f"])
Result: {"source":"customer_id","target":"buyer_key","confidence":0.97,"reasoning":"Both are unique customer identifiers with matching hash values."}

Example 3:
Source: order_status (samples: ["delivered","shipped","canceled"])
Target: fulfillment_stage (samples: ["delivered","shipped","canceled"])
Result: {"source":"order_status","target":"fulfillment_stage","confidence":0.95,"reasoning":"Both represent order lifecycle state with identical enumerated values."}

Example 4:
Source: order_delivered_carrier_date (samples: ["2017-10-04","2017-11-18"])
Target: shipped_date (samples: ["2017-10-04","2017-11-18"])
Result: {"source":"order_delivered_carrier_date","target":"shipped_date","confidence":0.93,"reasoning":"Carrier handoff date maps to shipped_date; both mark when carrier received the package."}

Example 5:
Source: order_delivered_customer_date (samples: ["2017-10-10","2017-12-01"])
Target: received_date (samples: ["2017-10-10","2017-12-01"])
Result: {"source":"order_delivered_customer_date","target":"received_date","confidence":0.93,"reasoning":"Customer delivery date maps to received_date; both mark when the customer got the package."}

## Output Format
Return ONLY a JSON array — no markdown, no preamble:
[
  {"source": "col_name", "target": "col_name", "confidence": 0.0-1.0, "reasoning": "one sentence"},
  ...
]
Each source column must appear exactly once. If no target fits, use "target": null.
