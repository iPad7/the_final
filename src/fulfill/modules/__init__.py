"""업무 모듈.

하나의 PostgreSQL 위에 놓인 논리적 경계이며 별도 서비스가 아니다. 모듈은 다른
모듈의 `models`를 import하지 않고, 그 모듈이 공개한 `queries`를 거친다
(ADR23 §3).
"""
