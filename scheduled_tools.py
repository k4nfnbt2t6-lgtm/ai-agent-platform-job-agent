"""Scheduled Tasks：进程内的有限计时器，只调度人工整数汇总。"""

import asyncio
import copy
import math
import secrets
from dataclasses import dataclass

from agents import function_tool
from pydantic import StrictFloat, StrictInt

from background_tools import BackgroundJob, BackgroundTasks


@dataclass
class ScheduledJob(BackgroundJob):
    run_at: float = 0.0
    delay_seconds: float = 0.0

    def snapshot(self):
        return super().snapshot() | {"run_at": self.run_at, "delay_seconds": self.delay_seconds}


class ScheduledTasks:
    """计时器等到期限才启动 worker；仅在当前事件循环存活期间有效。"""

    def __init__(self, numbers, delay_seconds=3.0, max_jobs=1, compute=sum):
        BackgroundTasks.validate_numbers(numbers)
        if type(max_jobs) is not int or max_jobs not in (1, 2):
            raise ValueError("本节最多安排两次任务，真实模型只允许一次。")
        self.validate_delay(delay_seconds)
        self.numbers, self.delay_seconds = list(numbers), float(delay_seconds)
        self.max_jobs, self.compute = max_jobs, compute
        self.jobs, self.timers, self.workers, self.finished = {}, {}, {}, {}
        self.observations = []
        self.closed = False

    @staticmethod
    def validate_delay(delay):
        if type(delay) not in (int, float) or not math.isfinite(delay) or not 0 < delay <= 5:
            raise ValueError("延迟必须大于 0、最多 5 秒，且是有限数值。")

    def schedule(self, numbers, delay_seconds):
        BackgroundTasks.validate_numbers(numbers)
        self.validate_delay(delay_seconds)
        if self.closed or len(self.jobs) >= self.max_jobs:
            raise ValueError("管理器已关闭，或已达到本节调度次数上限。")
        allowed_delays = (self.delay_seconds,) if self.max_jobs == 1 else (self.delay_seconds, self.delay_seconds * 2)
        if numbers != self.numbers or delay_seconds not in allowed_delays:
            raise ValueError("只允许本次人工数据和明确约定的短延迟。")
        loop = asyncio.get_running_loop()
        now = loop.time()
        job = ScheduledJob(task_id="scheduled-" + secrets.token_hex(6), numbers=list(numbers),
                           created_at=now, status="scheduled", run_at=now + delay_seconds,
                           delay_seconds=float(delay_seconds))
        self.jobs[job.task_id] = job
        self.finished[job.task_id] = loop.create_future()
        job.record()
        self.timers[job.task_id] = loop.call_at(job.run_at, self._launch, job)
        self.observe(job)
        return {"ok": True, "task": job.snapshot()}

    def _launch(self, job):
        if self.closed or job.status != "scheduled":
            return
        loop = asyncio.get_running_loop()
        now = loop.time()
        # 某些计时器会在时钟精度范围内稍早唤醒；到期前绝不启动 worker。
        if now < job.run_at:
            self.timers[job.task_id] = loop.call_at(job.run_at, self._launch, job)
            return
        job.status, job.started_at = "running", now
        job.record()
        print(f"调度器自动启动 {job.task_id}；比计划时间晚 {now - job.run_at:.6f} 秒。", flush=True)
        self.workers[job.task_id] = asyncio.create_task(self._run(job))

    async def _run(self, job):
        try:
            await asyncio.sleep(0.1)  # 短暂让出执行机会，便于观察 running；计算真实执行。
            total = self.compute(job.numbers)
            if type(total) is not int:
                raise ValueError("汇总没有返回整数。")
            job.result = {"sum": total, "count": len(job.numbers), "receipt": secrets.token_hex(6)}
            job.status = "completed"
        except asyncio.CancelledError:
            job.status, job.error = "failed", "任务已取消：当前练习进程正在结束。"
        except Exception as error:
            job.status, job.error = "failed", type(error).__name__
        finally:
            job.finished_at = asyncio.get_running_loop().time()
            job.record()
            if not self.finished[job.task_id].done():
                self.finished[job.task_id].set_result(None)

    def observe(self, job):
        event = {"at": asyncio.get_running_loop().time(), "task": job.snapshot()}
        self.observations.append(event)
        return event

    def get_status(self, task_id):
        if task_id not in self.jobs:
            return {"ok": False, "error": "未知任务编号。"}
        event = self.observe(self.jobs[task_id])
        return {"ok": True, "observed_at": event["at"], "task": event["task"]}

    async def get_result(self, task_id, wait=False):
        if task_id not in self.jobs:
            return {"ok": False, "error": "未知任务编号。"}
        if wait:
            try:
                await asyncio.wait_for(asyncio.shield(self.finished[task_id]), timeout=6.0)
            except TimeoutError:
                return {"ok": False, "error": "本地等待超过 6 秒。", "task": self.jobs[task_id].snapshot()}
        task = self.jobs[task_id].snapshot()
        return {"ok": task["status"] != "failed", "ready": task["status"] in ("completed", "failed"), "task": task}

    async def close(self):
        self.closed = True
        for timer in self.timers.values():
            timer.cancel()
        for worker in self.workers.values():
            if not worker.done():
                worker.cancel()
        if self.workers:
            await asyncio.gather(*self.workers.values(), return_exceptions=True)
        for task_id, job in self.jobs.items():
            if job.status in ("scheduled", "running"):
                job.status, job.error = "failed", "任务已取消：当前练习进程正在结束。"
                job.finished_at = asyncio.get_running_loop().time()
                job.record()
            if not self.finished[task_id].done():
                self.finished[task_id].set_result(None)

    def snapshot(self):
        return {"tasks": [job.snapshot() | {"history": copy.deepcopy(job.history)} for job in self.jobs.values()],
                "observations": copy.deepcopy(self.observations), "closed": self.closed}


class ScheduledCallbacks(ScheduledTasks):
    """宿主的单个业务计时器；复用到期检查、worker、查询和退出清理。"""

    def __init__(self):
        self.jobs, self.timers, self.workers, self.finished = {}, {}, {}, {}
        self.observations, self.callbacks = [], {}
        self.closed = False

    def schedule_callback(self, operation, delay_seconds, on_done=None):
        if self.closed or any(job.status in ("scheduled", "running") for job in self.jobs.values()):
            raise ValueError("业务计时器已安排或已关闭。")
        if not callable(operation) or type(delay_seconds) not in (int, float) or not math.isfinite(delay_seconds) or not 0 <= delay_seconds <= 86400:
            raise ValueError("业务计时器需要宿主操作和 0 到 86400 秒的有限延迟。")
        self.jobs.clear()
        self.timers.clear()
        self.workers.clear()
        self.finished.clear()
        self.callbacks.clear()
        self.observations.clear()
        loop = asyncio.get_running_loop()
        job = ScheduledJob("scheduled-" + secrets.token_hex(6), [], loop.time(), status="scheduled",
                           run_at=loop.time() + delay_seconds, delay_seconds=delay_seconds)
        self.jobs[job.task_id] = job
        self.callbacks[job.task_id] = (operation, on_done)
        self.finished[job.task_id] = loop.create_future()
        job.record()
        self.timers[job.task_id] = loop.call_at(job.run_at, self._launch, job)
        self.observe(job)
        return {"ok": True, "task": job.snapshot()}

    async def _run(self, job):
        operation, on_done = self.callbacks[job.task_id]
        try:
            job.result = await operation()
            job.status = "completed"
        except asyncio.CancelledError:
            job.status, job.error = "failed", "业务计时器已取消。"
        except Exception as error:
            job.status, job.error = "failed", type(error).__name__
        finally:
            job.finished_at = asyncio.get_running_loop().time()
            job.record()
            if not self.finished[job.task_id].done():
                self.finished[job.task_id].set_result(None)
            if on_done is not None and not self.closed:
                asyncio.get_running_loop().call_soon(on_done)


def create_scheduled_tools(manager):
    @function_tool
    async def schedule_task(numbers: list[StrictInt], delay_seconds: StrictFloat) -> dict:
        """在约定的几秒之后自动汇总人工数据；提交立即返回 scheduled 和任务编号。

        Args:
            numbers: 本次用户指定的人工整数。
            delay_seconds: 本次任务指定的短延迟，不能任意扩展时间。
        """
        return manager.schedule(numbers, delay_seconds)

    @function_tool
    async def get_task_status(task_id: str) -> dict:
        """立即查询 scheduled / running / completed / failed 状态，不启动任务。"""
        return manager.get_status(task_id)

    @function_tool
    async def get_task_result(task_id: str) -> dict:
        """取得自动执行的结果；本地最多等待 6 秒，不提前启动、不反复请求模型。"""
        return await manager.get_result(task_id, wait=True)

    return schedule_task, get_task_status, get_task_result
