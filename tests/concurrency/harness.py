"""경합 테스트 공용 하네스.

스레드는 각자 자기 커넥션으로 자기 트랜잭션을 돈다. SQLite로는 이 상황을 만들 수
없고(ADR23-03/07), 애플리케이션 조회만으로는 막을 수 없는 구간이 바로 여기다.
"""

import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor

# 경합을 만들 스레드 수. 늘리면 우연히 부딪힐 확률이 오르지만, 결정론이 필요한
# 경로는 확률이 아니라 끼어들기를 직접 만들어 검증한다.
WORKERS = 4


def run_together[T](tasks: Sequence[Callable[[], T]]) -> list[T]:
    """모든 작업을 같은 순간에 출발시킨다.

    출발을 맞추지 않으면 순차 실행이 되어 경합이 일어나지 않고, 테스트가 허위로
    통과한다.
    """
    barrier = threading.Barrier(len(tasks))

    def run(task: Callable[[], T]) -> T:
        barrier.wait()
        return task()

    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        return [future.result() for future in [pool.submit(run, task) for task in tasks]]
