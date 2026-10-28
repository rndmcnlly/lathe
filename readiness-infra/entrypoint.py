"""One migration process, then two supervised, ordinary uvicorn processes."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


PORTS = {"A": 8081, "B": 8082}


class Workers:
    """One migration, ordinary subprocesses, and explicit replacement."""
    def __init__(self):
        self.processes = {}
        self.handles = []

    def migrate(self):
        receipt = Path("/run/readiness/migration.json")
        if receipt.exists():
            raise RuntimeError("refusing a second migration")
        env = dict(os.environ, ENABLE_DB_MIGRATIONS="true")
        for key in ("DAYTONA_API_KEY", "OPENROUTER_API_KEY"):
            env.pop(key, None)
        with subprocess.Popen([sys.executable, "-c", "import worker"], env=env) as migration:
            try:
                if migration.wait(timeout=180):
                    raise RuntimeError("one-shot OWUI migration failed")
            finally:
                if migration.poll() is None:
                    migration.kill()
                    migration.wait()
        receipt.write_text(json.dumps({"pid": migration.pid, "count": 1}))
        Path("/app/backend/data/readiness-marker").write_text(os.environ["READINESS_RUN"])

    def start(self, name):
        if name in self.processes and self.processes[name].poll() is None:
            raise RuntimeError("worker already running")
        handle = open(f"/run/readiness/{name}-{time.time_ns()}.raw.log", "wb")
        self.handles.append(handle)
        env = dict(os.environ, ENABLE_DB_MIGRATIONS="false", READINESS_WORKER=name)
        env.pop("READINESS_PREVIOUS_PID", None)
        if name in self.processes:
            env["READINESS_PREVIOUS_PID"] = str(self.processes[name].pid)
        for key in ("DAYTONA_API_KEY", "OPENROUTER_API_KEY"):
            env.pop(key, None)
        self.processes[name] = subprocess.Popen([sys.executable, "-m", "uvicorn", "worker:app",
            "--host", "127.0.0.1", "--port", str(PORTS[name]), "--no-access-log"],
            env=env, stdout=handle, stderr=subprocess.STDOUT)

    def stop(self, name):
        child = self.processes.get(name)
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=10)

    def replace(self, name):
        self.stop(name)
        self.start(name)

    def close(self):
        try:
            for name in self.processes:
                self.stop(name)
        finally:
            for handle in self.handles:
                handle.close()


def main():
    Path("/run/readiness").mkdir(exist_ok=True)
    workers = Workers()

    def stop(signum, frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    # A container killed without its controller still has a bounded lifetime.
    signal.signal(signal.SIGALRM, stop)
    signal.alarm(900)
    try:
        workers.migrate()
        for name in PORTS:
            workers.start(name)
        while all(child.poll() is None for child in workers.processes.values()):
            time.sleep(0.25)
        raise RuntimeError("an OWUI worker exited")
    finally:
        workers.close()


if __name__ == "__main__":
    main()
