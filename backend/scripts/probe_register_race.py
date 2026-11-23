"""探测「注册成功后立即登录」的可见性竞态。

背景：`get_db` 的 commit 发生在**响应发出之后**。若不显式提前提交，
紧随注册的登录请求可能查不到刚插入的用户，表现为偶发 401。

用法（需先启动后端）：
    ./venv/bin/python scripts/probe_register_race.py --base http://127.0.0.1:8001/api/v1

修复前实测：16 组并发里 7 组返回 (201, 401)；
修复后：24 组并发全部 (201, 200)。
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import json
import random
import time
import urllib.error
import urllib.request


def post(base: str, path: str, body: dict) -> int:
    request = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Idempotency-Key": f"probe-{random.random()}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000/api/v1")
    parser.add_argument("--rounds", type=int, default=24)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    def one(index: int) -> tuple[int, int]:
        username = f"race_{int(time.time() * 1000)}_{index}"
        password = "racepass123"
        registered = post(args.base, "/auth/register", {"username": username, "password": password})
        logged_in = post(args.base, "/auth/session", {"username": username, "password": password})
        return registered, logged_in

    with futures.ThreadPoolExecutor(args.workers) as pool:
        results = list(pool.map(one, range(args.rounds)))

    bad = [item for item in results if item != (201, 200)]
    print(f"并发注册+立即登录 {args.rounds} 组：", end="")
    if bad:
        print(f"失败 {len(bad)} 组 {bad[:5]}")
        return 1
    print("全部成功 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
