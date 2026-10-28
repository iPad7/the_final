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
    "sku_demand_guard",
    "sku",
    "seller",
)
