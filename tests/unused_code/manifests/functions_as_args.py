from __future__ import annotations


class TimeoutSampler:
    def __init__(self, wait_timeout: int, sleep: int, func: callable, **func_kwargs) -> None:
        self.wait_timeout = wait_timeout
        self.sleep = sleep
        self.func = func
        self.func_kwargs = func_kwargs


def get_failed_cluster_operator(admin_client: str) -> bool:
    return admin_client == "failed"


def main(admin_client: str) -> TimeoutSampler:
    return TimeoutSampler(
        wait_timeout=10,
        sleep=1,
        func=get_failed_cluster_operator,
        admin_client=admin_client,
    )
