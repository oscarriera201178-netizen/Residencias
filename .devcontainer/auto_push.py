import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parent.parent
POLL_SECONDS = 2
QUIET_SECONDS = 8
PUSH_RETRY_SECONDS = 30


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        check=False,
    )


def report_error(action: str, result: subprocess.CompletedProcess[str]) -> None:
    details = (result.stderr or result.stdout).strip()
    print(f"Auto-sync: {action} falló: {details}", flush=True)


def main() -> None:
    print("Auto-sync: vigilando cambios; se publicarán tras 8 segundos sin ediciones.", flush=True)
    previous_status: str | None = None
    changed_at = time.monotonic()
    next_push_at = 0.0

    while True:
        status_result = git("status", "--porcelain", "--untracked-files=all")
        if status_result.returncode:
            report_error("git status", status_result)
            time.sleep(PUSH_RETRY_SECONDS)
            continue

        status = status_result.stdout
        now = time.monotonic()
        if status != previous_status:
            previous_status = status
            changed_at = now

        if status and now - changed_at >= QUIET_SECONDS:
            conflicts = git("diff", "--name-only", "--diff-filter=U")
            if conflicts.returncode:
                report_error("comprobar conflictos", conflicts)
                changed_at = now
            elif conflicts.stdout.strip():
                print("Auto-sync: hay conflictos Git; resuélvelos antes de publicar.", flush=True)
                changed_at = now
            else:
                staged = git("add", "--all")
                if staged.returncode:
                    report_error("git add", staged)
                    changed_at = now
                else:
                    message = datetime.now(timezone.utc).strftime("Auto-sync: %Y-%m-%d %H:%M UTC")
                    committed = git("commit", "-m", message)
                    if committed.returncode:
                        report_error("git commit", committed)
                        changed_at = now
                    else:
                        print(committed.stdout.strip(), flush=True)
                        previous_status = None
                        changed_at = now

        if now >= next_push_at:
            upstream = git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
            if upstream.returncode:
                report_error("detectar rama remota", upstream)
                next_push_at = now + PUSH_RETRY_SECONDS
            else:
                ahead = git("rev-list", "--count", f"{upstream.stdout.strip()}..HEAD")
                if ahead.returncode:
                    report_error("comprobar commits pendientes", ahead)
                    next_push_at = now + PUSH_RETRY_SECONDS
                elif int(ahead.stdout.strip()):
                    pushed = git("push")
                    if pushed.returncode:
                        report_error("git push", pushed)
                        next_push_at = now + PUSH_RETRY_SECONDS
                    else:
                        print("Auto-sync: cambios enviados a GitHub.", flush=True)
                        next_push_at = 0.0

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
