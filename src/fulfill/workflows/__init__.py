"""모듈을 넘는 use case 조정 계층.

여러 모듈의 상태를 함께 판정해야 하는 작업은 여기서 하나의 Unit of Work로
묶는다(ADR23 §3). 각 모듈은 자기 ORM만 소유하므로, 이 계층은 모듈이 공개한
입구(`queries`, `guard`, `repository`)만 호출한다.
"""
