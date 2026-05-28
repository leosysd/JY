from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

from .time_utils import beijing_now, beijing_now_iso


TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
MAX_TELEGRAM_MESSAGE = 3800


@dataclass
class PendingTask:
    task_id: str
    chat_id: int
    user_id: int
    prompt: str
    created_at: float


@dataclass
class RunningJob:
    job_id: str
    chat_id: int
    prompt: str
    started_at: float
    log_path: Path
    final_path: Path
    process: subprocess.Popen[str]


class CodexTelegramBot:
    def __init__(self, config_path: Path) -> None:
        load_dotenv(config_path)
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        allowed = os.getenv("TELEGRAM_ALLOWED_USER_ID", "").strip()
        self.allowed_user_ids = {int(item.strip()) for item in allowed.split(",") if item.strip().isdigit()}
        self.workdir = Path(os.getenv("CODEX_TELEGRAM_WORKDIR", "/opt/jy/JY")).resolve()
        self.codex_bin = os.getenv("CODEX_TELEGRAM_CODEX_BIN", "codex").strip() or "codex"
        self.model = os.getenv("CODEX_TELEGRAM_MODEL", "").strip()
        self.job_dir = Path(os.getenv("CODEX_TELEGRAM_JOB_DIR", str(self.workdir / "data/codex_telegram/jobs"))).resolve()
        self.prompt_prefix = os.getenv("CODEX_TELEGRAM_PROMPT_PREFIX", "").strip()
        self.pending: Optional[PendingTask] = None
        self.running: Optional[RunningJob] = None
        self.last_job: Optional[Dict[str, Any]] = None
        self.lock = threading.Lock()
        self.session = requests.Session()
        self.offset = 0
        if not self.token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN 未设置")
        if not self.allowed_user_ids:
            raise RuntimeError("TELEGRAM_ALLOWED_USER_ID 未设置")
        self.job_dir.mkdir(parents=True, exist_ok=True)

    def run(self) -> None:
        self.send_startup_notice()
        while True:
            try:
                updates = self.api(
                    "getUpdates",
                    {
                        "offset": self.offset,
                        "timeout": 50,
                        "allowed_updates": json.dumps(["message"]),
                    },
                    timeout=60,
                ).get("result", [])
                for update in updates:
                    self.offset = max(self.offset, int(update.get("update_id", 0)) + 1)
                    self.handle_update(update)
            except Exception as exc:
                print(f"[TELEGRAM ERROR] {type(exc).__name__}: {exc}")
                time.sleep(3)

    def api(self, method: str, data: Dict[str, Any], timeout: int = 20) -> Dict[str, Any]:
        url = TELEGRAM_API.format(token=self.token, method=method)
        response = self.session.post(url, data=data, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API error: {payload}")
        return payload

    def send_message(self, chat_id: int, text: str) -> None:
        chunks = split_text(text, MAX_TELEGRAM_MESSAGE)
        for chunk in chunks:
            self.api(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": chunk,
                    "disable_web_page_preview": "true",
                },
            )

    def send_startup_notice(self) -> None:
        text = f"Codex Telegram 控制服务已启动。\n工作目录: {self.workdir}\n时间: {beijing_now_iso()}"
        for user_id in self.allowed_user_ids:
            try:
                self.send_message(user_id, text)
            except Exception as exc:
                print(f"[TELEGRAM NOTICE WARN] user={user_id} error={exc}")

    def handle_update(self, update: Dict[str, Any]) -> None:
        message = update.get("message") if isinstance(update.get("message"), dict) else {}
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        user = message.get("from") if isinstance(message.get("from"), dict) else {}
        chat_id = int(chat.get("id") or 0)
        user_id = int(user.get("id") or 0)
        text = str(message.get("text") or "").strip()
        if not text:
            return
        if user_id not in self.allowed_user_ids:
            if chat_id:
                self.send_message(chat_id, "未授权。")
            return
        command, arg = parse_command(text)
        if command in {"/start", "/help"}:
            self.send_message(chat_id, help_text())
        elif command == "/ping":
            self.send_message(chat_id, f"pong {beijing_now_iso()}")
        elif command == "/status":
            self.send_message(chat_id, self.status_text())
        elif command == "/codex":
            self.create_pending_task(chat_id, user_id, arg)
        elif command == "/approve":
            self.approve_pending(chat_id, arg)
        elif command == "/cancel":
            self.cancel_pending(chat_id)
        elif command == "/job":
            self.send_message(chat_id, self.job_text())
        elif command == "/stopjob":
            self.stop_job(chat_id)
        elif command == "/last":
            self.send_message(chat_id, self.last_job_text())
        elif command == "/logs":
            self.send_message(chat_id, self.log_tail_text(arg))
        else:
            self.send_message(chat_id, "未知命令。发送 /help 查看用法。")

    def create_pending_task(self, chat_id: int, user_id: int, prompt: str) -> None:
        if not prompt:
            self.send_message(chat_id, "用法：/codex 你的代码修改任务")
            return
        with self.lock:
            if self.running is not None and self.running.process.poll() is None:
                self.send_message(chat_id, f"已有任务运行中：{self.running.job_id}。发送 /job 查看。")
                return
            task_id = beijing_now().strftime("%Y%m%d%H%M%S")
            self.pending = PendingTask(task_id, chat_id, user_id, prompt, time.time())
        self.send_message(
            chat_id,
            "已创建待确认 Codex 任务。\n"
            f"任务ID: {task_id}\n"
            f"工作目录: {self.workdir}\n"
            f"内容:\n{prompt}\n\n"
            f"确认执行：/approve {task_id}\n取消：/cancel",
        )

    def approve_pending(self, chat_id: int, arg: str) -> None:
        with self.lock:
            task = self.pending
            if task is None:
                self.send_message(chat_id, "没有待确认任务。")
                return
            if arg.strip() != task.task_id:
                self.send_message(chat_id, f"任务ID不匹配。需要发送：/approve {task.task_id}")
                return
            if self.running is not None and self.running.process.poll() is None:
                self.send_message(chat_id, f"已有任务运行中：{self.running.job_id}")
                return
            self.pending = None
        self.start_codex_job(task)

    def cancel_pending(self, chat_id: int) -> None:
        with self.lock:
            self.pending = None
        self.send_message(chat_id, "已取消待确认任务。")

    def start_codex_job(self, task: PendingTask) -> None:
        job_id = task.task_id
        log_path = self.job_dir / f"{job_id}.log"
        final_path = self.job_dir / f"{job_id}.final.txt"
        prompt = self.build_prompt(task.prompt)
        cmd = [
            self.codex_bin,
            "exec",
            "--cd",
            str(self.workdir),
            "--sandbox",
            "danger-full-access",
            "--ask-for-approval",
            "never",
            "-o",
            str(final_path),
            "-",
        ]
        if self.model:
            cmd[2:2] = ["--model", self.model]
        log_fh = log_path.open("w", encoding="utf-8")
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(self.workdir),
        )
        assert process.stdin is not None
        process.stdin.write(prompt)
        process.stdin.close()
        job = RunningJob(job_id, task.chat_id, task.prompt, time.time(), log_path, final_path, process)
        with self.lock:
            self.running = job
        self.send_message(task.chat_id, f"Codex 任务已启动：{job_id}\n发送 /job 查看状态。")
        threading.Thread(target=self.watch_job, args=(job, log_fh), daemon=True).start()

    def build_prompt(self, prompt: str) -> str:
        prefix = self.prompt_prefix or (
            "你是通过 Telegram 远程启动的 Codex。"
            "在 /opt/jy/JY 仓库工作，用中文简明汇报。"
            "不要输出、提交或泄露 .env、Telegram token、私钥等敏感信息。"
            "修改代码后需要运行合适检查；如需部署，按本项目现有 VPS 流程同步到 /opt/polymarket-copy 并重启服务。"
            "除非用户明确要求，不要改真实下单开关。"
        )
        return f"{prefix}\n\n用户任务：\n{prompt}\n"

    def watch_job(self, job: RunningJob, log_fh: Any) -> None:
        code = job.process.wait()
        log_fh.close()
        final = read_text_tail(job.final_path, 3000) if job.final_path.exists() else read_text_tail(job.log_path, 3000)
        with self.lock:
            if self.running and self.running.job_id == job.job_id:
                self.running = None
            self.last_job = {
                "job_id": job.job_id,
                "code": code,
                "finished_at": time.time(),
                "log_path": str(job.log_path),
                "final_path": str(job.final_path),
                "prompt": job.prompt,
                "final": final,
            }
        status = "完成" if code == 0 else f"失败 code={code}"
        self.send_message(job.chat_id, f"Codex 任务 {job.job_id} {status}。\n\n{final}")

    def stop_job(self, chat_id: int) -> None:
        with self.lock:
            job = self.running
        if job is None or job.process.poll() is not None:
            self.send_message(chat_id, "当前没有运行中的任务。")
            return
        job.process.terminate()
        self.send_message(chat_id, f"已请求停止任务：{job.job_id}")

    def status_text(self) -> str:
        service = run_cmd(["systemctl", "is-active", "polymarket-copy"], self.workdir)
        telegram = run_cmd(["systemctl", "is-active", "jy-codex-telegram"], self.workdir)
        git_status = run_cmd(["git", "status", "-sb"], self.workdir)
        git_log = run_cmd(["git", "log", "--oneline", "-3"], self.workdir)
        return (
            f"时间: {beijing_now_iso()}\n"
            f"交易服务: {service.strip()}\n"
            f"Telegram-Codex服务: {telegram.strip()}\n"
            f"工作目录: {self.workdir}\n\n"
            f"{git_status.strip()}\n\n"
            f"{git_log.strip()}"
        )

    def job_text(self) -> str:
        with self.lock:
            pending = self.pending
            running = self.running
        if running is not None and running.process.poll() is None:
            elapsed = int(time.time() - running.started_at)
            tail = read_text_tail(running.log_path, 1200)
            return f"运行中：{running.job_id}\n已运行: {elapsed}s\n日志: {running.log_path}\n\n{tail}"
        if pending is not None:
            return f"待确认：{pending.task_id}\n内容:\n{pending.prompt}\n\n/approve {pending.task_id}"
        return "没有运行中或待确认的任务。"

    def last_job_text(self) -> str:
        with self.lock:
            last = self.last_job
        if not last:
            return "暂无已完成任务。"
        return (
            f"任务: {last['job_id']}\n"
            f"退出码: {last['code']}\n"
            f"日志: {last['log_path']}\n\n"
            f"{last.get('final') or ''}"
        )

    def log_tail_text(self, arg: str) -> str:
        job_id = arg.strip()
        if not job_id:
            with self.lock:
                job_id = self.running.job_id if self.running else str((self.last_job or {}).get("job_id") or "")
        if not job_id:
            return "没有可查看的任务日志。"
        path = self.job_dir / f"{job_id}.log"
        if not path.exists():
            return f"找不到日志：{path}"
        return f"{path}\n\n{read_text_tail(path, 3000)}"


def parse_command(text: str) -> tuple[str, str]:
    parts = text.strip().split(maxsplit=1)
    command = parts[0].split("@", 1)[0].lower()
    arg = parts[1] if len(parts) > 1 else ""
    return command, arg


def split_text(text: str, limit: int) -> List[str]:
    if len(text) <= limit:
        return [text]
    chunks: List[str] = []
    remaining = text
    while remaining:
        chunks.append(remaining[:limit])
        remaining = remaining[limit:]
    return chunks


def read_text_tail(path: Path, limit: int) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"读取失败: {exc}"
    if len(text) <= limit:
        return text
    return text[-limit:]


def run_cmd(cmd: List[str], cwd: Path) -> str:
    try:
        result = subprocess.run(cmd, cwd=str(cwd), text=True, capture_output=True, timeout=10, check=False)
    except Exception as exc:
        return f"ERROR {type(exc).__name__}: {exc}"
    output = (result.stdout + result.stderr).strip()
    return output or f"exit={result.returncode}"


def help_text() -> str:
    return (
        "Codex Telegram 控制命令：\n"
        "/status 查看服务和 Git 状态\n"
        "/codex <任务> 创建 Codex 代码任务\n"
        "/approve <任务ID> 确认执行\n"
        "/cancel 取消待确认任务\n"
        "/job 查看当前任务\n"
        "/stopjob 停止当前任务\n"
        "/last 查看上个任务结果\n"
        "/logs [任务ID] 查看任务日志\n"
        "/ping 测试连通\n\n"
        "安全规则：只有白名单 Telegram ID 可用；/codex 必须二次确认。"
    )


def main() -> None:
    config_path = Path(os.getenv("CODEX_TELEGRAM_ENV", "/opt/polymarket-copy/.env"))
    bot = CodexTelegramBot(config_path)
    bot.run()


if __name__ == "__main__":
    main()
