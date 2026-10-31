"""통합 테스트가 쓰는 가상 시드 데이터. SIMULATION 데이터만 사용한다(ADR23-08)."""

SELLER_A = "SELLER-A"
SELLER_B = "SELLER-B"
SKU_P = "SKU-P"
SKU_Q = "SKU-Q"

# 자식 테이블부터 지운다. FK 때문에 순서가 중요하고, 직전 테스트가 남긴 요청이
# 멱등 판정에 끼어들면 결과가 뒤바뀐다.
TABLES_IN_DELETE_ORDER = (
    "demand_segment",
    "request_line",
    "fulfillment_request",
    # 입고 쪽도 자식부터. 세 테이블이 모두 receipt를 참조하고, receipt는
    # inbound_advice와 sku를 참조한다.
    "stock_position",
    "qc_decision",
    "ownership_review",
    "receipt",
    "inbound_advice",
    "sku_demand_guard",
    "sku",
    "seller",
)
