"""Background Tasks 课程：只运行有界的人工数据计算，不接受命令或路径。"""

import asyncio
import copy
import secrets
from dataclasses import dataclass, field

from agents import function_tool
from pydantic import StrictInt


@dataclass
class BackgroundJob:
    task_id: str
    numbers: list[int]
    created_at: float
    status: str = "pending"
    started_at: float | None = None
    finished_at: float | None = None
    result: dict | None = None
    error: str | None = None
    history: list = field(default_factory=list)

    def snapshot(self):
        return copy.deepcopy({
            "task_id": self.task_id, "status": self.status, "numbers": self.numbers,
            "created_at": self.created_at, "started_at": self.started_at,
            "finished_at": self.finished_at, "result": self.result, "error": self.error,
        })

    def record(self):
        self.history.append(self.snapshot())


class BackgroundTasks:
    """进程内管理器；退出时等待取消完成，不遗留 worker。"""

    def __init__(self, numbers, foreground_a, foreground_b, delay=4.0, compute=sum):
        self.validate_numbers(numbers)
        if not 0 <= delay <= 5:
            raise ValueError("练习计算延迟只能在 0 到 5 秒之间。")
        self.numbers = list(numbers)
        self.foreground_numbers = (foreground_a, foreground_b)
        self.delay, self.compute = delay, compute
        self.jobs = {}
        self.workers = {}
        self.foreground_events = []
        self.closed = False

    @staticmethod
    def validate_numbers(numbers):
        if not isinstance(numbers, list) or not 1 <= len(numbers) <= 8 or any(
                type(n) is not int or abs(n) > 10_000 for n in numbers):
            raise ValueError("只接受 1 到 8 个人工整数，绝对值不超过 10000。")

    def start(self, numbers):
        self.validate_numbers(numbers)
        if self.closed or self.jobs:
            raise ValueError("本节只允许启动一次；管理器关闭后不能再启动。")
        if numbers != self.numbers:
            raise ValueError("只允许汇总本次任务指定的人工数据。")
        job = BackgroundJob("bg-" + secrets.token_hex(6), list(numbers), asyncio.get_running_loop().time())
        job.record()
        self.jobs[job.task_id] = job
        self.workers[job.task_id] = asyncio.create_task(self._run(job))
        # 不 await worker；此回执保存的是刚提交时的 pending 状态。
        return {"ok": True, "task": job.snapshot()}

    async def _run(self, job):
        loop = asyncio.get_running_loop()
        job.status, job.started_at = "running", loop.time()
        job.record()
        try:
            # 模拟耗时的本地工作；await 让出执行机会，不阻塞主流程。
            await asyncio.sleep(self.delay)
            total = self.compute(job.numbers)
            if type(total) is not int:
                raise ValueError("计算器未返回整数。")
            job.result = {"sum": total, "count": len(job.numbers), "receipt": secrets.token_hex(6)}
            job.status = "completed"
        except asyncio.CancelledError:
            job.status, job.error = "failed", "任务已取消：当前练习进程正在结束。"
        except Exception as error:
            # 只记录异常类型；不把任意异常文本或凭据写入记录。
            job.status, job.error = "failed", type(error).__name__
        finally:
            job.finished_at = loop.time()
            job.record()

    def get_status(self, task_id):
        if task_id not in self.jobs:
            return {"ok": False, "error": "未知任务编号。"}
        return {"ok": True, "task": self.jobs[task_id].snapshot()}

    async def get_result(self, task_id, wait=False):
        if task_id not in self.jobs:
            return {"ok": False, "error": "未知任务编号。"}
        if wait:
            try:
                # 单次有界等待，不反复付费轮询；超时不会误取消后台 worker。
                await asyncio.wait_for(asyncio.shield(self.workers[task_id]), timeout=6.0)
            except TimeoutError:
                return {"ok": False, "error": "等待结果超过 6 秒。", "task": self.jobs[task_id].snapshot()}
        task = self.jobs[task_id].snapshot()
        return {"ok": task["status"] != "failed", "ready": task["status"] in ("completed", "failed"), "task": task}

    def add(self, a, b):
        if not self.jobs or self.foreground_events or type(a) is not int or type(b) is not int or (
                (a, b) != self.foreground_numbers and (b, a) != self.foreground_numbers):
            return {"ok": False, "error": "启动后台任务后，只计算一次指定的独立加法。"}
        now = asyncio.get_running_loop().time()
        task = next(iter(self.jobs.values())).snapshot()
        event = {"at": now, "result": a + b, "task_id": task["task_id"], "background_status": task["status"]}
        self.foreground_events.append(event)
        return {"ok": True, **event}

    async def close(self):
        self.closed = True
        for worker in self.workers.values():
            if not worker.done():
                worker.cancel()
        if self.workers:
            await asyncio.gather(*self.workers.values(), return_exceptions=True)
        # 极早停止时，协程可能尚未运行，补记失败状态。
        for job in self.jobs.values():
            if job.status in ("pending", "running"):
                job.status, job.error = "failed", "任务已取消：当前练习进程正在结束。"
                job.finished_at = asyncio.get_running_loop().time()
                job.record()

    def snapshot(self):
        return {"tasks": [job.snapshot() | {"history": copy.deepcopy(job.history)} for job in self.jobs.values()],
                "foreground_events": copy.deepcopy(self.foreground_events), "closed": self.closed}


class BackgroundCallbacks(BackgroundTasks):
    """宿主程序的异步业务任务；不暴露给模型，不接受命令、源码或路径。"""

    def __init__(self):
        self.jobs, self.workers, self.foreground_events = {}, {}, []
        self.closed = False

    def start_callback(self, operation):
        if self.closed or any(job.status in ("pending", "running") for job in self.jobs.values()):
            raise ValueError("后台任务已在运行或管理器已关闭。")
        if not callable(operation):
            raise ValueError("业务操作需要由宿主提供。")
        # 历史交给业务 SQLite 保存，只保留当前任务，避免长期运行无限占用内存。
        self.jobs.clear()
        self.workers.clear()
        job = BackgroundJob("bg-" + secrets.token_hex(6), [], asyncio.get_running_loop().time())
        job.record()
        self.jobs[job.task_id] = job
        self.workers[job.task_id] = asyncio.create_task(self._run_callback(job, operation))
        return {"ok": True, "task": job.snapshot()}

    async def _run_callback(self, job, operation):
        job.status, job.started_at = "running", asyncio.get_running_loop().time()
        job.record()
        try:
            job.result = await operation()
            job.status = "completed"
        except asyncio.CancelledError:
            job.status, job.error = "failed", "后台业务任务已取消。"
        except Exception as error:
            job.status, job.error = "failed", type(error).__name__
        finally:
            job.finished_at = asyncio.get_running_loop().time()
            job.record()


def create_background_tools(manager):
    @function_tool
    async def start_background(numbers: list[StrictInt]) -> dict:
        """启动固定约 4 秒的本地整数汇总，立即返回任务编号和提交时状态。

        Args:
            numbers: 本次任务明确给出的人工整数列表。
        """
        result = manager.start(numbers)
        await asyncio.sleep(0)  # 让 worker 获得一次运行机会，仍不等待它完成。
        return result

    @function_tool
    async def add(a: StrictInt, b: StrictInt) -> dict:
        """主 Agent 的独立工作：计算本次另两个整数的和，不等待后台结果。"""
        return manager.add(a, b)

    @function_tool
    async def get_task_status(task_id: str) -> dict:
        """查询本次任务的 pending / running / completed / failed 实时状态。"""
        return manager.get_status(task_id)

    @function_tool
    async def get_task_result(task_id: str) -> dict:
        """取得后台结果；未完成时本地等待最多 6 秒，不做反复模型轮询。"""
        return await manager.get_result(task_id, wait=True)

    return start_background, add, get_task_status, get_task_result
